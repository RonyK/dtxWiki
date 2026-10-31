import { expect, type Page, type Response } from "@playwright/test";
import { createHash } from "node:crypto";

import type { WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";
import { backendBaseUrl } from "./auth";

const OVERVIEW_PATH = "/graph/overview/query";

type OverviewWireRecord = {
  requestId: string; url: string; postText: string | null;
  responseUrl: string | null; status: number | null;
  contentEncoding: string | null;
  encodedDataLength: number | null; body: string | null; bodyBytes: number; bodySha256: string;
};

export async function createOverviewResponseReader(
  page: Page, workspaceId: string, options: { resourceLimitMiB?: 64 | 192 } = {},
) {
  // Network payload retention belongs to the DevTools session. Read the
  // original UI response through this SAME public session, never PW's default
  // response.json()/text(), interception, private fields or another fetch.
  const resourceLimitMiB = options.resourceLimitMiB ?? 64;
  if (resourceLimitMiB !== 64 && resourceLimitMiB !== 192) throw new Error("Overview response retention must use the bounded 64MiB or dense-fixture 192MiB profile");
  const network = await page.context().newCDPSession(page);
  const exactUrl = `${backendBaseUrl}/api/v1/workspaces/${workspaceId}${OVERVIEW_PATH}`;
  const resourceLimit = resourceLimitMiB * 1024 * 1024;
  const postLimit = 1024 * 1024;
  const records = new Map<string, OverviewWireRecord>();
  const seenIds = new Set<string>();
  const captures: Array<{ requestId: string; url: string; status: number; actualUiPostMatched: true;
    postUtf8Bytes: number; postSha256: string; bodyUtf8Bytes: number; bodySha256: string;
    encodedDataLength: number; contentEncoding: string | null; graphRevision: string }> = [];
  let fatal: Error | null = null;
  let disposed = false;
  let pendingCheck: (() => void) | null = null;
  const fail = (error: unknown) => {
    fatal ??= error instanceof Error ? error : new Error(String(error));
    pendingCheck?.();
  };
  const onRequest = (event: { requestId: string; request: { url: string; method: string; postData?: string };
    redirectResponse?: { status: number } }) => {
    if (seenIds.has(event.requestId)) {
      fail(new Error(`Overview CDP request ID reused or redirected: ${event.requestId}`));
      return;
    }
    if (event.request.url !== exactUrl || event.request.method !== "POST") return;
    if (event.redirectResponse || seenIds.size >= 3 || records.size) {
      fail(new Error("Overview reader permits at most three sequential original UI reads, without redirects or overlap"));
      return;
    }
    const record: OverviewWireRecord = { requestId: event.requestId, url: event.request.url,
      postText: event.request.postData ?? null, responseUrl: null, status: null,
      contentEncoding: null,
      encodedDataLength: null, body: null, bodyBytes: 0, bodySha256: "" };
    // Register synchronously before any async work: loadingFinished can race
    // metadata retrieval. Keep only this exact endpoint and the single
    // whitelisted Content-Encoding value, never complete headers/events.
    records.set(event.requestId, record);
    seenIds.add(event.requestId);
    if (record.postText === null) {
      // This reads metadata for the same requestId; it issues no HTTP request.
      void network.send("Network.getRequestPostData", { requestId: event.requestId }).then((result) => {
        if (disposed) return;
        record.postText = result.base64Encoded ? Buffer.from(result.postData, "base64").toString("utf8") : result.postData;
        if (Buffer.byteLength(record.postText, "utf8") > postLimit) throw new Error("Overview POST exceeds the 1MiB metadata bound");
        pendingCheck?.();
      }).catch(fail);
    } else if (Buffer.byteLength(record.postText, "utf8") > postLimit) {
      fail(new Error("Overview POST exceeds the 1MiB metadata bound"));
    }
    pendingCheck?.();
  };
  const onResponse = (event: { requestId: string; response: { url: string; status: number;
    headers: Record<string, string> } }) => {
    const record = records.get(event.requestId);
    if (!record) return;
    record.responseUrl = event.response.url;
    record.status = event.response.status;
    record.contentEncoding = Object.entries(event.response.headers)
      .find(([name]) => name.toLowerCase() === "content-encoding")?.[1] ?? null;
    if (record.responseUrl !== exactUrl || record.status < 200 || record.status >= 300) {
      fail(new Error(`Original overview response URL/status mismatch: ${record.responseUrl} ${record.status}`));
    }
    pendingCheck?.();
  };
  const onFinished = (event: { requestId: string; encodedDataLength: number }) => {
    const record = records.get(event.requestId);
    if (!record || record.encodedDataLength !== null) return;
    record.encodedDataLength = event.encodedDataLength;
    // Eagerly capture now, not after a UI/assertion/PW-event wait. Handle every
    // rejection immediately so an early inspector failure cannot go unhandled.
    void network.send("Network.getResponseBody", { requestId: event.requestId }).then((result) => {
      if (disposed) return;
      const bytes = Buffer.from(result.body, result.base64Encoded ? "base64" : "utf8");
      if (bytes.length > resourceLimit) throw new Error(`Original overview body exceeds ${resourceLimitMiB}MiB: ${bytes.length} bytes`);
      record.bodyBytes = bytes.length;
      record.bodySha256 = createHash("sha256").update(bytes).digest("hex");
      record.body = bytes.toString("utf8");
      pendingCheck?.();
    }).catch(fail);
  };
  const onFailed = (event: { requestId: string; errorText: string }) => {
    if (records.has(event.requestId)) fail(new Error(`Original overview loading failed: ${event.errorText}`));
  };
  const onClosed = () => fail(new Error("Original overview response reader target/session closed"));
  network.on("Network.requestWillBeSent", onRequest);
  network.on("Network.responseReceived", onResponse);
  network.on("Network.loadingFinished", onFinished);
  network.on("Network.loadingFailed", onFailed);
  network.on("close", onClosed);
  page.on("close", onClosed);
  async function dispose() {
    if (disposed) return;
    disposed = true;
    fail(new Error("Original overview response reader disposed"));
    network.off("Network.requestWillBeSent", onRequest);
    network.off("Network.responseReceived", onResponse);
    network.off("Network.loadingFinished", onFinished);
    network.off("Network.loadingFailed", onFailed);
    network.off("close", onClosed);
    page.off("close", onClosed);
    records.clear();
    // Detachment is cleanup only; a target already closed may reject it.
    await network.detach().catch(() => undefined);
  }
  try {
    await network.send("Network.enable", { maxResourceBufferSize: resourceLimit,
      maxTotalBufferSize: 256 * 1024 * 1024, maxPostDataSize: postLimit });
  } catch (error) {
    await dispose();
    throw error;
  }
  return {
    dispose,
    evidence: () => {
      if (fatal) throw fatal;
      if (records.size || seenIds.size !== captures.length) {
        throw new Error("Unconsumed original overview response at the evidence boundary");
      }
      return [...captures];
    },
    async read(response: Response): Promise<WorkspaceGraphScene> {
      const request = response.request();
      const postText = request.postData();
      if (disposed || request.url() !== exactUrl || request.method() !== "POST" || postText === null
        || response.url() !== exactUrl || !response.ok() || request.redirectedFrom()) {
        throw new Error("Expected the same original, nonredirected production UI overview response");
      }
      if (pendingCheck) throw new Error("Overview response consumption must be sequential");
      const record = await new Promise<OverviewWireRecord>((resolve, reject) => {
        const finish = (error: Error | null, value?: OverviewWireRecord) => {
          clearTimeout(timeout);
          pendingCheck = null;
          if (error) reject(error); else resolve(value!);
        };
        const timeout = setTimeout(() => finish(new Error("Timed out capturing the original overview CDP response")), 60_000);
        pendingCheck = () => {
          if (fatal) { finish(fatal); return; }
          const matches = [...records.values()].filter((row) => row.url === request.url() && row.postText === postText);
          if (matches.length > 1) { finish(new Error("Ambiguous original overview URL/POST correlation")); return; }
          if (!matches.length && records.size === 1 && [...records.values()][0].postText !== null) {
            finish(new Error("CDP overview raw POST text does not match the actual Playwright UI request"));
            return;
          }
          const match = matches[0];
          if (!match || match.status === null || match.responseUrl === null
            || match.encodedDataLength === null || match.body === null) return;
          if (match.status !== response.status() || match.responseUrl !== response.url()) {
            finish(new Error("CDP overview status/URL does not match the actual Playwright UI response"));
            return;
          }
          finish(null, match);
        };
        pendingCheck();
      });
      const envelope = JSON.parse(record.body!) as { data: WorkspaceGraphScene };
      if (!envelope.data || typeof envelope.data.graph_revision !== "string") throw new Error("Original overview body has no scene envelope");
      // Chromium decoded these same original UI responses. Require real wire
      // compression without weakening any identity/topology assertions above.
      expect(record.contentEncoding).toBe("gzip");
      expect(record.encodedDataLength!).toBeGreaterThan(0);
      expect(record.encodedDataLength!).toBeLessThan(record.bodyBytes);
      captures.push({ requestId: record.requestId, url: record.url, status: record.status!, actualUiPostMatched: true,
        postUtf8Bytes: Buffer.byteLength(postText, "utf8"), postSha256: createHash("sha256").update(postText).digest("hex"),
        bodyUtf8Bytes: record.bodyBytes, bodySha256: record.bodySha256,
        encodedDataLength: record.encodedDataLength!, contentEncoding: record.contentEncoding,
        graphRevision: envelope.data.graph_revision });
      // Initial and return-to-2K have identical POSTs: retire the consumed ID
      // and large body before the next explicit UI action can issue its read.
      records.delete(record.requestId);
      record.body = null;
      return envelope.data;
    },
  };
}

export type OverviewResponseReader = Awaited<ReturnType<typeof createOverviewResponseReader>>;

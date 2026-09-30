import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ImageUploadApiError, uploadImage } from "../uploads";

const BASE = "http://127.0.0.1:21080";
const ASSET_ID = "123e4567-e89b-12d3-a456-426614174000";

describe("uploads api", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    delete window.__DTXWIKI_RUNTIME_CONFIG__;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("targets the page's editing version and preserves additive context media fields", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          mime: "image/png",
          bytes: 5,
          sha256: "a".repeat(64),
          asset_id: ASSET_ID,
          logical_ref: `dtx-media://asset/${ASSET_ID}`,
          content_url: `/api/v1/workspaces/workspace-1/context/pages/page-1/versions/version-1/media/${ASSET_ID}/content`,
        },
      }),
    );

    const result = await uploadImage({
      file: new File(["image"], "image.png", { type: "image/png" }),
      marker: "marker-1",
      workspaceId: "workspace-1",
      pageId: "page-1",
      editingVersionId: "version-1",
      policyRevision: "context-mvp-v1",
    });

    const request = fetchMock.mock.calls[0][1];
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `${BASE}/api/v1/workspaces/workspace-1/context/pages/page-1/media`,
    );
    expect(request).toMatchObject({ method: "POST", credentials: "include" });
    const body = request?.body as FormData;
    expect(body.get("marker_uuid")).toBe("marker-1");
    expect(body.get("editing_version_id")).toBe("version-1");
    expect(new Headers(request?.headers).get("X-DTX-Media-Policy-Revision")).toBe(
      "context-mvp-v1",
    );
    expect(result).toMatchObject({
      asset_id: ASSET_ID,
      logical_ref: `dtx-media://asset/${ASSET_ID}`,
    });
  });

  it("rejects a legacy url-only response from a canonical target", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          url: "/api/v1/uploads/workspace-1/legacy.png",
          mime: "image/png",
          bytes: 4,
          sha256: "b".repeat(64),
        },
      }),
    );

    await expect(uploadImage({
      file: new File(["data"], "legacy.png", { type: "image/png" }),
      marker: "marker-legacy-response",
      workspaceId: "workspace-1",
      pageId: "page-1",
      editingVersionId: "version-1",
    })).rejects.toThrow("Upload response was incomplete");
  });

  it("forwards cancellation to the image upload request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          asset_id: ASSET_ID,
          logical_ref: `dtx-media://asset/${ASSET_ID}`,
          content_url: `/api/v1/workspaces/workspace-1/context/pages/page-1/versions/version-1/media/${ASSET_ID}/content`,
          mime: "image/png",
          bytes: 4,
          sha256: "c".repeat(64),
        },
      }),
    );
    const controller = new AbortController();

    await uploadImage({
      file: new File(["data"], "cancellable.png", { type: "image/png" }),
      marker: "marker-cancellable",
      workspaceId: "workspace-1",
      pageId: "page-1",
      editingVersionId: "version-1",
      signal: controller.signal,
    });

    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `${BASE}/api/v1/workspaces/workspace-1/context/pages/page-1/media`,
    );
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);
  });

  it("exposes Retry-After delta-seconds on image upload errors", async () => {
    const error = await rejectImageUploadWithRetryAfter("7");

    expect(error).toBeInstanceOf(ImageUploadApiError);
    expect(error).toMatchObject({
      status: 429,
      body: { error_code: "media_upload_rate_limited" },
      retryAfterMs: 7_000,
    });
  });

  it("exposes Retry-After HTTP-date relative to the response time", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-08T00:00:00.000Z"));

    const error = await rejectImageUploadWithRetryAfter(
      "Sat, 08 Aug 2026 00:00:12 GMT",
    );

    expect(error).toBeInstanceOf(ImageUploadApiError);
    expect(error).toMatchObject({ retryAfterMs: 12_000 });
  });

  it("reports invalid Retry-After values without inventing a delay", async () => {
    const error = await rejectImageUploadWithRetryAfter("not-a-retry-time");

    expect(error).toBeInstanceOf(ImageUploadApiError);
    expect(error).toMatchObject({ retryAfterMs: null });
  });
});

async function rejectImageUploadWithRetryAfter(
  retryAfter: string,
): Promise<unknown> {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    jsonResponse(
      {
        error_code: "media_upload_rate_limited",
        message: "Upload admission is temporarily rate limited.",
        request_id: "request-429",
        warnings: [],
      },
      429,
      { "Retry-After": retryAfter },
    ),
  );

  try {
    await uploadImage({
      file: new File(["data"], "retry.png", { type: "image/png" }),
      marker: "marker-retry",
      workspaceId: "workspace-1",
      pageId: "page-1",
      editingVersionId: "version-1",
    });
  } catch (error) {
    return error;
  }
  throw new Error("Expected image upload to reject.");
}

function jsonResponse(
  body: unknown,
  status = 200,
  headers?: HeadersInit,
) {
  const responseHeaders = new Headers(headers);
  responseHeaders.set("Content-Type", "application/json");
  return new Response(JSON.stringify(body), {
    status,
    headers: responseHeaders,
  });
}

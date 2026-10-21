import type { Page, Request, Response } from "@playwright/test";

import { redactFailedRequest } from "./redact-network.ts";

const RESPONSE_COMPLETION_TIMEOUT_MS = 15_000;
const MAX_LIFECYCLE_EVENTS = 160;

/**
 * A response event only guarantees headers. Wait for the same request's body
 * completion before calling response.json(), which has no Playwright timeout.
 * Arm every listener before the action and release them on every exit path.
 */
export async function runWithCompletedResponse(
  page: Page,
  matchesRequest: (request: Request) => boolean,
  action: () => Promise<unknown>,
  timeoutMs = RESPONSE_COMPLETION_TIMEOUT_MS,
): Promise<Response> {
  let matchedResponse: Response | null = null;
  let matchedRequest: Request | null = null;
  let resolveCompletion!: (response: Response) => void;
  let rejectCompletion!: (error: Error) => void;
  const completion = new Promise<Response>((resolve, reject) => {
    resolveCompletion = resolve;
    rejectCompletion = reject;
  });
  const onResponse = (response: Response) => {
    if (!matchedRequest && matchesRequest(response.request())) {
      matchedResponse = response;
      matchedRequest = response.request();
    }
  };
  const onFinished = (request: Request) => {
    if (request === matchedRequest && matchedResponse) {
      resolveCompletion(matchedResponse);
    }
  };
  const onFailed = (request: Request) => {
    if (request === matchedRequest || (!matchedRequest && matchesRequest(request))) {
      rejectCompletion(
        new Error("Matched request failed before its response body completed"),
      );
    }
  };
  const onClosed = () =>
    rejectCompletion(new Error("Page closed before response completion"));
  const onCrashed = () =>
    rejectCompletion(new Error("Page crashed before response completion"));
  page.on("response", onResponse);
  page.on("requestfinished", onFinished);
  page.on("requestfailed", onFailed);
  page.on("close", onClosed);
  page.on("crash", onCrashed);
  const timer = setTimeout(() => {
    rejectCompletion(
      new Error(
        `Response completion exceeded ${timeoutMs}ms (${matchedResponse ? "headers received; body unfinished" : "no matching response headers"})`,
      ),
    );
  }, timeoutMs);
  try {
    // Attach rejection handlers before invoking the action, including actions
    // that throw synchronously. No background body-read promise is started.
    const [response] = await Promise.all([
      completion,
      Promise.resolve().then(action),
    ]);
    return response;
  } finally {
    clearTimeout(timer);
    page.off("response", onResponse);
    page.off("requestfinished", onFinished);
    page.off("requestfailed", onFailed);
    page.off("close", onClosed);
    page.off("crash", onCrashed);
  }
}

type LifecycleEntry = {
  at_ms: number;
  event: string;
  request_id?: number;
  method?: string;
  origin?: string;
  pathname?: string;
  status?: number;
  errorText?: string;
};

/** Safe bounded metadata for pages created outside the shared page fixture. */
export function createPageLifecycleRecorder(page: Page) {
  const started = performance.now();
  const entries: LifecycleEntry[] = [];
  const requestIds = new WeakMap<Request, number>();
  let nextRequestId = 0;
  let droppedEvents = 0;
  const record = (entry: Omit<LifecycleEntry, "at_ms">) => {
    if (entries.length === MAX_LIFECYCLE_EVENTS) {
      entries.shift();
      droppedEvents += 1;
    }
    entries.push({ at_ms: Math.round(performance.now() - started), ...entry });
  };
  const requestEntry = (event: string, request: Request, status?: number) => {
    if (!requestIds.has(request)) requestIds.set(request, ++nextRequestId);
    const { errorText, ...identity } = redactFailedRequest({
      method: request.method(),
      url: request.url(),
      errorText: event === "requestfailed" ? request.failure()?.errorText : "",
    });
    record({
      event,
      request_id: requestIds.get(request),
      ...identity,
      ...(status === undefined ? {} : { status }),
      ...(event === "requestfailed" ? { errorText } : {}),
    });
  };
  const onRequest = (request: Request) => requestEntry("request", request);
  const onResponse = (response: Response) =>
    requestEntry("response", response.request(), response.status());
  const onFinished = (request: Request) => requestEntry("requestfinished", request);
  const onFailed = (request: Request) => requestEntry("requestfailed", request);
  const onDomReady = () => record({ event: "domcontentloaded" });
  const onLoad = () => record({ event: "load" });
  const onClose = () => record({ event: "close" });
  const onCrash = () => record({ event: "crash" });
  page.on("request", onRequest);
  page.on("response", onResponse);
  page.on("requestfinished", onFinished);
  page.on("requestfailed", onFailed);
  page.on("domcontentloaded", onDomReady);
  page.on("load", onLoad);
  page.on("close", onClose);
  page.on("crash", onCrash);
  return {
    snapshot: () => ({ events: [...entries], dropped_events: droppedEvents }),
    stop() {
      page.off("request", onRequest);
      page.off("response", onResponse);
      page.off("requestfinished", onFinished);
      page.off("requestfailed", onFailed);
      page.off("domcontentloaded", onDomReady);
      page.off("load", onLoad);
      page.off("close", onClose);
      page.off("crash", onCrash);
    },
  };
}

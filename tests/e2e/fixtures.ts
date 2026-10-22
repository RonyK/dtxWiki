import {
  expect,
  test as base,
  type Page,
  type Request,
} from "@playwright/test";

export { expect };
export type {
  APIRequestContext,
  APIResponse,
  BoundingBox,
  Browser,
  BrowserContext,
  Locator,
  Page,
  Request,
  Response,
  TestInfo,
} from "@playwright/test";

import {
  createFailedRequestLog,
  type FailedRequestReport,
} from "./redact-network.ts";

/** How long after a transport failure the SAME url's import error is excused. */
export const NETWORK_CHANGE_WINDOW_MS = 10_000;

export type NetworkChangeLogEntry = { at: number; url: string };

export type PageErrorVerdict = {
  blocking: boolean;
  kind: "product-error" | "recovered-import";
  cause: NetworkChangeLogEntry | null;
};

const networkChangeLog = new WeakMap<Page, NetworkChangeLogEntry[]>();

/**
 * Every route-level recovery control the ERR_NETWORK_CHANGED fixture is allowed
 * to click, addressed by stable test id rather than by presentation class.
 *
 * The shipped selector was `.route-loading [role="alert"] button.primary,
 * .workspace-resolution-card button.primary`. It matched exactly two of the
 * controls it was meant to reach -- RootRouteError and Shell's
 * workspace-resolution error card -- and silently missed RequireAuth's
 * session-verification Retry and RequireEditor's workspace Retry, both of which
 * are `icon-button text-button` with no `primary`. A class that exists for
 * styling is not a contract; these ids are, and
 * tests/static/test_e2e_artifact_redaction_contract.py keeps them in sync with
 * frontend/src/app/App.tsx and frontend/src/shared/components/Shell.tsx.
 */
export const RECOVERY_TEST_IDS = [
  "session-verify-retry",
  "workspace-access-retry",
  "workspace-resolution-retry",
  "route-error-reload",
] as const;

const pendingRecovery = new WeakMap<Page, Promise<void>>();

export type NetworkChangeRecorder = {
  onRequestFailed(request: Request): void;
  settle(): Promise<void>;
  recovered(): boolean;
  isEmpty(): boolean;
  report(): FailedRequestReport;
  summary(): string;
};

/**
 * The fixture's requestfailed handler, extracted so it can be driven directly
 * by tests/e2e/redact-network.test.mjs. Two invariants live here:
 *
 * 1. Nothing but a redacted (method, origin, pathname, errorText) tuple is ever
 *    retained for an artifact -- see ./redact-network.ts.
 * 2. At most ONE auto-recovery click per test. `attempted` is set
 *    synchronously, before the promise exists, and is never reset. The shipped
 *    version cleared its in-flight marker in the `.then`, so every subsequent
 *    failed GET started a fresh blind 5s wait and a fresh click -- and the
 *    click that mattered was the second one, landing on a form the test had
 *    already filled.
 *
 * This remains correlation, not causation: nothing ties the failed URL to the
 * error state being clicked. `retryOnceAfterNetworkChange` in
 * tests/e2e/network_retry.ts is the explicit, non-racing alternative a spec
 * should reach for; this layer only bounds the damage of the implicit one.
 */
export function createNetworkChangeRecorder(
  page: Page,
  recover: (page: Page) => Promise<boolean> = recoverFromNetworkChange,
): NetworkChangeRecorder {
  const failedRequests = createFailedRequestLog();
  let recovery: Promise<void> | null = null;
  let attempted = false;
  let didRecover = false;

  return {
    onRequestFailed(request: Request) {
      const errorText = request.failure()?.errorText ?? "";
      if (
        request.method() !== "GET" ||
        !errorText.includes("ERR_NETWORK_CHANGED")
      ) {
        return;
      }

      const url = request.url();
      recordNetworkChange(page, url);
      failedRequests.record({ method: request.method(), url, errorText });

      if (attempted) {
        return;
      }
      attempted = true;
      recovery = recover(page).then((outcome) => {
        didRecover = outcome;
      });
      pendingRecovery.set(page, recovery);
    },
    async settle() {
      if (recovery) {
        await recovery;
      }
    },
    recovered() {
      return didRecover;
    },
    isEmpty() {
      return failedRequests.isEmpty();
    },
    report() {
      return failedRequests.toJSON();
    },
    summary() {
      return (
        `${failedRequests.totalCount()} failed GET(s) across ` +
        `${failedRequests.uniqueCount()} distinct endpoint(s)`
      );
    },
  };
}

/**
 * Await the at-most-one in-flight auto-recovery click for a page. A spec that
 * wants the click to be ordered with respect to its own actions rather than
 * racing them can await this at a point of its choosing.
 */
export function settleNetworkChangeRecovery(page: Page): Promise<void> {
  return pendingRecovery.get(page) ?? Promise.resolve();
}

export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    const recorder = createNetworkChangeRecorder(page);

    page.on("requestfailed", recorder.onRequestFailed);
    await use(page);
    await recorder.settle();
    page.off("requestfailed", recorder.onRequestFailed);
    pendingRecovery.delete(page);

    if (recorder.recovered()) {
      testInfo.annotations.push({
        type: "network-change-recovery",
        description:
          "Clicked a route recovery control after ERR_NETWORK_CHANGED on " +
          recorder.summary(),
      });
    }
    if (!recorder.isEmpty()) {
      // This body is copied verbatim into playwright-report/data/ by the HTML
      // reporter, and both CI workflows upload that directory. It carries no
      // query string, no fragment, no userinfo, no headers and no body.
      await testInfo.attach("network-change-requests", {
        body: Buffer.from(JSON.stringify(recorder.report(), null, 2)),
        contentType: "application/json",
      });
    }
  },
});

/**
 * Record one ERR_NETWORK_CHANGED transport failure against a page. Exported so
 * tests/e2e/network_retry.test.mjs can drive `isTransientNetworkImportError`
 * itself — the function all five recording sites actually call — rather than
 * only the pure classifier underneath it.
 */
export function recordNetworkChange(
  page: Page,
  url: string,
  at: number = Date.now(),
): NetworkChangeLogEntry {
  const entry = { at, url: sanitizeUrl(url) };
  const log = networkChangeLog.get(page) ?? [];
  log.push(entry);
  networkChangeLog.set(
    page,
    log.filter((candidate) => at - candidate.at < NETWORK_CHANGE_WINDOW_MS),
  );
  return entry;
}

/** Drop query and hash: these specs drive links carrying state tokens. */
export function sanitizeUrl(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.origin + parsed.pathname;
  } catch {
    return url.split("?")[0];
  }
}

/**
 * The module URL named by a dynamic-import failure, `""` when the engine's
 * wording carries no URL (Firefox), and `null` when this is not an import
 * failure at all.
 */
export function moduleUrlFromImportError(message: string): string | null {
  if (!message.includes("Failed to fetch dynamically imported module")) {
    return null;
  }
  const match = /Failed to fetch dynamically imported module:?\s*(\S+)/.exec(
    message,
  );
  return match ? sanitizeUrl(match[1]) : "";
}

/**
 * Causation, not correlation. A dynamic-import failure is excused only when the
 * very module URL it names is the URL that lost the network, and only inside
 * the window. Anything else — including an import failure that merely
 * COINCIDES with an unrelated favicon or poll losing the network — is a
 * blocking product error, which is what frontend/src/app/preloadRecovery.ts
 * deliberately escalates after its own single automatic reload.
 *
 * KNOWN RESIDUAL, "Scenario B" -- open, and open deliberately.
 *
 * Causation here is established from the module URL alone, and a URL cannot
 * distinguish "this chunk failed again because the network is still flapping"
 * from "this chunk failed again because it is genuinely missing". One recorded
 * transport failure therefore excuses EVERY import error naming that same URL
 * for the rest of the window, a second one with a real cause included. It
 * matters more than it looks: preloadRecovery.ts prevents the FIRST
 * vite:preloadError and reloads, and lets the second reach the router boundary
 * -- so the second failure of a chunk is precisely the one the application
 * itself has decided is real, and this layer can still swallow it.
 *
 * The obvious closure is to consume the matched entry, so one recorded failure
 * excuses at most one import error. It is a handful of lines, and it was
 * written and mutation-tested against the Scenario B case. It is not taken here
 * because it makes this suppressor strictly stricter, and a suppressor that is
 * too strict does not fail loudly -- it produces a new intermittent RED, which
 * can only be assessed against the real intermittent event over DEVELOPMENT.md's
 * 2-day / 2-machine rule, not against any test this lane can run.
 *
 * Pinned as-is by the "Scenario B" case in tests/e2e/network_retry.test.mjs, so
 * the current behaviour is a decision on the record rather than an oversight,
 * and so closing it later is a visible edit to a failing assertion.
 */
export function classifyPageError(
  message: string,
  log: NetworkChangeLogEntry[],
  now: number,
): PageErrorVerdict {
  const moduleUrl = moduleUrlFromImportError(message);
  if (moduleUrl === null || moduleUrl === "") {
    return { blocking: true, kind: "product-error", cause: null };
  }
  const cause =
    log.find(
      (entry) =>
        now - entry.at < NETWORK_CHANGE_WINDOW_MS && moduleUrl === entry.url,
    ) ?? null;
  return cause
    ? { blocking: false, kind: "recovered-import", cause }
    : { blocking: true, kind: "product-error", cause: null };
}

export function isTransientNetworkImportError(page: Page, message: string) {
  return !classifyPageError(
    message,
    networkChangeLog.get(page) ?? [],
    Date.now(),
  ).blocking;
}

/**
 * Chromium reports every resource that fails with ERR_NETWORK_CHANGED twice:
 * as a `requestfailed` event and again as this console error. The text IS the
 * transport failure, not something that merely coincides with one, so a
 * monitor that already treats the `requestfailed` half as transport must treat
 * this echo the same way. Matched exactly: any other "Failed to load resource"
 * (an HTTP status, a refused connection) and any message that only mentions
 * the code stay blocking.
 */
export function isNetworkChangeConsoleEcho(message: string): boolean {
  return message === "Failed to load resource: net::ERR_NETWORK_CHANGED";
}

async function recoverFromNetworkChange(page: Page) {
  const recoveryControl = page
    .locator(RECOVERY_TEST_IDS.map((id) => `[data-testid="${id}"]`).join(", "))
    .first();
  try {
    // 2s, not 5s. This wait is blind -- nothing has asked for a recovery
    // control to appear -- so its cost is paid on every unrelated blip. A
    // control that only shows up after 3s is better reported as a loud test
    // failure than absorbed by a silent stall.
    await recoveryControl.waitFor({ state: "visible", timeout: 2_000 });
    await recoveryControl.click({ timeout: 2_000, noWaitAfter: true });
    return true;
  } catch {
    // An unrelated GET may fail while the current screen remains usable. In
    // that case there is no visible recovery action and the test continues;
    // its own semantic assertions still decide the outcome.
    return false;
  }
}

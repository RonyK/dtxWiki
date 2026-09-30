import type { Page, Request } from "@playwright/test";

/** method + origin + pathname. Query and hash are deliberately dropped. */
export type RequestIdentity = {
  method: string;
  origin: string;
  pathname: string;
};

type NetworkChangeRetryResult<T> = {
  value: T;
  retried: boolean;
  recovered: RequestIdentity[];
};

/**
 * Retry one browser operation after Chromium reports the host-level
 * ERR_NETWORK_CHANGED transport failure.
 *
 * Three invariants, each pinned by tests/e2e/network_retry.test.mjs:
 *
 *  (a) Only a failure whose method + origin + pathname the CALLER declared in
 *      `expected` licenses a retry. A background poll on another path, or a
 *      request to another origin, does not.
 *  (b) An assertion failure is NEVER retried, even when a matching network
 *      change did occur. A retry must not be able to re-roll a verdict.
 *  (c) The caller must therefore place its semantic assertions OUTSIDE
 *      `operation`; `operation` should contain only the transport work
 *      (navigation, waitForResponse) that a network change can legitimately
 *      destroy. tests/static/test_e2e_evidence_integrity_contract.py enforces
 *      this mechanically.
 *
 * Before this was true, an unrelated ERR_NETWORK_CHANGED plus a genuine
 * `expect(...).toBeVisible()` failure inside `operation` re-ran the whole
 * closure and could report green on a broken route.
 */
export async function retryOnceAfterNetworkChange<T>(
  page: Page,
  expected: RequestIdentity | RequestIdentity[],
  operation: () => Promise<T>,
): Promise<NetworkChangeRetryResult<T>> {
  const declared = Array.isArray(expected) ? expected : [expected];
  let recovered: RequestIdentity[] = [];

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const failures: RequestIdentity[] = [];
    const onRequestFailed = (request: Request) => {
      if (request.failure()?.errorText.includes("ERR_NETWORK_CHANGED")) {
        failures.push(requestIdentity(request.url(), request.method()));
      }
    };
    page.on("requestfailed", onRequestFailed);
    try {
      return { value: await operation(), retried: attempt > 0, recovered };
    } catch (error) {
      if (!shouldRetryAfterNetworkChange({ attempt, error, failures, expected: declared })) {
        throw error;
      }
      recovered = matchedFailures(failures, declared);
    } finally {
      page.off("requestfailed", onRequestFailed);
    }
  }
  throw new Error("unreachable network-change retry state");
}

/**
 * The whole fix, as one pure predicate so it can be driven without a browser.
 */
export function shouldRetryAfterNetworkChange(args: {
  attempt: number;
  error: unknown;
  failures: RequestIdentity[];
  expected: RequestIdentity[];
}): boolean {
  return (
    args.attempt === 0 &&
    !isAssertionFailure(args.error) &&
    matchedFailures(args.failures, args.expected).length > 0
  );
}

export function requestIdentity(url: string, method: string): RequestIdentity {
  const normalizedMethod = method.toUpperCase();
  try {
    const parsed = new URL(url);
    return {
      method: normalizedMethod,
      origin: parsed.origin,
      pathname: parsed.pathname,
    };
  } catch {
    return { method: normalizedMethod, origin: "", pathname: url };
  }
}

export function identityMatches(a: RequestIdentity, b: RequestIdentity): boolean {
  return (
    a.method === b.method &&
    a.origin === b.origin &&
    trimOneTrailingSlash(a.pathname) === trimOneTrailingSlash(b.pathname)
  );
}

/**
 * True for a Playwright assertion rejection. Three independent signals, so the
 * guard survives a Playwright internals change; each is pinned separately by
 * tests/e2e/network_retry.test.mjs.
 *
 * The third exists because `expect.poll` does NOT go through the matcher path:
 * its timeout is a plain `new Error(message)` with no `matcherResult`, and the
 * message Playwright builds for it -- "Timeout 5000ms exceeded while waiting on
 * the predicate" (node_modules/playwright/lib/worker/testInfo.js) -- contains
 * neither "expect(" nor "Timed out ". All of the first two signals miss it, so
 * a poll assertion failure was still being re-rolled: the exact defect this
 * helper exists to prevent, reached by a different door.
 */
export function isAssertionFailure(error: unknown): boolean {
  if (typeof error !== "object" || error === null) {
    return false;
  }
  const candidate = error as { matcherResult?: unknown; message?: unknown };
  if (
    typeof candidate.matcherResult === "object" &&
    candidate.matcherResult !== null
  ) {
    return true;
  }
  const message = String(candidate.message ?? "");
  if (/^expect(\.|\()/.test(message)) {
    return true;
  }
  if (message.includes("Timed out ") && message.includes("expect(")) {
    return true;
  }
  return message.includes("exceeded while waiting on the predicate");
}

function matchedFailures(
  failures: RequestIdentity[],
  expected: RequestIdentity[],
): RequestIdentity[] {
  return failures.filter((failure) =>
    expected.some((candidate) => identityMatches(failure, candidate)),
  );
}

function trimOneTrailingSlash(pathname: string): string {
  return pathname.length > 1 && pathname.endsWith("/")
    ? pathname.slice(0, -1)
    : pathname;
}

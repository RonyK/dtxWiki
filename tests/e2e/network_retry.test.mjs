// E2E-02 regression: the ERR_NETWORK_CHANGED retry must never be able to
// re-roll a semantic verdict, and the dynamic-import suppression must require
// causation rather than coincidence.
//
// Run with `node tests/e2e/network_retry.test.mjs`. Exits non-zero on first
// failure. This file is intentionally a single-file self-runner (no test
// framework), matching tests/e2e/run-e2e.test.mjs: the modules under test are
// test infrastructure, not application code, and pulling a runner in here would
// create a new infrastructure surface that e2e itself depends on.
//
// tests/static/test_e2e_evidence_integrity_contract.py executes this file, so
// it also runs under `make test-static` and `make test`.

import { strict as assert } from "node:assert";

const {
  identityMatches,
  isAssertionFailure,
  requestIdentity,
  retryOnceAfterNetworkChange,
  shouldRetryAfterNetworkChange,
} = await import("./network_retry.ts");

const {
  classifyPageError,
  isTransientNetworkImportError,
  moduleUrlFromImportError,
  recordNetworkChange,
  sanitizeUrl,
} = await import("./fixtures.ts");

// Synthetic origins. These cases never open a socket, and
// tests/static/test_endpoint_port_contract.py forbids a real default
// endpoint anywhere under tests/e2e.
const APP_ORIGIN = "http://app.e2e.invalid";
const OTHER_ORIGIN = "http://other.e2e.invalid";

function fakePage() {
  const listeners = new Map();
  return {
    on(event, handler) {
      if (!listeners.has(event)) {
        listeners.set(event, new Set());
      }
      listeners.get(event).add(handler);
    },
    off(event, handler) {
      listeners.get(event)?.delete(handler);
    },
    listenerCount(event) {
      return listeners.get(event)?.size ?? 0;
    },
    emitRequestFailed(request) {
      for (const handler of [...(listeners.get("requestfailed") ?? [])]) {
        handler(request);
      }
    },
  };
}

function failedRequest(url, method, errorText) {
  return {
    url: () => url,
    method: () => method,
    failure: () => ({ errorText }),
  };
}

function assertionError(message) {
  return Object.assign(new Error(message), {
    matcherResult: { name: "toBeVisible", pass: false },
  });
}

const cases = [
  {
    label:
      "an assertion failure is never retried, even when an unrelated request reports ERR_NETWORK_CHANGED",
    async run() {
      const page = fakePage();
      const expected = requestIdentity(`${APP_ORIGIN}/library`, "GET");
      const thrown = assertionError(
        'expect(locator).toBeVisible() failed\n\nLocator: getByRole("heading", { name: "Library" })',
      );
      let calls = 0;

      await assert.rejects(
        retryOnceAfterNetworkChange(page, expected, async () => {
          calls += 1;
          // A background poll on an unrelated path loses the network while a
          // genuine route regression keeps the heading off the screen.
          page.emitRequestFailed(
            failedRequest(
              `${APP_ORIGIN}/api/v1/activity?cursor=1`,
              "GET",
              "net::ERR_NETWORK_CHANGED",
            ),
          );
          if (calls === 1) {
            throw thrown;
          }
          return "green";
        }),
        (error) => error === thrown,
      );
      assert.equal(calls, 1, "the operation must run exactly once");
      assert.equal(page.listenerCount("requestfailed"), 0);
    },
  },
  {
    label:
      "an assertion failure is still recognised when the network change DOES match the declared navigation",
    async run() {
      const page = fakePage();
      const expected = requestIdentity(`${APP_ORIGIN}/library`, "GET");
      const thrown = assertionError("expect(locator).toBeVisible() failed");
      let calls = 0;

      await assert.rejects(
        retryOnceAfterNetworkChange(page, expected, async () => {
          calls += 1;
          page.emitRequestFailed(
            failedRequest(
              `${APP_ORIGIN}/library`,
              "GET",
              "net::ERR_NETWORK_CHANGED",
            ),
          );
          if (calls === 1) {
            throw thrown;
          }
          return "green";
        }),
        (error) => error === thrown,
      );
      assert.equal(calls, 1);
    },
  },
  {
    label: "isAssertionFailure pins all three signals independently",
    run() {
      const withMatcherResult = assertionError("anything at all");
      assert.equal(isAssertionFailure(withMatcherResult), true);

      const messageOnly = assertionError(
        'expect(locator).toBeVisible() failed\n\nLocator: getByRole("heading")',
      );
      delete messageOnly.matcherResult;
      assert.equal(isAssertionFailure(messageOnly), true);

      const legacyWording = new Error(
        "Timed out 5000ms waiting for expect(locator).toBeVisible()",
      );
      assert.equal(isAssertionFailure(legacyWording), true);

      // expect.poll does not go through the matcher path: Playwright throws a
      // plain Error whose message (built in worker/testInfo.js) contains
      // neither "expect(" nor "Timed out ", so the first two signals miss it
      // entirely. A poll assertion failure was therefore still being re-rolled.
      const pollTimeout = new Error(
        "Timeout 5000ms exceeded while waiting on the predicate",
      );
      assert.equal(pollTimeout.matcherResult, undefined);
      assert.equal(isAssertionFailure(pollTimeout), true);

      assert.equal(
        isAssertionFailure(new Error("page.goto: net::ERR_NETWORK_CHANGED")),
        false,
      );
      assert.equal(isAssertionFailure("expect(x).toBe(y)"), false);
      assert.equal(isAssertionFailure(null), false);
    },
  },
  {
    label:
      "a network change on a different origin or pathname does not license retrying this navigation",
    async run() {
      const expected = requestIdentity(`${APP_ORIGIN}/library`, "GET");
      const unrelated = [
        `${OTHER_ORIGIN}/api/v1/notifications`,
        `${APP_ORIGIN}/context`,
      ];

      for (const url of unrelated) {
        const page = fakePage();
        const thrown = new Error("page.goto: Timeout 30000ms exceeded");
        let calls = 0;
        await assert.rejects(
          retryOnceAfterNetworkChange(page, expected, async () => {
            calls += 1;
            page.emitRequestFailed(
              failedRequest(url, "GET", "net::ERR_NETWORK_CHANGED"),
            );
            throw thrown;
          }),
          (error) => error === thrown,
          `${url} must not license a retry`,
        );
        assert.equal(calls, 1, `${url} must not license a retry`);

        assert.equal(
          shouldRetryAfterNetworkChange({
            attempt: 0,
            error: new Error("boom"),
            failures: [requestIdentity(url, "GET")],
            expected: [expected],
          }),
          false,
        );
      }

      // Same url, different method is also a different identity.
      assert.equal(
        shouldRetryAfterNetworkChange({
          attempt: 0,
          error: new Error("boom"),
          failures: [requestIdentity(`${APP_ORIGIN}/library`, "POST")],
          expected: [expected],
        }),
        false,
      );
      // And a second attempt never retries again.
      assert.equal(
        shouldRetryAfterNetworkChange({
          attempt: 1,
          error: new Error("boom"),
          failures: [requestIdentity(`${APP_ORIGIN}/library`, "GET")],
          expected: [expected],
        }),
        false,
      );
    },
  },
  {
    label:
      "the declared navigation is still retried once, ignoring query string and hash",
    async run() {
      const page = fakePage();
      const expected = requestIdentity(`${APP_ORIGIN}/library`, "GET");
      let calls = 0;

      const result = await retryOnceAfterNetworkChange(
        page,
        expected,
        async () => {
          calls += 1;
          if (calls === 1) {
            page.emitRequestFailed(
              failedRequest(
                `${APP_ORIGIN}/library?tab=all#top`,
                "GET",
                "net::ERR_NETWORK_CHANGED",
              ),
            );
            throw new Error("page.goto: net::ERR_NETWORK_CHANGED");
          }
          return "ok";
        },
      );

      assert.equal(result.retried, true);
      assert.equal(result.value, "ok");
      assert.equal(calls, 2);
      assert.deepEqual(result.recovered, [
        { method: "GET", origin: APP_ORIGIN, pathname: "/library" },
      ]);
      assert.equal(
        page.listenerCount("requestfailed"),
        0,
        "both attempts must detach their handler",
      );
    },
  },
  {
    label: "a caller may declare several identities, and the first attempt wins",
    async run() {
      const page = fakePage();
      const declared = [
        requestIdentity(`${APP_ORIGIN}/context`, "GET"),
        requestIdentity(`${APP_ORIGIN}/api/v1/workspaces/w1/context-pages`, "GET"),
      ];
      let calls = 0;

      const result = await retryOnceAfterNetworkChange(page, declared, async () => {
        calls += 1;
        if (calls === 1) {
          page.emitRequestFailed(
            failedRequest(
              `${APP_ORIGIN}/api/v1/workspaces/w1/context-pages`,
              "GET",
              "net::ERR_NETWORK_CHANGED",
            ),
          );
          throw new Error("waitForResponse: Timeout 15000ms exceeded");
        }
        return "second";
      });

      assert.equal(result.value, "second");
      assert.equal(result.retried, true);
      assert.deepEqual(result.recovered, [
        {
          method: "GET",
          origin: APP_ORIGIN,
          pathname: "/api/v1/workspaces/w1/context-pages",
        },
      ]);
      assert.equal(page.listenerCount("requestfailed"), 0);
    },
  },
  {
    label: "requestIdentity and identityMatches drop query, hash and one trailing slash",
    run() {
      assert.deepEqual(requestIdentity(`${APP_ORIGIN}/library?tab=all#top`, "get"), {
        method: "GET",
        origin: APP_ORIGIN,
        pathname: "/library",
      });
      assert.deepEqual(requestIdentity("not a url", "get"), {
        method: "GET",
        origin: "",
        pathname: "not a url",
      });
      assert.equal(
        identityMatches(
          { method: "GET", origin: APP_ORIGIN, pathname: "/library/" },
          { method: "GET", origin: APP_ORIGIN, pathname: "/library" },
        ),
        true,
      );
      assert.equal(
        identityMatches(
          { method: "GET", origin: APP_ORIGIN, pathname: "/library" },
          { method: "GET", origin: OTHER_ORIGIN, pathname: "/library" },
        ),
        false,
      );
    },
  },
  {
    label:
      "a dynamic-import failure is suppressed only when that exact module URL is the one that lost the network",
    run() {
      const now = 1_000_000;
      const cause = {
        at: now - 1_000,
        url: `${APP_ORIGIN}/assets/Activity-abc123.js`,
      };
      const log = [cause];

      const differentModule = classifyPageError(
        `Failed to fetch dynamically imported module: ${APP_ORIGIN}/assets/Library-def456.js`,
        log,
        now,
      );
      assert.deepEqual(differentModule, {
        blocking: true,
        kind: "product-error",
        cause: null,
      });

      const sameModule = classifyPageError(
        `Failed to fetch dynamically imported module: ${APP_ORIGIN}/assets/Activity-abc123.js`,
        log,
        now,
      );
      assert.deepEqual(sameModule, {
        blocking: false,
        kind: "recovered-import",
        cause,
      });

      // Firefox wording carries no URL, so causation can never be established.
      const noUrl = classifyPageError(
        "Failed to fetch dynamically imported module",
        log,
        now,
      );
      assert.deepEqual(noUrl, {
        blocking: true,
        kind: "product-error",
        cause: null,
      });

      // The window survives only as an upper bound.
      const stale = classifyPageError(
        `Failed to fetch dynamically imported module: ${APP_ORIGIN}/assets/Activity-abc123.js`,
        log,
        now + 11_000,
      );
      assert.equal(stale.blocking, true);

      // A plain product error is never an import error.
      assert.deepEqual(
        classifyPageError("ReferenceError: x is not defined", log, now),
        { blocking: true, kind: "product-error", cause: null },
      );
      assert.equal(
        moduleUrlFromImportError("ReferenceError: x is not defined"),
        null,
      );
      assert.equal(
        moduleUrlFromImportError("Failed to fetch dynamically imported module"),
        "",
      );
    },
  },
  {
    label:
      "KNOWN RESIDUAL (Scenario B): a genuine second failure of the same chunk inside the window is still suppressed",
    run() {
      // This case pins a HOLE, not a fix, and it is here because the hole was
      // written down nowhere. `classifyPageError` establishes causation from
      // the module URL alone, so it cannot tell a chunk that is still flapping
      // from a chunk that is genuinely gone: the transport failure records the
      // chunk's OWN url, and every later import error naming that url is
      // excused for the rest of the window.
      //
      // frontend/src/app/preloadRecovery.ts prevents the first
      // vite:preloadError and reloads, and lets the second reach the router
      // boundary -- so the failure this suppresses is exactly the one the
      // application itself has decided is real.
      //
      // Closing it (consume the matched entry, so one recorded transport
      // failure excuses at most one import error) MUST break the second half of
      // this case. That is what it is for. See the residual note on
      // classifyPageError in tests/e2e/fixtures.ts for why it was left open.
      const now = 1_000_000;
      const chunk = `${APP_ORIGIN}/assets/Chats-abc.js`;
      const cause = { at: now - 1_000, url: chunk };
      const message = `Failed to fetch dynamically imported module: ${chunk}`;

      assert.deepEqual(classifyPageError(message, [cause], now), {
        blocking: false,
        kind: "recovered-import",
        cause,
      });
      assert.deepEqual(
        classifyPageError(message, [cause], now + 1_000),
        { blocking: false, kind: "recovered-import", cause },
        "one recorded failure currently excuses EVERY import error for that " +
          "url inside the 10s window, not just the first",
      );

      // The same through the entry point the five recording sites call.
      const page = fakePage();
      recordNetworkChange(page, chunk);
      assert.equal(isTransientNetworkImportError(page, message), true);
      assert.equal(
        isTransientNetworkImportError(page, message),
        true,
        "Scenario B: the second failure of the same chunk is suppressed too",
      );
    },
  },
  {
    label:
      "isTransientNetworkImportError itself — the entry point all five recording sites call — requires causation",
    run() {
      const unrelatedPage = fakePage();
      recordNetworkChange(
        unrelatedPage,
        `${APP_ORIGIN}/api/v1/activity?cursor=1`,
      );
      assert.equal(
        isTransientNetworkImportError(
          unrelatedPage,
          `Failed to fetch dynamically imported module: ${APP_ORIGIN}/assets/Chats-9f3a1c.js`,
        ),
        false,
        "an unrelated poll losing the network must not excuse a broken chunk",
      );

      const causedPage = fakePage();
      recordNetworkChange(causedPage, `${APP_ORIGIN}/assets/Chats-9f3a1c.js`);
      assert.equal(
        isTransientNetworkImportError(
          causedPage,
          `Failed to fetch dynamically imported module: ${APP_ORIGIN}/assets/Chats-9f3a1c.js`,
        ),
        true,
      );

      // A page that never lost the network suppresses nothing.
      assert.equal(
        isTransientNetworkImportError(
          fakePage(),
          `Failed to fetch dynamically imported module: ${APP_ORIGIN}/assets/Chats-9f3a1c.js`,
        ),
        false,
      );
    },
  },
  {
    label: "recorded network-change evidence is sanitized of query strings",
    run() {
      assert.equal(
        sanitizeUrl(`${APP_ORIGIN}/reset-password?state=super-secret-token`),
        `${APP_ORIGIN}/reset-password`,
      );
      assert.equal(sanitizeUrl("not a url?state=secret"), "not a url");

      const page = fakePage();
      const entry = recordNetworkChange(
        page,
        `${APP_ORIGIN}/reset-password?state=super-secret-token`,
      );
      assert.equal(entry.url, `${APP_ORIGIN}/reset-password`);
      assert.equal(
        JSON.stringify(entry).includes("super-secret-token"),
        false,
        "attached evidence must not carry a state token",
      );
    },
  },
];

let failed = 0;
for (const testCase of cases) {
  try {
    await testCase.run();
    console.log(`ok - ${testCase.label}`);
  } catch (error) {
    failed += 1;
    console.error(`FAIL - ${testCase.label}`);
    console.error(error?.stack ?? error);
  }
}

if (failed > 0) {
  console.error(`${failed} of ${cases.length} network-retry cases failed`);
  process.exit(1);
}
console.log(`${cases.length} network-retry cases passed`);

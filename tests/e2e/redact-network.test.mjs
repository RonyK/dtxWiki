// E2E-03 regression: a failed request must not be able to carry a secret, or
// unbounded volume, into a Playwright artifact -- and the fixture's implicit
// recovery click must fire at most once per test.
//
// Run with `node tests/e2e/redact-network.test.mjs`. Exits non-zero on first
// failure. Single-file self-runner (no test framework), matching
// tests/e2e/network_retry.test.mjs and tests/e2e/run-e2e.test.mjs: the modules
// under test are test infrastructure, not application code.
//
// tests/static/test_e2e_artifact_redaction_contract.py executes this file, so
// it also runs under `make test-static` and `make test`.

import { strict as assert } from "node:assert";

const {
  createFailedRequestLog,
  CREDENTIAL_PREFIXES,
  MAX_FIELD_CHARS,
  MAX_UNIQUE_RECORDS,
  redactFailedRequest,
} = await import("./redact-network.ts");

const {
  createNetworkChangeRecorder,
  RECOVERY_TEST_IDS,
  settleNetworkChangeRecovery,
} = await import("./fixtures.ts");

// Synthetic origins. These cases never open a socket, and
// tests/static/test_endpoint_port_contract.py forbids a real default endpoint
// anywhere under tests/e2e.
const APP_ORIGIN = "http://app.e2e.invalid";
const RESET_TOKEN = "SUPERSECRET-RESET-TOKEN";

function failedRequest(url, method = "GET", errorText = "net::ERR_NETWORK_CHANGED") {
  return {
    url: () => url,
    method: () => method,
    failure: () => ({ errorText }),
  };
}

function recorderWithSpyRecovery(outcome = true) {
  const calls = [];
  const page = {};
  const recorder = createNetworkChangeRecorder(page, async (target) => {
    calls.push(target);
    return outcome;
  });
  return { calls, page, recorder };
}

const cases = [
  {
    label:
      "redactFailedRequest strips the query and fragment from a reset-password URL",
    run() {
      const out = redactFailedRequest({
        method: "GET",
        url: `${APP_ORIGIN}/reset-password?token=${RESET_TOKEN}#frag`,
        errorText: "net::ERR_NETWORK_CHANGED",
      });
      assert.deepEqual(out, {
        method: "GET",
        origin: APP_ORIGIN,
        pathname: "/reset-password",
        errorText: "net::ERR_NETWORK_CHANGED",
      });
      assert.equal(
        JSON.stringify(out).includes(RESET_TOKEN),
        false,
        "the one-time reset token must not survive redaction",
      );
    },
  },
  {
    // M-2: recovery links carry the token in the fragment now. A fragment is
    // never sent to the server, but Playwright can still report it as part of
    // a request URL, so the record must drop it like the query.
    label:
      "redactFailedRequest strips a #token= fragment from a recovery link",
    run() {
      for (const path of ["/reset-password", "/verify-email"]) {
        const out = redactFailedRequest({
          method: "GET",
          url: `${APP_ORIGIN}${path}#token=${RESET_TOKEN}`,
          errorText: "net::ERR_NETWORK_CHANGED",
        });
        assert.deepEqual(out, {
          method: "GET",
          origin: APP_ORIGIN,
          pathname: path,
          errorText: "net::ERR_NETWORK_CHANGED",
        });
        assert.equal(
          JSON.stringify(out).includes(RESET_TOKEN),
          false,
          "a fragment token must not survive redaction",
        );
      }
    },
  },
  {
    label:
      "redactFailedRequest drops userinfo and degrades on an unparsable URL instead of throwing",
    run() {
      assert.equal(
        redactFailedRequest({ method: "GET", url: "http://u:p@app.e2e.invalid/x?y=1" })
          .origin,
        APP_ORIGIN,
        "basic-auth credentials in the URL must not survive",
      );
      assert.deepEqual(
        redactFailedRequest({ url: `/relative?token=${RESET_TOKEN}` }),
        {
          method: "UNKNOWN",
          origin: "unparsable",
          pathname: "/",
          errorText: "REDACTED",
        },
      );
      // `data:` and `blob:` parse, and their `pathname` is the payload rather
      // than a path -- so a scheme allowlist, not `new URL` alone, is what
      // makes the redaction structural.
      assert.deepEqual(
        redactFailedRequest({
          method: "GET",
          url: `data:text/html,${RESET_TOKEN}`,
        }),
        {
          method: "GET",
          origin: "data:",
          pathname: "/",
          errorText: "REDACTED",
        },
      );
      assert.equal(
        redactFailedRequest({
          method: "GET",
          url: `blob:${APP_ORIGIN}/${RESET_TOKEN}`,
        }).pathname,
        "/",
      );
    },
  },
  {
    label:
      "a hostile errorText that interpolates the failing URL is replaced wholesale",
    run() {
      const out = redactFailedRequest({
        method: "GET",
        url: `${APP_ORIGIN}/reset-password`,
        errorText: `failed loading ${APP_ORIGIN}/reset-password?token=${RESET_TOKEN}`,
      });
      assert.equal(out.errorText, "REDACTED");
      assert.equal(JSON.stringify(out).includes(RESET_TOKEN), false);
    },
  },
  {
    label: "createFailedRequestLog collapses repeats of one endpoint into a count",
    run() {
      const log = createFailedRequestLog();
      for (const cursor of [1, 2, 3]) {
        log.record({
          method: "GET",
          url: `${APP_ORIGIN}/api/v1/activity?cursor=${cursor}`,
          errorText: "net::ERR_NETWORK_CHANGED",
        });
      }
      const report = log.toJSON();
      assert.equal(report.requests.length, 1);
      assert.equal(report.requests[0].count, 3);
      assert.equal(report.requests[0].pathname, "/api/v1/activity");
      assert.equal(report.dropped_unique_requests, 0);
      assert.equal(log.totalCount(), 3);
      assert.equal(log.uniqueCount(), 1);
    },
  },
  {
    label:
      "createFailedRequestLog caps distinct endpoints at 20 and says how many it dropped",
    run() {
      // Every number below is a literal on purpose. This case used to loop
      // `MAX_UNIQUE_RECORDS + 5` and assert `=== MAX_UNIQUE_RECORDS`, which
      // holds for ANY cap: raising the constant 100x left this file and
      // tests/static both green. A case that reads the constant under test
      // does not test the constant.
      assert.equal(
        MAX_UNIQUE_RECORDS,
        20,
        "the cap E2E-03 specified; changing it is a deliberate act, not a " +
          "silent one -- an attachment is published for 14 days",
      );

      const log = createFailedRequestLog();
      for (let index = 0; index < 25; index += 1) {
        log.record({
          method: "GET",
          url: `${APP_ORIGIN}/p${index}`,
          errorText: "net::ERR_NETWORK_CHANGED",
        });
      }
      const report = log.toJSON();
      assert.equal(report.requests.length, 20);
      assert.equal(report.dropped_unique_requests, 5);
      assert.equal(log.totalCount(), 25);
      assert.equal(log.uniqueCount(), 20);
    },
  },
  {
    label:
      "one record cannot grow without bound either: every retained field is clamped to 120",
    run() {
      assert.equal(
        MAX_FIELD_CHARS,
        120,
        "the per-field bound is pinned by a literal, for the same reason " +
          "the record cap is",
      );

      // The reproduction from the review: one record, a 2 MB pathname, a
      // 2 MB attachment. The tuple-count cap did not touch it.
      const huge = "z".repeat(2_000_000);
      const out = redactFailedRequest({
        method: huge,
        url: `${APP_ORIGIN}/${huge}`,
        errorText: "net::ERR_NETWORK_CHANGED",
      });
      assert.equal(out.pathname.length, 120);
      assert.ok(
        out.pathname.endsWith("..."),
        "a truncated path must say so, or it reads as the real one",
      );
      assert.equal(out.method, "UNKNOWN", "a 2 MB `method` is not a method");

      // `origin` is caller-controlled too -- a long host, not just a long path.
      assert.equal(
        redactFailedRequest({
          method: "GET",
          url: `http://${"h".repeat(400)}.e2e.invalid/x`,
        }).origin.length,
        120,
      );

      // What actually matters is the size of the emitted attachment, so bound
      // that, with 25 maximally long DISTINCT records.
      const log = createFailedRequestLog();
      for (let index = 0; index < 25; index += 1) {
        log.record({
          method: "GET",
          url: `http://${"h".repeat(400)}.e2e.invalid/${index}/${huge}`,
          errorText: "net::ERR_NETWORK_CHANGED",
        });
      }
      assert.equal(log.toJSON().requests.length, 20);
      const bytes = Buffer.byteLength(
        JSON.stringify(log.toJSON(), null, 2),
        "utf8",
      );
      assert.ok(
        bytes < 10_000,
        `the worst attachment this log can emit was ${bytes} bytes`,
      );
    },
  },
  {
    label:
      "a credential this app mints is stripped from a PATH segment, not only from the query",
    run() {
      assert.deepEqual(
        [...CREDENTIAL_PREFIXES],
        ["dtxw_", "dtxr_", "dtxe_", "rt_"],
        "this list is the code-side half of the pathname precondition; " +
          "emptying or shrinking it must not be silent",
      );

      // The reviewer's live proof: `/mcp/session/<PAT>` was emitted verbatim
      // into the report, which CI publishes for 14 days.
      const PAT = "dtxw_E2E_ARTIFACT_PROBE_PAT";
      const out = redactFailedRequest({
        method: "GET",
        url: `${APP_ORIGIN}/mcp/session/${PAT}`,
        errorText: "net::ERR_NETWORK_CHANGED",
      });
      assert.equal(out.pathname, "/mcp/session/REDACTED");
      assert.equal(JSON.stringify(out).includes(PAT), false);

      for (const prefix of CREDENTIAL_PREFIXES) {
        assert.equal(
          redactFailedRequest({
            method: "GET",
            url: `${APP_ORIGIN}/x/${prefix}abc123`,
          }).pathname,
          "/x/REDACTED",
          `a ${prefix} credential must not reach an artifact`,
        );
      }

      // A credential longer than the 120-character field bound is REPLACED,
      // not clamped into a partial token. What is load-bearing
      // is that the scrub sees the whole segment; the scrub/clamp ORDER is not,
      // and was mutation-tested to confirm that.
      assert.equal(
        redactFailedRequest({
          method: "GET",
          url: `${APP_ORIGIN}/mcp/session/dtxw_${"s".repeat(2_000)}`,
        }).pathname,
        "/mcp/session/REDACTED",
      );

      // It is a net, not a boundary, and it must not eat ordinary routes: a
      // word that merely CONTAINS `rt_` (repo-rt_-...) is not a credential.
      assert.equal(
        redactFailedRequest({
          method: "GET",
          url: `${APP_ORIGIN}/reports/quarterly`,
        }).pathname,
        "/reports/quarterly",
      );
      assert.equal(
        redactFailedRequest({
          method: "GET",
          url: `${APP_ORIGIN}/api/v1/auth/password-reset/request`,
        }).pathname,
        "/api/v1/auth/password-reset/request",
      );
    },
  },
  {
    label:
      "the fixture recorder's own attachment body carries no reset token and stays bounded",
    async run() {
      const { recorder } = recorderWithSpyRecovery(false);
      recorder.onRequestFailed(
        failedRequest(`${APP_ORIGIN}/reset-password?token=${RESET_TOKEN}`),
      );
      for (let index = 0; index < 1000; index += 1) {
        recorder.onRequestFailed(
          failedRequest(`${APP_ORIGIN}/api/v1/activity?cursor=${index}`),
        );
      }
      await recorder.settle();

      const body = JSON.stringify(recorder.report(), null, 2);
      assert.equal(
        body.includes(RESET_TOKEN),
        false,
        "the attachment reaches playwright-report/data/, which CI uploads",
      );
      assert.equal(recorder.report().requests.length, 2);
      assert.equal(recorder.report().requests[1].count, 1000);
      assert.ok(
        body.length < 1_000,
        `1001 failures must not produce a ${body.length}-byte attachment`,
      );
      assert.equal(recorder.isEmpty(), false);
      assert.match(recorder.summary(), /1001 failed GET\(s\) across 2 /);
    },
  },
  {
    label: "the fixture recorder ignores a non-GET and a non-network-change failure",
    async run() {
      const { recorder, calls } = recorderWithSpyRecovery();
      recorder.onRequestFailed(
        failedRequest(`${APP_ORIGIN}/api/v1/pages`, "POST"),
      );
      recorder.onRequestFailed(
        failedRequest(`${APP_ORIGIN}/api/v1/pages`, "GET", "net::ERR_ABORTED"),
      );
      await recorder.settle();
      assert.equal(recorder.isEmpty(), true);
      assert.deepEqual(calls, []);
      assert.equal(recorder.recovered(), false);
    },
  },
  {
    label:
      "the recovery click fires at most once per test, however many GETs lose the network",
    async run() {
      const { recorder, calls, page } = recorderWithSpyRecovery(true);
      for (let index = 0; index < 20; index += 1) {
        recorder.onRequestFailed(
          failedRequest(`${APP_ORIGIN}/api/v1/activity?cursor=${index}`),
        );
      }
      await recorder.settle();
      assert.deepEqual(
        calls,
        [page],
        "a second blind click is the one that lands on a form the test filled",
      );
      assert.equal(recorder.recovered(), true);
    },
  },
  {
    label:
      "a failed recovery attempt is not retried by the next failure either",
    async run() {
      const { recorder, calls } = recorderWithSpyRecovery(false);
      recorder.onRequestFailed(failedRequest(`${APP_ORIGIN}/a`));
      await recorder.settle();
      recorder.onRequestFailed(failedRequest(`${APP_ORIGIN}/b`));
      await recorder.settle();
      assert.equal(
        calls.length,
        1,
        "the shipped version reset its marker in the .then, so every later " +
          "failure paid a fresh blind wait and a fresh click",
      );
      assert.equal(recorder.recovered(), false);
    },
  },
  {
    label:
      "settleNetworkChangeRecovery lets a spec order the implicit click against its own actions",
    async run() {
      const calls = [];
      const page = {};
      let release;
      const recovery = new Promise((resolve) => {
        release = resolve;
      });
      const recorder = createNetworkChangeRecorder(page, async (target) => {
        calls.push(target);
        await recovery;
        return true;
      });

      // Nothing in flight: settling is a no-op, not a hang.
      await settleNetworkChangeRecovery(page);
      assert.deepEqual(calls, []);

      recorder.onRequestFailed(failedRequest(`${APP_ORIGIN}/a`));
      let settled = false;
      const waiter = settleNetworkChangeRecovery(page).then(() => {
        settled = true;
      });
      await Promise.resolve();
      assert.equal(settled, false, "must still be waiting on the click");
      release();
      await waiter;
      assert.equal(settled, true);
      assert.equal(recorder.recovered(), true);
    },
  },
  {
    label:
      "the recovery selector addresses stable test ids, not presentation classes",
    run() {
      assert.ok(RECOVERY_TEST_IDS.length >= 4);
      assert.deepEqual(
        [...RECOVERY_TEST_IDS].sort(),
        [
          "route-error-reload",
          "session-verify-retry",
          "workspace-access-retry",
          "workspace-resolution-retry",
        ],
      );
      const selector = RECOVERY_TEST_IDS.map(
        (id) => `[data-testid="${id}"]`,
      ).join(", ");
      assert.equal(selector.includes(".primary"), false);
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
  console.error(`${failed} of ${cases.length} artifact-redaction cases failed`);
  process.exit(1);
}
console.log(`${cases.length} artifact-redaction cases passed`);

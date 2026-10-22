import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const isWindows = process.platform === "win32";
const pythonExecutable = path.join(
  ".venv",
  isWindows ? "Scripts" : "bin",
  isWindows ? "python.exe" : "python",
);

const backendPort = Number.parseInt(
  process.env.DTXWIKI_BACKEND_PORT ?? "21080",
  10,
);
// Dead in this config, and deliberately so: E2E serves the built SPA from the
// backend the way the packaged wheel does, so no browser here ever reaches a
// Vite dev server. Nothing else in this file reads the constant --
// frontend/vite.config.ts reads DTXWIKI_FRONTEND_DEV_PORT from the environment
// itself, and tests/e2e/single_origin.spec.ts gets the dev origin from
// tests/e2e/auth.ts. It survives only because
// tests/static/test_endpoint_port_contract.py
// (test_frontend_and_playwright_configs_use_sprint3_ports) asserts this exact
// literal is present in this file, while a second assertion there requires the
// constant never to be interpolated into any value. Deleting it fails that
// static test; change the two together or not at all.
const frontendDevPort = Number.parseInt(
  process.env.DTXWIKI_FRONTEND_DEV_PORT ?? "21173",
  10,
);
const backendBaseUrl = `http://127.0.0.1:${backendPort}`;
const identityRecoveryEnv = {
  // The E2E server deliberately advertises a canonical HTTPS origin even
  // though the browser talks to the local API port. Recovery links must
  // be derived from configured state, never from the request Host header.
  DTXWIKI_PUBLIC_BASE_URL: "https://e2e.dtxwiki.invalid",
  DTXWIKI_SESSION_COOKIE_SECURE: "false",
  DTXWIKI_SIGNUP_ENABLED: "true",
  DTXWIKI_MAIL_PROVIDER: "smtp",
  DTXWIKI_MAIL_FROM_ADDRESS: "no-reply@e2e.dtxwiki.invalid",
  DTXWIKI_SMTP_HOST: "127.0.0.1",
  DTXWIKI_SMTP_PORT: "1",
  DTXWIKI_SMTP_USERNAME: "e2e-mail-user",
  DTXWIKI_SMTP_PASSWORD: "e2e-only-smtp-secret",
  DTXWIKI_SMTP_STARTTLS: "true",
  DTXWIKI_SMTP_USE_SSL: "false",
  DTXWIKI_SMTP_TIMEOUT_SECONDS: "0.2",
};
const backendServerScript = [
  "import uvicorn",
  "from backend.app.main import create_app",
  // Explicit response closure prevents Playwright's HTTP agent from reusing
  // an idle socket whose peer FIN has not reached the client's event loop.
  `uvicorn.run(create_app(bootstrap_on_startup=True), host="127.0.0.1", port=${backendPort}, headers=[("Connection", "close")])`,
].join("; ");
// Single-origin E2E never issues a cross-origin request, so this list only has
// to stay non-empty: Settings rejects an empty DTXWIKI_CORS_ORIGINS at startup.
const corsOrigins =
  process.env.DTXWIKI_CORS_ORIGINS ??
  [
    `http://127.0.0.1:${backendPort}`,
    `http://localhost:${backendPort}`,
  ].join(",");
const e2eDatabaseUrl = (() => {
  const url = process.env.DTXWIKI_E2E_DATABASE_URL;
  if (!url) {
    throw new Error(
      "DTXWIKI_E2E_DATABASE_URL must be set by tests/e2e/run-e2e.mjs. " +
        "Run `npm run test:e2e`, do not invoke `playwright test` directly.",
    );
  }
  return url;
})();
const e2eLibraryBlobRoot = (() => {
  const root = process.env.DTXWIKI_E2E_LIBRARY_BLOB_ROOT;
  if (!root) {
    throw new Error(
      "DTXWIKI_E2E_LIBRARY_BLOB_ROOT must be set by tests/e2e/run-e2e.mjs. " +
        "Run `npm run test:e2e`, do not invoke `playwright test` directly.",
    );
  }
  return root;
})();
const e2eStaticRoot = (() => {
  const root = process.env.DTXWIKI_E2E_STATIC_ROOT;
  if (!root) {
    throw new Error(
      "DTXWIKI_E2E_STATIC_ROOT must be set by tests/e2e/run-e2e.mjs. " +
        "Run `npm run test:e2e`, do not invoke `playwright test` directly.",
    );
  }
  return root;
})();
const inheritedEnv = envWithoutUndefined(process.env);
// Set by tests/e2e/run-e2e.mjs, the only supported entry point.
const e2eProfile = process.env.DTXWIKI_E2E_PROFILE ?? "standard";
const crossBrowserGrep =
  e2eProfile === "exhaustive" ? undefined : /@cross-browser/;

function envWithoutUndefined(env: NodeJS.ProcessEnv): Record<string, string> {
  return Object.fromEntries(
    Object.entries(env).filter((entry): entry is [string, string] => {
      return typeof entry[1] === "string";
    }),
  );
}

function quoted(commandPath: string) {
  return JSON.stringify(commandPath);
}

export default defineConfig({
  testDir: "tests/e2e",
  // Collect only committed Playwright specs. The default testMatch would
  // also pick up run-e2e.test.mjs (a framework-less node self-runner whose
  // top-level assertions call process.exit on failure — run it with
  // `node tests/e2e/run-e2e.test.mjs`) and developer-local _*.spec.ts
  // scratch files.
  testMatch: "**/*.spec.ts",
  testIgnore: "**/_*.spec.ts",
  timeout: 30_000,
  retries: 0,
  captureGitInfo: { diff: false },
  reporter: process.env.CI
    ? [["dot"], ["html", { open: "never", outputFolder: "playwright-report" }]]
    : [["list"]],
  // Every browser project and the live background workers share the wrapper's
  // one SQLite database. Playwright otherwise derives a high worker count from
  // the host CPU count, which turns valid concurrent writes into lock cascades.
  workers: 1,
  use: {
    // Fail at the operation that stalled, before a multi-step acceptance test
    // exhausts its whole budget. Response-event waits inherit actionTimeout.
    actionTimeout: 15_000,
    navigationTimeout: 20_000,
    // One origin for the document, the bundle and the API — the shape the wheel
    // ships. A Vite dev server on a second port gave every page two Chromium
    // socket pools, so a host network-change notification flushed both and the
    // suite reported ERR_NETWORK_CHANGED transport flakes as product failures.
    // Collapsing to one socket pool was expected to remove that class of flake.
    //
    // E2E-02 collected the evidence, and it did NOT. Four full standard-profile
    // runs on 2026-09-03 under this exact topology: two clean (124/124) and two
    // hit by a host network-change storm, one of them 20 ERR_NETWORK_CHANGED
    // lines deep, failing ui_fix_acceptance.spec.ts on nothing but transport.
    // One socket pool is fewer sockets to flush, not none. So this stays.
    //
    // The evidence that would license deleting tests/e2e/network_retry.ts, its
    // two call sites and the recovery layer in tests/e2e/fixtures.ts is spelled
    // out in DEVELOPMENT.md. Read it before deleting anything: the obvious
    // criterion needs BOTH halves, and the obvious spellings of each are
    // broken. `find test-results -name '*network-change*'` cannot return a
    // non-zero count in ANY case -- the annotation lives only in the
    // reporter's JSON and both attachments use `body:`, so neither becomes a
    // file. And a log grep for ERR_NETWORK_CHANGED only ever fired on runs
    // where a test had already FAILED (measured over six runs), because the
    // local reporter is [["list"]] and console text reaches the log through a
    // failing test's printed error -- so a flake this layer ABSORBED, which is
    // the case that decides whether the layer is still needed, leaves the grep
    // at zero. The working criterion runs with `--reporter=list,json` and
    // reads the network-change annotation/attachment out of that JSON.
    baseURL: backendBaseUrl,
    screenshot: "only-on-failure",
    // E2E-03: a trace records every request URL and POST body verbatim and
    // Playwright offers no redaction hook, so a trace of a failing
    // reset-password test contains the one-time token and the new password
    // (proved by driving context.tracing directly and grepping trace.trace and
    // trace.network). The HTML reporter copies traces and attachment bodies
    // into playwright-report/data/, and both e2e jobs -- ci.yml's `e2e` and
    // e2e-browsers.yml's exhaustive matrix -- upload that report `if: always()`
    // for 14 days. (Named by job rather than by line: the line numbers this
    // comment first carried were already stale one commit later.) Each job
    // scrubs the report with scripts/redact_evidence.sh --dir first;
    // tests/static/test_ci_e2e_artifact_publication_contract.py pins that the
    // scrub covers the exact path the upload publishes, and runs before it.
    //
    // FAILURE-GATING DOES NOT BOUND THAT EXPOSURE, and an earlier version of
    // this comment claimed it did. The HTML reporter base64-inlines every step
    // TITLE into playwright-report/index.html on EVERY run, passing runs
    // included, and a `page.goto("/reset-password?token=...")` title carries
    // the token verbatim. Reproduced in a standalone Playwright 1.59.1 project
    // with retries:0, trace:"off" and screenshot:"off" on a test that PASSES:
    // the token was still in index.html. So this knob is a debugging-cost
    // choice, not a security control -- "on"/"on-all-retries" would publish a
    // trace for all 124 tests rather than only the failed ones, which is why
    // tests/static/test_e2e_artifact_redaction_contract.py still rejects them.
    //
    // The exposure that remains is bounded by ephemerality, not by this
    // setting: tests/e2e/run-e2e.mjs provisions the run's SQLite database in a
    // mkdtemp directory destroyed when the run ends, and no spec sources a
    // credential from the environment, so a published token authenticates
    // against a database that no longer exists. Treat it as a hygiene and
    // contract defect. The only mitigation that reaches index.html is a
    // redaction step before upload-artifact in the e2e job -- see
    // docs/reviews/phase5/FINAL-REVIEW.md S1.
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    // Firefox and WebKit re-ran every spec, tripling a 78-test suite to prove
    // the same application logic three times. Nothing under tests/e2e reads
    // `browserName`, so the engine was never the subject. The exceptions are
    // the specs whose contract IS engine behavior -- CSS cascade and layout,
    // scroll synchronisation, CodeMirror key handling, file choosers, OAuth
    // popups -- and those carry a @cross-browser tag and still run everywhere.
    // DTXWIKI_E2E_PROFILE=exhaustive drops the filter and runs the whole suite
    // on all three, which is what the weekly workflow does.
    {
      name: "firefox",
      use: { ...devices["Desktop Firefox"] },
      grep: crossBrowserGrep,
    },
    {
      name: "webkit",
      use: { ...devices["Desktop Safari"] },
      grep: crossBrowserGrep,
    },
  ],
  webServer: [
    {
      name: "backend",
      command: `${quoted(pythonExecutable)} -c ${quoted(backendServerScript)}`,
      env: {
        ...inheritedEnv,
        ...identityRecoveryEnv,
        DTXWIKI_BACKEND_PORT: String(backendPort),
        DTXWIKI_CORS_ORIGINS: corsOrigins,
        DTXWIKI_DATABASE_URL: e2eDatabaseUrl,
        DTXWIKI_LIBRARY_BLOB_ROOT: e2eLibraryBlobRoot,
        DTXWIKI_SESSION_SECRET: "dtxwiki-e2e-session-secret",
        // Explicit, so the run cannot fall back to mount_spa's default
        // backend/static — present with a stale bundle on a developer machine
        // and absent on a fresh CI checkout, where the SPA silently 404s.
        DTXWIKI_STATIC_ROOT: e2eStaticRoot,
      },
      url: `${backendBaseUrl}/health/ready`,
      reuseExistingServer: false,
      timeout: 20_000,
    },
    {
      name: "background worker",
      command: [
        quoted(pythonExecutable),
        "-m backend.worker.entry run",
        "--skip-bootstrap",
        "--job-type library_conversion",
        "--job-type library_preview",
        "--poll-interval-seconds 0.5",
      ].join(" "),
      env: {
        ...inheritedEnv,
        ...identityRecoveryEnv,
        DTXWIKI_DATABASE_URL: e2eDatabaseUrl,
        DTXWIKI_LIBRARY_BLOB_ROOT: e2eLibraryBlobRoot,
        DTXWIKI_SESSION_SECRET: "dtxwiki-e2e-session-secret",
      },
      reuseExistingServer: false,
      stdout: "pipe",
      wait: { stdout: /"status":\s*"idle"/ },
      timeout: 20_000,
    },
    // No frontend server. tests/e2e/run-e2e.mjs builds the production bundle
    // into DTXWIKI_E2E_STATIC_ROOT before Playwright starts, and the backend
    // entry above serves it; its /health/ready gate is the only readiness wait
    // the browser needs.
    //
    // Every computed-style and cascade assertion in the suite now reads
    // minified, chunk-ordered production CSS instead of Vite's per-module dev
    // <style> tags -- tests/e2e/css_cascade_regressions.spec.ts most of all.
    // The full standard profile has since run clean under this topology
    // (124/124), and it found one real product defect the dev-server topology
    // could not reach: frontend/src/app/preloadRecovery.ts reloaded the page
    // the user was leaving, because Vite reports a navigation-cancelled chunk
    // and a genuinely missing chunk as the same vite:preloadError.
  ],
});

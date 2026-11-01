import { defineConfig, devices } from "@playwright/test";

// Run under Node 22.18 with:
//   npx playwright test --config tests/graph-webgl/playwright.config.ts
// A shader-only browser gate: no app server, credentials, or database. Keep
// this outside tests/e2e so the packaged-app wrapper never collects it.
export default defineConfig({
  testDir: ".",
  testMatch: "**/*.spec.ts",
  timeout: 30_000,
  retries: 0,
  workers: 1,
  reporter: "list",
  use: { screenshot: "only-on-failure", trace: "off" },
  // Titles tagged @pc10k drive 10K-node workloads. PR CI (ci.yml `graph-2k`)
  // runs `--project=chromium`; release-checks.yml `graph-10k` runs
  // `--project=chromium-pc10k` beside the real-API 10K specs. A local run
  // without --project runs both.
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] }, grepInvert: /@pc10k/ },
    { name: "chromium-pc10k", use: { ...devices["Desktop Chrome"] }, grep: /@pc10k/ },
  ],
});

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
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});

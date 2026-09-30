import type {
  Locator,
  LocatorScreenshotOptions,
  Page,
  PageScreenshotOptions,
  TestInfo,
} from "@playwright/test";

/**
 * Screenshots taken on the success path.
 *
 * The specs here produced several hundred PNGs per run -- per viewport, per
 * browser project, per screen -- and none of them was ever read by an
 * assertion. They exist so a person can look at the result afterwards, which
 * is worth keeping, but not worth paying for on every run. Playwright already
 * captures a screenshot and a trace when a test fails; this covers the other
 * case.
 *
 * Set DTXWIKI_E2E_EVIDENCE=1 to get them back. Failure-only screenshots and
 * traces are configured separately in playwright.config.ts and stay enabled.
 */
export function evidenceEnabled(): boolean {
  return process.env.DTXWIKI_E2E_EVIDENCE === "1";
}

/**
 * Capture `name` into the test's own output directory and attach it, or do
 * nothing when evidence is off.
 *
 * Always writes through `testInfo.outputPath` -- a path shared between browser
 * projects would have them overwrite each other's evidence, and would stop the
 * suite from ever being distributed.
 */
export async function evidenceScreenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
  options: Omit<PageScreenshotOptions, "path"> = {},
): Promise<string | undefined> {
  if (!evidenceEnabled()) {
    return undefined;
  }
  const target = testInfo.outputPath(
    name.endsWith(".png") ? name : `${name}.png`,
  );
  await page.screenshot({ ...options, path: target });
  await testInfo.attach(name, { path: target, contentType: "image/png" });
  return target;
}

/** Locator equivalent of evidenceScreenshot, under the same opt-in gate. */
export async function evidenceLocatorScreenshot(
  locator: Locator,
  testInfo: TestInfo,
  name: string,
  options: Omit<LocatorScreenshotOptions, "path"> = {},
): Promise<string | undefined> {
  if (!evidenceEnabled()) {
    return undefined;
  }
  const target = testInfo.outputPath(
    name.endsWith(".png") ? name : `${name}.png`,
  );
  await locator.screenshot({ ...options, path: target });
  await testInfo.attach(name, { path: target, contentType: "image/png" });
  return target;
}

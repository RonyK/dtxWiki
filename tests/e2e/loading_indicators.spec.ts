import { expect, test, type Locator, type Page } from "./fixtures";
import { login, seededMcpAccessEmail } from "./auth";
import { evidenceScreenshot } from "./evidence";

const viewportCases = [
  { locale: "en", width: 1440, height: 900 },
  { locale: "ko", width: 390, height: 844 },
] as const;

for (const viewport of viewportCases) {
  test(`shared loading indicators ${viewport.locale} ${viewport.width}px @cross-browser`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.addInitScript((locale) => {
      localStorage.setItem("dtxwiki.uiLanguage", locale);
    }, viewport.locale);
    // These journeys only read fixtures, so browsers can share this account.
    await login(page.request, seededMcpAccessEmail("chromium"));

    const tokenRoute = /\/api\/v1\/auth\/tokens(?:\?|$)/;
    let release = await holdResponses(page, tokenRoute);
    try {
      await page.goto("/account/integrations");
      const section = page.locator(".mcp-keys-section");
      const loading = section.locator(".loading-indicator");
      await expect(loading).toBeVisible();
      await expect(loading).toHaveAttribute("role", "status");
      await expect(loading).toHaveAttribute("aria-live", "polite");
      expect(await loading.evaluate((element) => element.closest('[aria-busy="true"]') === null)).toBe(true);
      await expect(section.locator(".mcp-key-row")).toHaveCount(0);
      await assertGlyph(loading, 16);
      await loading.scrollIntoViewIfNeeded();
      await evidenceScreenshot(page, testInfo, "access-keys-loading");

      release();
      await expect(section.locator(".mcp-key-row")).toHaveCount(20);
      await expect(loading).toHaveCount(0);
      await page.unroute(tokenRoute);

      release = await holdResponses(page, tokenRoute);
      await section.getByRole("searchbox").fill("Active UI key 01");
      const search = section.locator(".search-field-submit");
      await search.click();
      await expect(loading).toBeVisible();
      // A new query clears the old page; loading must explain the gap until
      // results for the new query arrive, without presenting stale matches.
      await expect(section.locator(".mcp-key-row")).toHaveCount(0);
      await expect(search).toBeDisabled();
      await assertGlyph(search, 14);
      release();
      await expect(section.locator(".mcp-key-row")).toHaveCount(1);
      await expect(loading).toHaveCount(0);
      await page.unroute(tokenRoute);

      const workspaceRoute = /\/api\/v1\/auth\/credential-workspaces(?:\?|$)/;
      release = await holdResponses(page, workspaceRoute);
      const opener = page.locator(".mcp-key-form .workspace-scope-summary button");
      await opener.click();
      const dialog = page.locator(".workspace-picker-dialog");
      const workspaceLoading = dialog.locator(".workspace-scope-loading");
      await expect(workspaceLoading).toBeVisible();
      await expect(dialog.getByRole("option")).toHaveCount(0);
      await expect(workspaceLoading.locator('[role="status"]')).toHaveCount(0);
      await assertGlyph(workspaceLoading, 16);
      const geometry = await workspaceLoading.evaluate((element) => {
        const glyph = element.querySelector(".spinner")!.getBoundingClientRect();
        const label = element.querySelector(".loading-indicator-label")!.getBoundingClientRect();
        return { separated: glyph.right <= label.left, fits: element.getBoundingClientRect().right <= innerWidth };
      });
      expect(geometry).toEqual({ separated: true, fits: true });
      await page.emulateMedia({ reducedMotion: "reduce" });
      await expect(workspaceLoading.locator(".spinner-glyph")).toHaveCSS("animation-name", "none");
      await expect(workspaceLoading.locator(".spinner")).toHaveCSS("animation-name", "none");
      await evidenceScreenshot(page, testInfo, "workspace-loading-reduced-motion");
      release();
      await expect(workspaceLoading).toHaveCount(0);
      await expect(dialog.getByRole("option").first()).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(opener).toBeFocused();
    } finally {
      release();
    }
  });
}

async function assertGlyph(container: Locator, size: number) {
  const spinner = container.locator(".spinner");
  await expect(spinner).toHaveAttribute("aria-hidden", "true");
  const glyph = spinner.locator(".spinner-glyph");
  await expect(glyph).toBeVisible();
  await expect(glyph).toHaveCSS("width", `${size}px`);
  await expect(glyph).toHaveCSS("height", `${size}px`);
}

async function holdResponses(page: Page, pattern: RegExp) {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route(pattern, async (route) => {
    await gate;
    await route.continue();
  });
  return release;
}

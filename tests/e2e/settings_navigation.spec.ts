import { expect, test } from "./fixtures";
import { authenticatePage } from "./auth";
import { evidenceScreenshot } from "./evidence";

for (const width of [390, 768]) {
  test(`@cross-browser ${width}px settings navigation scrolls with attachments`, async ({ page }, testInfo) => {
    await authenticatePage(page, testInfo, `settings-navigation-${width}`);
    await page.setViewportSize({ width, height: 520 });
    await page.addInitScript(() => localStorage.setItem("dtxwiki.uiLanguage", "ko"));
    await page.goto("/settings?section=attachments");
    const picker = page.locator(".settings-section-picker");
    const trigger = picker.getByRole("button");
    await expect(trigger).toContainText("첨부 파일");
    await expect(page.locator(".attachments")).toBeVisible();
    await expect(picker).toHaveCSS("position", "static");
    const bounds = await picker.boundingBox();
    const content = await page.locator(".settings-content").boundingBox();
    expect(bounds).not.toBeNull();
    expect(content).not.toBeNull();
    expect(content!.y).toBeGreaterThanOrEqual(bounds!.y + bounds!.height);
    await evidenceScreenshot(page, testInfo, `attachments-navigation-${width}`);

    const pane = page.locator(".feature-main");
    await pane.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await expect.poll(() => pane.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
    await expect.poll(async () => (await picker.boundingBox())!.y).toBeLessThan(bounds!.y);
    await evidenceScreenshot(page, testInfo, `attachments-scrolled-${width}`);

    await pane.evaluate((element) => { element.scrollTop = 0; });
    await trigger.click();
    const dialog = page.locator(".settings-section-picker-dialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /^워크스페이스 / }).click();
    await expect(dialog).toBeHidden();
    await expect(page.locator("#settings-destination-title")).toHaveText("워크스페이스");
  });
}

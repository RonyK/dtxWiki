import { expect, test } from "./fixtures";
import { authenticatePage, currentWorkspace, workspaceUrl } from "./auth";
import { evidenceScreenshot } from "./evidence";

for (const [locale, width] of [["en", 1440], ["ko", 390]] as const) {
  test(`@cross-browser ${locale} ${width}px copies issued keys and setup commands into the clipboard`, async ({ page }, testInfo) => {
    await authenticatePage(page, testInfo, `mcp-copy-${locale}`);
    await page.setViewportSize({ width, height: 900 });
    const privateHttp = process.env.DTXWIKI_E2E_PRIVATE_HTTP === "1";
    await page.addInitScript(({ language, forceLegacy }) => {
      localStorage.setItem("dtxwiki.uiLanguage", language);
      // CI serves localhost, which browsers treat as secure. Remove only the
      // modern API there so CI also exercises real legacy copy and paste.
      if (forceLegacy) Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    }, { language: locale, forceLegacy: !privateHttp });
    const workspace = await currentWorkspace(page.request);
    await page.goto(workspaceUrl(workspace.id, "settings/integrations"));
    if (privateHttp) {
      expect(await page.evaluate(() => window.isSecureContext)).toBe(false);
      const fallback = page.locator('#oauth-consents a[href="#mcp-key-setup"]');
      await expect(fallback).toHaveText(locale === "ko" ? "Access Key를 사용하세요." : "Use an Access Key.");
      await expect(fallback).toHaveCSS("display", "inline-flex");
      await fallback.scrollIntoViewIfNeeded();
      await evidenceScreenshot(page, testInfo, `oauth-access-key-link-${locale}`, { fullPage: true });
      await fallback.click();
      await expect(page.locator("#mcp-key-setup")).toBeFocused();
      const heading = await page.locator("#mcp-key-setup").boundingBox();
      expect(heading!.y).toBeGreaterThanOrEqual(0);
      expect(heading!.y).toBeLessThan(900);
      expect(await page.locator("#mcp-key-setup").evaluate((node) => {
        const box = node.getBoundingClientRect();
        const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        return node === hit || node.contains(hit);
      })).toBe(true);
      await evidenceScreenshot(page, testInfo, `access-key-anchor-${locale}`);
    }
    await page.getByLabel(locale === "ko" ? "키 이름" : "Key name", { exact: true }).fill(`Clipboard ${locale}`);
    await page.getByRole("button", { name: locale === "ko" ? "액세스 키 생성" : "Generate access key", exact: true }).click();
    const keyNode = page.locator(".token-once code").first();
    await expect(keyNode).toContainText("dtxw_");
    const key = (await keyNode.textContent())!.trim();
    const recipe = page.locator(".mcp-run-step-card .mcp-setup-recipe code").first();
    await expect(recipe).toBeVisible();
    if (privateHttp) await expect(recipe).toContainText(new URL(page.url()).origin + "/mcp");
    await expect(page.getByText(locale === "ko" ? "이 설정 절차를 안전하게 표시할 수 없습니다." : "This setup recipe could not be rendered safely.", { exact: true })).toHaveCount(0);
    await evidenceScreenshot(page, testInfo, `access-key-copy-${locale}`, {
      fullPage: true,
      mask: [keyNode, page.locator(".mcp-setup-recipe code")],
    });

    // Native paste proves the OS/browser clipboard received the full value;
    // text selection or a mocked write function cannot make this pass.
    await page.evaluate(() => {
      const sink = document.createElement("textarea");
      sink.dataset.testid = "clipboard-paste-probe";
      Object.assign(sink.style, { position: "fixed", top: "0", left: "0", zIndex: "9999" });
      document.body.append(sink);
    });
    const sink = page.getByTestId("clipboard-paste-probe");
    const operations = [
      [page.locator(".mcp-copy-issued-key"), key],
      [page.locator(".mcp-run-step-card .mcp-copy-artifact").first(), (await recipe.textContent())!],
    ] as const;
    for (const [index, [button, expected]] of operations.entries()) {
      await sink.fill("");
      await button.click();
      if (index === 0) {
        await expect(page.getByText(locale === "ko" ? "액세스 키 복사됨" : "Access key copied", { exact: true })).toBeVisible();
      } else {
        await expect(button.locator(".mcp-copy-artifact-status")).toHaveText(locale === "ko" ? "복사됨" : "Copied");
      }
      await sink.focus();
      await page.keyboard.press("Control+V");
      await expect(sink).toHaveValue(expected);
    }
    await sink.evaluate((node) => node.remove());
    await evidenceScreenshot(page, testInfo, `access-key-copied-${locale}`, {
      mask: [keyNode, page.locator(".mcp-setup-recipe code")],
    });
  });
}

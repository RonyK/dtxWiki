import { expect, test } from "@playwright/test";

import { login } from "./auth";

test.describe("Knowledge Map compact first viewport with real 2k data", () => {
  test.skip(process.env.DTXWIKI_E2E_GRAPH_2K !== "1", "Requires the isolated 2k seed");
  for (const language of ["en", "ko"] as const) {
    for (const viewport of [{ width: 1440, height: 900 }, { width: 834, height: 1112 }, { width: 390, height: 844 }]) {
      test(`${language} ${viewport.width} keeps the map or list in the first viewport`, async ({ page }, testInfo) => {
        const errors: string[] = [];
        page.on("pageerror", (error) => errors.push(error.message));
        page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
        await page.setViewportSize(viewport);
        await page.emulateMedia({ reducedMotion: "reduce" });
        expect((await page.request.get("/health/ready")).ok()).toBe(true);
        await login(page.request, "e2e-graph-2k-owner@example.com");
        await page.addInitScript((nextLanguage) => localStorage.setItem("dtxwiki.uiLanguage", nextLanguage), language);
        await page.goto("/graph");
        const list = page.getByTestId("workspace-graph-node-list");
        await expect(list).toHaveAttribute("data-total-count", "1928");
        await expect(page.locator(".workspace-graph-coverage")).toContainText("1928/2000");
        const feature = page.locator(".feature-main.workspace-graph");
        expect(await feature.evaluate((element) => element.scrollTop)).toBe(0);
        const geometry = await feature.evaluate((element) => {
          const listPanel = element.querySelector<HTMLElement>(".workspace-graph-list")!;
          const canvas = element.querySelector<HTMLElement>(".graph-canvas-frame");
          const listBounds = listPanel.getBoundingClientRect();
          let top = Math.max(0, listBounds.top);
          let bottom = Math.min(innerHeight, listBounds.bottom);
          for (let parent = listPanel.parentElement; parent; parent = parent.parentElement) {
            if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
            const bounds = parent.getBoundingClientRect();
            top = Math.max(top, bounds.top);
            bottom = Math.min(bottom, bounds.bottom);
          }
          return { canvasTop: canvas?.getBoundingClientRect().top ?? null,
            listVisibleHeight: Math.max(0, bottom - top), viewportHeight: innerHeight,
            horizontalOverflow: document.documentElement.scrollWidth - innerWidth };
        });
        await testInfo.attach("first-viewport-geometry", { body: JSON.stringify(geometry), contentType: "application/json" });
        expect(geometry.horizontalOverflow).toBeLessThanOrEqual(1);
        if (viewport.width > 640) {
          expect(geometry.canvasTop).not.toBeNull();
          expect(geometry.canvasTop!).toBeLessThanOrEqual(220);
        } else {
          expect(geometry.listVisibleHeight).toBeGreaterThanOrEqual(viewport.height * 0.4);
        }
        const view = page.locator(".workspace-graph-view-options");
        await view.locator("summary").click();
        await expect(view).toHaveAttribute("open", "");
        const menuBounds = await view.locator(".workspace-graph-view-panel").boundingBox();
        expect(menuBounds!.x).toBeGreaterThanOrEqual(0);
        expect(menuBounds!.x + menuBounds!.width).toBeLessThanOrEqual(viewport.width);
        const folder = view.getByRole("button", { name: language === "en" ? "Folder context" : "폴더 보조 정보", exact: true });
        await folder.click();
        await expect(folder).toHaveAttribute("aria-pressed", "true");
        // Metadata disclosure and selection must not push the original canvas down.
        await view.press("Escape");
        await expect(view.locator("summary")).toBeFocused();
        if (viewport.width > 640) {
          const after = await page.locator(".graph-canvas-frame").boundingBox();
          expect(after!.y).toBe(geometry.canvasTop);
        }
        expect(errors).toEqual([]);
        await page.screenshot({ path: testInfo.outputPath(`first-view-${language}-${viewport.width}.png`) });
      });
    }
  }
});

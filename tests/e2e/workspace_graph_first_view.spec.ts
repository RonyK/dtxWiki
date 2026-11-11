import { expect, test } from "@playwright/test";

import type { WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";
import { login, enableKnowledgeMapLab } from "./auth";
import { isResizeObserverDeferralReport } from "./fixtures";
import { listedNodeIds, sceneRelations } from "./workspace_graph_scene";

test.describe("Knowledge Map compact first viewport with real 2k data", () => {
  test.skip(process.env.DTXWIKI_E2E_GRAPH_2K !== "1", "Requires the isolated 2k seed");
  // Each width owns a shell layout (desktop, 641-1024 tablet, <=640 mobile).
  // Locale changes only text geometry, so each width runs once in the locale
  // whose copy is wider there: the English coverage chip in the 390px header,
  // the Korean Refine label in the single-row 834px toolbar.
  for (const { language, viewport } of [
    { language: "en", viewport: { width: 1440, height: 900 } },
    { language: "ko", viewport: { width: 834, height: 1112 } },
    { language: "en", viewport: { width: 390, height: 844 } },
  ] as const) {
    test(`${language} ${viewport.width} keeps the map or list in the first viewport`, async ({ page }, testInfo) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => { if (!isResizeObserverDeferralReport(error.message)) errors.push(error.message); });
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.setViewportSize(viewport);
      await page.emulateMedia({ reducedMotion: "reduce" });
      expect((await page.request.get("/health/ready")).ok()).toBe(true);
      await login(page.request, "e2e-graph-2k-owner@example.com");
      await enableKnowledgeMapLab(page);
      await page.addInitScript((nextLanguage) => localStorage.setItem("dtxwiki.uiLanguage", nextLanguage), language);
      const overview = page.waitForResponse((response) =>
        response.url().includes("/graph/overview/query") && response.request().method() === "POST");
      await page.goto("/graph");
      const scene = (await (await overview).json()).data as WorkspaceGraphScene;
      const linked = new Set(sceneRelations(scene).flatMap((edge) => [edge.source, edge.target]));
      expect(linked.size).toBe(1928);
      // The list holds the 1,928 linked documents and the folders that hold them.
      const list = page.getByTestId("workspace-graph-node-list");
      await expect(list).toHaveAttribute("data-total-count", String(listedNodeIds(scene, linked).size));
      await expect(page.locator(".workspace-graph-coverage")).toContainText("1928/2000");
      const feature = page.locator(".feature-main.workspace-graph");
      const canvas = feature.locator(".graph-canvas-frame");
      await expect(canvas).toBeVisible();
      expect(await feature.evaluate((element) => element.scrollTop)).toBe(0);
      const geometry = await feature.evaluate((element) => {
        // The relationship list now lives in the sidebar/sheet. The map is
        // the first-viewport content at every size, below its attached title.
        const canvas = element.querySelector<HTMLElement>(".graph-canvas-frame")!;
        const title = element.querySelector<HTMLElement>(".workspace-graph-top-panel")!;
        const canvasBounds = canvas.getBoundingClientRect();
        let top = Math.max(0, canvasBounds.top);
        let bottom = Math.min(innerHeight, canvasBounds.bottom);
        for (let parent = canvas.parentElement; parent; parent = parent.parentElement) {
          if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
          const bounds = parent.getBoundingClientRect();
          top = Math.max(top, bounds.top);
          bottom = Math.min(bottom, bounds.bottom);
        }
        return { canvasTop: canvasBounds.top, titleBottom: title.getBoundingClientRect().bottom,
          canvasVisibleHeight: Math.max(0, bottom - top), viewportHeight: innerHeight,
          horizontalOverflow: document.documentElement.scrollWidth - innerWidth };
      });
      await testInfo.attach("first-viewport-geometry", { body: JSON.stringify(geometry), contentType: "application/json" });
      expect(geometry.horizontalOverflow).toBeLessThanOrEqual(1);
      expect(geometry.canvasTop).toBeLessThanOrEqual(220);
      expect(geometry.canvasTop).toBeGreaterThanOrEqual(geometry.titleBottom);
      expect(geometry.canvasVisibleHeight).toBeGreaterThanOrEqual(viewport.height * 0.4);
      const view = page.locator(".graph-view-options");
      await view.locator("summary").click();
      await expect(view).toHaveAttribute("open", "");
      const menuBounds = await view.locator(".graph-view-panel").boundingBox();
      expect(menuBounds!.x).toBeGreaterThanOrEqual(0);
      expect(menuBounds!.x + menuBounds!.width).toBeLessThanOrEqual(viewport.width);
      // Opening and closing the View disclosure must not push the original canvas down.
      await view.press("Escape");
      await expect(view.locator("summary")).toBeFocused();
      const after = await canvas.boundingBox();
      expect(after!.y).toBe(geometry.canvasTop);
      expect(errors).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`first-view-${language}-${viewport.width}.png`) });
    });
  }
});

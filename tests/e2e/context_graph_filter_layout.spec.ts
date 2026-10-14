import { randomUUID } from "node:crypto";

import { expect, test, type APIRequestContext } from "./fixtures";
import { authenticatePage, backendBaseUrl, currentWorkspace } from "./auth";

const viewports = [
  { key: "desktop", width: 1440, height: 900 },
  { key: "mobile", width: 390, height: 844 },
] as const;

for (const viewport of viewports) {
  test.describe(`Context Graph filter layout ${viewport.key}`, () => {
    test.use({ viewport });

    test("puts the live canvas before closed advanced filters and keeps them operable", async ({
      page,
    }, testInfo) => {
      test.setTimeout(120_000);
      await authenticatePage(page, testInfo, `context-graph-layout-${viewport.key}`);
      const workspace = await currentWorkspace(page.request);
      const suffix = `${viewport.key}-${testInfo.workerIndex}-${Date.now()}`;
      const related = await createAndPublishPage(page.request, workspace.id, {
        title: `Graph related ${suffix}`,
        body_md: `# Graph related ${suffix}`,
        page_type: "concept",
        tags: ["e2e", "graph-layout"],
      });
      const selected = await createAndPublishPage(page.request, workspace.id, {
        title: `Graph source ${suffix}`,
        body_md: `# Graph source ${suffix}\n\nSee [[${related.slug}|related page]].`,
        page_type: "decision",
        tags: ["e2e", "graph-layout"],
      });

      await page.goto(`/context?target=${encodeURIComponent(selected.id)}`);
      await page.getByRole("tablist", { name: "Context reader views" })
        .getByRole("tab", { name: /^Graph/ }).click();
      const graph = page.getByTestId("graph-view");
      const stage = graph.locator(".graph-local-stage");
      const canvasFrame = graph.locator(".graph-canvas-frame");
      const details = graph.locator(".graph-details");
      const filters = graph.locator(".graph-filter-bar");
      await expect(canvasFrame).toHaveAttribute("data-renderer-status", "ready", {
        timeout: 60_000,
      });
      expect(await filters.getAttribute("open")).toBeNull();
      await expect(filters.locator(".graph-filter-options-grid")).toBeHidden();
      await expect(details).toBeHidden();

      await graph.locator(".graph-view-header").evaluate((element) => {
        element.scrollIntoView({ block: "start" });
      });
      const geometry = await graph.evaluate((element) => {
        const stageBounds = element.querySelector(".graph-local-stage")!.getBoundingClientRect();
        const canvasBounds = element.querySelector(".graph-canvas-frame")!.getBoundingClientRect();
        const filterBounds = element.querySelector(".graph-filter-bar")!.getBoundingClientRect();
        return {
          canvasTop: canvasBounds.top,
          documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          filterTop: filterBounds.top,
          stageBottom: stageBounds.bottom,
          stageWidth: stageBounds.width,
          canvasWidth: canvasBounds.width,
          stageHeight: stageBounds.height,
          canvasHeight: canvasBounds.height,
        };
      });
      expect(geometry.canvasTop).toBeLessThan(viewport.height * 0.75);
      expect(geometry.filterTop).toBeGreaterThanOrEqual(geometry.stageBottom - 1);
      expect(geometry.documentOverflow).toBeLessThanOrEqual(1);
      expect(geometry.canvasWidth).toBe(geometry.stageWidth);
      expect(geometry.canvasHeight).toBe(geometry.stageHeight);
      await page.screenshot({ path: testInfo.outputPath(`${viewport.key}-graph-first-viewport.png`) });

      // Live API list selection (not a synthetic renderer callback). Playwright
      // may scroll the reader to reach this row; compare canvas size, not its
      // viewport y-position, and explicitly scroll the stage for the capture.
      let graphQueries = 0;
      page.on("request", (request) => {
        if (request.method() === "POST" && /\/graph\/query(?:\?|$)/.test(request.url())) graphQueries += 1;
      });
      const nativeCanvas = await graph.locator("canvas.sigma-nodes").elementHandle();
      const sourceRow = graph.locator(".graph-relationship-list > details > ul > li > button.graph-list-select")
        .filter({ has: page.locator("strong", { hasText: `Graph source ${suffix}` }) });
      await sourceRow.click();
      await expect(details.locator("h5")).toHaveText(`Graph source ${suffix}`);
      await expect(details.getByRole("link", { name: "Open", exact: true })).toBeVisible();
      await expect(details.getByRole("button", { name: "Center", exact: true })).toBeVisible();
      await details.getByRole("button", { name: "Pin", exact: true }).click();
      await expect(details.getByRole("button", { name: "Unpin", exact: true })).toBeVisible();
      await stage.scrollIntoViewIfNeeded();
      const selectedGeometry = await graph.evaluate((element) => {
        const owner = element.querySelector(".graph-local-stage")!.getBoundingClientRect();
        const canvas = element.querySelector(".graph-canvas-frame")!.getBoundingClientRect();
        const panel = element.querySelector(".graph-details")!.getBoundingClientRect();
        return { stageWidth: owner.width, stageHeight: owner.height,
          canvasWidth: canvas.width, canvasHeight: canvas.height,
          panelTop: panel.top - owner.top, panelBottom: panel.bottom - owner.bottom,
          panelLeft: panel.left - owner.left, panelRight: panel.right - owner.right,
          panelHeight: panel.height,
          painted: element.querySelector(".graph-details")!.contains(document.elementFromPoint(panel.left + 4, panel.top + 4)) };
      });
      expect(selectedGeometry.stageWidth).toBe(geometry.stageWidth);
      expect(selectedGeometry.stageHeight).toBe(geometry.stageHeight);
      expect(selectedGeometry.canvasWidth).toBe(geometry.canvasWidth);
      expect(selectedGeometry.canvasHeight).toBe(geometry.canvasHeight);
      expect(selectedGeometry.panelLeft).toBeGreaterThanOrEqual(0);
      expect(selectedGeometry.panelRight).toBeLessThanOrEqual(0);
      expect(selectedGeometry.panelBottom).toBeLessThanOrEqual(1);
      expect(selectedGeometry.painted).toBe(true);
      if (selectedGeometry.stageWidth <= 760) {
        expect(selectedGeometry.panelTop).toBeGreaterThan(geometry.stageHeight / 2);
        expect(selectedGeometry.panelHeight).toBeLessThanOrEqual(geometry.stageHeight * 0.45 + 1);
      } else {
        expect(selectedGeometry.panelTop).toBe(12);
        expect(selectedGeometry.panelRight).toBe(-12);
      }
      await page.screenshot({ path: testInfo.outputPath(`${viewport.key}-graph-selected-details.png`) });
      await details.getByRole("button", { name: "Clear selection", exact: true }).focus();
      await page.keyboard.press("Escape");
      await expect(details).toBeHidden();
      await expect(graph).toBeFocused();
      await sourceRow.click();
      await expect(details.getByRole("button", { name: "Unpin", exact: true })).toBeVisible();
      await details.getByRole("button", { name: "Clear selection", exact: true }).click();
      await expect(details).toBeHidden();
      expect(graphQueries).toBe(0);
      expect(await nativeCanvas!.evaluate((element) => element === document.querySelector('[data-testid="graph-view"] canvas.sigma-nodes'))).toBe(true);
      const clearedSize = await canvasFrame.boundingBox();
      expect(clearedSize!.width).toBe(geometry.canvasWidth);
      expect(clearedSize!.height).toBe(geometry.canvasHeight);

      await filters.locator("summary").click();
      await expect(filters).toHaveAttribute("open", "");
      const status = filters.getByRole("group", { name: "Node status" });
      await expect(status).toBeVisible();
      const checkbox = status.getByRole("checkbox").first();
      const initiallyChecked = await checkbox.isChecked();
      await checkbox.click();
      expect(await checkbox.isChecked()).toBe(!initiallyChecked);
      await filters.getByRole("button", { name: "Clear all" }).click();
      expect(await checkbox.isChecked()).toBe(initiallyChecked);
      await page.screenshot({ path: testInfo.outputPath(`${viewport.key}-graph-filters-open.png`) });
      await expect(stage).toBeAttached();
    });
  });
}

async function createAndPublishPage(
  request: APIRequestContext,
  workspaceId: string,
  data: Record<string, unknown>,
) {
  const base = `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages`;
  const createdResponse = await request.post(base, { data });
  expect(createdResponse.ok(), await createdResponse.text()).toBeTruthy();
  const created = (await createdResponse.json()).data as {
    id: string;
    slug: string;
    version_id: string;
    editing_revision: number;
  };
  const submitResponse = await request.post(`${base}/${created.id}/submit-for-review`, {
    data: {
      version_id: created.version_id,
      expected_editing_revision: created.editing_revision,
      idempotency_key: randomUUID(),
    },
  });
  expect(submitResponse.ok(), await submitResponse.text()).toBeTruthy();
  const submitted = (await submitResponse.json()).data as {
    version_id: string;
    content_hash: string;
    review_item_id: string;
  };
  const decisionResponse = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/reviews/${submitted.review_item_id}/decision`,
    { data: { decision_type: "approve" } },
  );
  expect(decisionResponse.ok(), await decisionResponse.text()).toBeTruthy();
  const publishResponse = await request.post(
    `${base}/${created.id}/versions/${submitted.version_id}/publish`,
    {
      data: {
        reason: "e2e: publish Context Graph layout fixture",
        expected_content_hash: submitted.content_hash,
      },
    },
  );
  expect(publishResponse.ok(), await publishResponse.text()).toBeTruthy();
  return created;
}

import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { login } from "./auth";

const GRAPH_OWNER_EMAIL = "e2e-graph-owner@example.com";
const NODE_COUNT = 2_000;
const EDGE_COUNT = 4_700;

// Frontend-only scale fixture. The production overview endpoint permits an
// explicit 2k/6k budget, but this route interception does not demonstrate a
// larger backend response or a performance acceptance result.
const syntheticScene = makeSyntheticScene();

test.describe("Workspace Map synthetic 2k list", () => {
  test.describe("desktop", () => {
    test.use({ viewport: { width: 1440, height: 900 } });

    test("bounds DOM rows, keeps selection keyboard-accessible, and paints WebGL", async ({
      page,
    }, testInfo) => {
      test.setTimeout(120_000);
      await page.emulateMedia({ reducedMotion: "no-preference" });
      await login(page.request, GRAPH_OWNER_EMAIL);
      const diagnostics = watchBrowserDiagnostics(page);
      const requests = await interceptOverview(page);

      await page.goto("/graph");
      await expect(page.getByRole("searchbox", { name: "Find in visible map" }))
        .toHaveAttribute("placeholder", "Highlight matches and narrow the list without moving the map");
      await expect(page.getByRole("searchbox", { name: "Search published documents" }))
        .toHaveCount(0);
      await page.getByRole("button", { name: "Refine" }).click();
      const searchDialog = page.getByRole("dialog", { name: "Refine data" });
      await expect(searchDialog.getByRole("searchbox", { name: "Search published documents" }))
        .toHaveAttribute("placeholder", "Search titles or paths, then Apply");
      await expect(searchDialog.getByRole("button", { name: "Apply data filters" }))
        .toBeVisible();
      await searchDialog.getByRole("button", { name: "Close Refine data" }).click();
      await expect(page.locator("details.workspace-graph-advanced-controls"))
        .not.toHaveAttribute("open", "");
      const nodeList = page.getByTestId("workspace-graph-node-list");
      const edgeList = page.getByTestId("workspace-graph-edge-list");
      await expect(nodeList).toHaveAttribute("data-total-count", String(NODE_COUNT));
      await expect(edgeList).toHaveAttribute("data-total-count", String(EDGE_COUNT));
      expect(requests.count).toBeGreaterThan(0);
      await expect(nodeList).toHaveAttribute("aria-describedby", /keyboard-hint/);
      await expect(edgeList).toHaveAttribute("aria-describedby", /keyboard-hint/);
      await expectBoundedRows(nodeList, edgeList);

      const canvas = page.getByTestId("graph-canvas");
      await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]'))
        .toBeVisible({ timeout: 60_000 });
      await expect(canvas).toHaveAttribute("data-map-node-count", String(NODE_COUNT));
      await expect(canvas).toHaveAttribute("data-map-edge-count", String(EDGE_COUNT));
      await expect(canvas).toHaveAttribute("data-map-community-count", /^[1-9]\d*$/, {
        timeout: 30_000,
      });
      await expect(canvas).toHaveAttribute("data-map-community-layout-status", "applied");
      const pauseLayout = page.getByRole("button", { name: "Pause layout" });
      const resumeLayout = page.getByRole("button", { name: "Resume layout" });
      await expect(pauseLayout).toBeEnabled();
      await pauseLayout.click();
      await expect(resumeLayout).toBeEnabled();
      await resumeLayout.click();
      await expect(pauseLayout).toBeEnabled();
      // The current 2K contract is a live layout. Explicitly pause before
      // pixel/camera comparisons rather than relying on natural cooling.
      await pauseLayout.click();
      await expect(resumeLayout).toBeEnabled();
      await expect(page.getByRole("button", { name: "Fixed layout for large maps" })).toHaveCount(0);
      const canvasBounds = await canvas.boundingBox();
      expect(canvasBounds?.y).toBeLessThan(700);
      await afterAnimationFrame(page);
      await capture(page, testInfo, "synthetic-2k-desktop-first-viewport.png");
      await capture(canvas, testInfo, "synthetic-2k-desktop-overview-webgl-canvas.png");
      // Counts prove the data reached the canvas, not that Cosmos is actually
      // drawing relationships. Compare the native WebGL pixels while the
      // client-only "Show relationships" setting is switched off and back on.
      const nativeCanvas = canvas.locator("canvas").first();
      const linksOn = await capture(nativeCanvas, testInfo, "synthetic-2k-native-links-on.png");
      await page.getByRole("button", { name: "Refine" }).click();
      const displayDialog = page.getByRole("dialog", { name: "Refine data" });
      const showRelationships = displayDialog.getByRole("checkbox", {
        name: "Show relationships",
      });
      await expect(showRelationships).toBeChecked();
      await showRelationships.uncheck();
      await page.keyboard.press("Escape");
      await afterAnimationFrame(page);
      const linksOff = await capture(nativeCanvas, testInfo, "synthetic-2k-native-links-off.png");
      expect(linksOff.equals(linksOn), "toggling relationships must change native WebGL pixels")
        .toBe(false);
      await page.getByRole("button", { name: "Refine" }).click();
      await showRelationships.check();
      await page.keyboard.press("Escape");
      await afterAnimationFrame(page);
      const linksRestored = await capture(
        nativeCanvas,
        testInfo,
        "synthetic-2k-native-links-restored.png",
      );
      expect(linksRestored.equals(linksOff), "restoring relationships must change WebGL pixels")
        .toBe(false);

      await expectFilteredScrollContinuity(page, requests);

      await nodeList.evaluate((element) => { element.scrollTop = element.scrollHeight; });
      const lastNode = nodeList.getByRole("button", { name: /Virtual Document 1999/ });
      await expect(lastNode).toBeVisible();
      await expect(lastNode.locator("xpath=.."))
        .toHaveAttribute("aria-posinset", String(NODE_COUNT));
      await lastNode.click();
      await expect(page.locator(".workspace-graph-details").getByRole("heading", {
        name: "Virtual Document 1999",
      })).toBeVisible();
      await expect(page.getByText("A selection is active. Other relationships may appear faded."))
        .toBeVisible();
      await expect(page.getByRole("button", { name: "Clear selection" })).toBeVisible();
      const selectedLabel = canvas.locator("[data-workspace-map-labels] span")
        .filter({ hasText: "Virtual Document 1999" });
      await expect(selectedLabel).toBeVisible({ timeout: 10_000 });
      const labelBounds = await selectedLabel.boundingBox();
      const selectedCanvasBounds = await canvas.boundingBox();
      expect(labelBounds).not.toBeNull();
      expect(selectedCanvasBounds).not.toBeNull();
      expect(labelBounds!.x).toBeGreaterThanOrEqual(selectedCanvasBounds!.x);
      expect(labelBounds!.y).toBeGreaterThanOrEqual(selectedCanvasBounds!.y);
      expect(labelBounds!.x + labelBounds!.width)
        .toBeLessThanOrEqual(selectedCanvasBounds!.x + selectedCanvasBounds!.width);
      expect(labelBounds!.y + labelBounds!.height)
        .toBeLessThanOrEqual(selectedCanvasBounds!.y + selectedCanvasBounds!.height);
      await capture(canvas, testInfo, "synthetic-2k-desktop-selected-webgl-canvas.png");
      // Keep an intentional list scroll while toolbar/camera state rerenders
      // the screen; selection should reveal once, never trap the scroll.
      await nodeList.evaluate((element) => { element.scrollTop = 1_500 * 64; });
      await afterAnimationFrame(page);
      const manualScroll = await nodeList.evaluate((element) => element.scrollTop);
      // Browser scrollHeight can clamp while virtual spacers settle. Compare
      // the settled user position, not an immediate assignment before paint.
      expect(manualScroll).toBeGreaterThan(90_000);
      await expect(lastNode).toHaveCount(0);
      await page.getByRole("button", { name: "Zoom out" }).click();
      await afterAnimationFrame(page);
      expect(await nodeList.evaluate((element) => element.scrollTop)).toBe(manualScroll);
      await page.getByRole("button", { name: "Refine" }).click();
      await page.keyboard.press("Escape");
      expect(await nodeList.evaluate((element) => element.scrollTop)).toBe(manualScroll);
      await nodeList.evaluate((element) => { element.scrollTop = element.scrollHeight; });
      await lastNode.focus();
      await page.keyboard.press("Home");
      await expect(nodeList.getByRole("button", { name: /Virtual Document 0000/ }))
        .toBeFocused();

      await edgeList.evaluate((element) => { element.scrollTop = element.scrollHeight; });
      await expect(edgeList.locator('li[data-graph-edge-id="edge:04699"]'))
        .toBeVisible();
      await expectBoundedRows(nodeList, edgeList);

      const gpu = await canvas.evaluate((element) => {
        const canvases = element.querySelectorAll("canvas");
        const gl = canvases[0]?.getContext("webgl2");
        return {
          canvasCount: canvases.length,
          contextLost: gl?.isContextLost() ?? true,
          glError: gl?.getError() ?? -1,
          webgl2: Boolean(gl),
          width: canvases[0]?.width ?? 0,
          height: canvases[0]?.height ?? 0,
        };
      });
      expect(gpu).toMatchObject({
        canvasCount: 1,
        contextLost: false,
        glError: 0,
        webgl2: true,
      });
      expect(gpu.width).toBeGreaterThan(0);
      expect(gpu.height).toBeGreaterThan(0);
      await afterAnimationFrame(page);
      await capture(page, testInfo, "synthetic-2k-desktop-map.png");
      await capture(canvas, testInfo, "synthetic-2k-desktop-webgl-canvas.png");
      expect(diagnostics.pageErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
    });
  });

  test.describe("mobile", () => {
    test.use({ viewport: { width: 390, height: 844 } });

    test("remains list-first with bounded touch and keyboard navigation", async ({
      page,
    }, testInfo) => {
      test.setTimeout(90_000);
      await page.emulateMedia({ reducedMotion: "reduce" });
      await login(page.request, GRAPH_OWNER_EMAIL);
      const diagnostics = watchBrowserDiagnostics(page);
      const requests = await interceptOverview(page);

      await page.goto("/graph");
      await expect(page.getByRole("searchbox", { name: "Find in visible map" }))
        .toHaveAttribute("placeholder", "Find and highlight in this map");
      await expect(page.getByRole("searchbox", { name: "Search published documents" }))
        .toHaveCount(0);
      await page.getByRole("button", { name: "Refine" }).click();
      const searchDialog = page.getByRole("dialog", { name: "Refine data" });
      await expect(searchDialog.getByRole("searchbox", { name: "Search published documents" }))
        .toHaveAttribute("placeholder", "Search titles or paths, then Apply");
      await searchDialog.getByRole("button", { name: "Close Refine data" }).click();
      await expect(page.locator("details.workspace-graph-advanced-controls"))
        .not.toHaveAttribute("open", "");
      const nodeList = page.getByTestId("workspace-graph-node-list");
      const edgeList = page.getByTestId("workspace-graph-edge-list");
      await expect(nodeList).toHaveAttribute("data-total-count", String(NODE_COUNT));
      await expect(edgeList).toHaveAttribute("data-total-count", String(EDGE_COUNT));
      expect(requests.count).toBeGreaterThan(0);
      await expect(page.getByTestId("graph-canvas")).toHaveCount(0);
      await expectBoundedRows(nodeList, edgeList);
      const listBounds = await nodeList.boundingBox();
      expect(listBounds?.y).toBeLessThan(700);
      await capture(page, testInfo, "synthetic-2k-mobile-first-viewport.png");

      await expectFilteredScrollContinuity(page, requests);

      await nodeList.evaluate((element) => { element.scrollTop = element.scrollHeight; });
      const lastNode = nodeList.getByRole("button", { name: /Virtual Document 1999/ });
      await expect(lastNode).toBeVisible();
      await lastNode.click();
      await expect(page.locator(".workspace-graph-details").getByRole("heading", {
        name: "Virtual Document 1999",
      })).toBeVisible();
      await lastNode.focus();
      await page.keyboard.press("Home");
      await expect(nodeList.getByRole("button", { name: /Virtual Document 0000/ }))
        .toBeFocused();
      await expectBoundedRows(nodeList, edgeList);
      await capture(page, testInfo, "synthetic-2k-mobile-list.png");

      const openCanvas = page.getByRole("button", { name: "Open canvas fullscreen" });
      await openCanvas.click();
      const dialog = page.getByRole("dialog", { name: "Knowledge Map canvas" });
      const closeCanvas = dialog.getByRole("button", { name: "Close fullscreen canvas" });
      await expect(closeCanvas).toBeFocused();
      const toolbar = dialog.getByRole("toolbar");
      await expect(toolbar.getByRole("button", { name: "Zoom in" })).toBeVisible();
      await expect(toolbar.getByRole("button", { name: "Layout motion is off" }))
        .toBeDisabled();
      const canvas = dialog.getByTestId("graph-canvas");
      await expect(dialog.locator('.graph-canvas-frame[data-renderer-status="ready"]'))
        .toBeVisible({ timeout: 60_000 });
      await expect(canvas).toHaveAttribute("data-map-node-count", String(NODE_COUNT));
      await expect(canvas).toHaveAttribute("data-map-community-count", /^[1-9]\d*$/);
      const viewport = page.viewportSize()!;
      const toolbarBounds = (await toolbar.boundingBox())!;
      const canvasBounds = (await canvas.boundingBox())!;
      expect(canvasBounds.x).toBeGreaterThanOrEqual(0);
      expect(canvasBounds.y).toBeGreaterThanOrEqual(toolbarBounds.y + toolbarBounds.height);
      expect(canvasBounds.width).toBeGreaterThan(300);
      expect(canvasBounds.height).toBeGreaterThan(300);
      expect(canvasBounds.x + canvasBounds.width).toBeLessThanOrEqual(viewport.width);
      expect(canvasBounds.y + canvasBounds.height).toBeLessThanOrEqual(viewport.height);
      await afterAnimationFrame(page);
      const nativeCanvas = canvas.locator("canvas").first();
      const painted = await capture(nativeCanvas, testInfo, "synthetic-2k-mobile-native-canvas.png");
      const paintedPixels = await saturatedPixelCount(page, painted);
      await testInfo.attach("synthetic-2k-mobile-geometry.json", {
        body: JSON.stringify({ viewport, toolbarBounds, canvasBounds, paintedPixels }),
        contentType: "application/json",
      });
      expect(paintedPixels,
        "The fullscreen native framebuffer must paint graph geometry, not just a border or empty background")
        .toBeGreaterThan(100);
      await capture(page, testInfo, "synthetic-2k-mobile-fullscreen.png");
      await expect(dialog.getByText("A selection is active. Other relationships may appear faded."))
        .toBeVisible();
      const clearSelection = dialog.getByRole("button", { name: "Clear selection" });
      await expect(clearSelection).toBeVisible();
      await clearSelection.click();
      await expect(clearSelection).toHaveCount(0);
      await expect(closeCanvas).toBeFocused();
      await expect(dialog).toBeVisible();
      await capture(page, testInfo, "synthetic-2k-mobile-selection-cleared.png");
      await closeCanvas.click();
      await expect(dialog).toHaveCount(0);
      await expect(canvas).toHaveCount(0);
      await expect(openCanvas).toBeFocused();
      expect(diagnostics.pageErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
    });
  });
});

async function interceptOverview(page: Page) {
  const requests = { count: 0 };
  await page.route("**/api/v1/workspaces/*/graph/overview/query", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    requests.count += 1;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ data: syntheticScene }),
      status: 200,
    });
  });
  return requests;
}

async function expectBoundedRows(
  nodeList: ReturnType<Page["getByTestId"]>,
  edgeList: ReturnType<Page["getByTestId"]>,
) {
  expect(await nodeList.locator("li[data-graph-node-id]").count()).toBeLessThan(30);
  expect(await edgeList.locator("li[data-graph-edge-id]").count()).toBeLessThan(30);
}

async function expectFilteredScrollContinuity(page: Page, requests: { count: number }) {
  const list = page.getByTestId("workspace-graph-node-list");
  const search = page.getByRole("searchbox", { name: "Find in visible map" });
  const query = "0";
  const matching = syntheticScene.nodes.filter((node) => node.label.includes(query));
  expect(matching.length).toBeGreaterThan(1_000);
  expect(matching.length).toBeLessThan(NODE_COUNT);
  const requestCount = requests.count;
  const nativeCanvas = page.getByTestId("graph-canvas").locator("canvas");
  const nativeCount = await nativeCanvas.count();
  const native = nativeCount ? await nativeCanvas.first().elementHandle() : null;
  const savedViewports = () => page.evaluate(() => Object.keys(sessionStorage)
    .filter((key) => key.startsWith("dtxwiki:workspace-graph:v2:") && key.endsWith(":latest"))
    .sort().map((key) => ({ key, viewport: JSON.parse(sessionStorage.getItem(key)!).state.viewport })));
  await expect.poll(async () => (await savedViewports()).length).toBeGreaterThan(0);
  const viewportBefore = await savedViewports();
  await list.locator("button[data-list-index='0']").focus();
  await page.keyboard.press("End");
  await expect(list.locator(`button[data-list-index='${NODE_COUNT - 1}']`)).toBeFocused();
  await search.fill(query);
  await expect(list).toHaveAttribute("data-total-count", String(matching.length));
  const window = await list.evaluate((element) => {
    const rows = Array.from(element.querySelectorAll<HTMLLIElement>("li[data-graph-node-id]"));
    return {
      indices: rows.map((row) => Number(row.querySelector<HTMLButtonElement>("button")!.dataset.listIndex)),
      rowHeight: Number.parseFloat(rows[0]?.style.height ?? "0"),
      spacerHeight: Array.from(element.querySelectorAll<HTMLElement>(".workspace-graph-list-spacer"))
        .reduce((height, spacer) => height + Number.parseFloat(spacer.style.height), 0),
      scrollTop: element.scrollTop, maximumScrollTop: element.scrollHeight - element.clientHeight,
    };
  });
  expect(window.indices.length).toBeGreaterThan(0);
  expect(window.indices.length).toBeLessThan(30);
  expect(window.rowHeight).toBeGreaterThan(0);
  expect(window.indices[0]).toBeGreaterThanOrEqual(0);
  expect(window.indices.at(-1)!).toBeLessThan(matching.length);
  expect(window.indices).toEqual(Array.from({ length: window.indices.length }, (_, offset) => window.indices[0] + offset));
  expect(window.spacerHeight).toBe((matching.length - window.indices.length) * window.rowHeight);
  expect(window.scrollTop).toBeLessThanOrEqual(window.maximumScrollTop + 1);
  await search.clear();
  await expect(list).toHaveAttribute("data-total-count", String(NODE_COUNT));
  await list.locator("button[data-list-index]").first().focus();
  await page.keyboard.press("Home");
  await expect(list.locator("button[data-list-index='0']")).toBeFocused();
  await page.keyboard.press("End");
  await expect(list.locator(`button[data-list-index='${NODE_COUNT - 1}']`)).toBeFocused();
  await expect(page.getByTestId("graph-canvas").locator("canvas")).toHaveCount(nativeCount);
  if (native) {
    expect(await native.evaluate((element) => element === document.querySelector('[data-testid="graph-canvas"] canvas'))).toBe(true);
    await native.dispose();
  }
  expect(await savedViewports()).toEqual(viewportBefore);
  expect(requests.count).toBe(requestCount);
}

function watchBrowserDiagnostics(page: Page) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  return { pageErrors, consoleErrors };
}

async function capture(
  scope: Page | ReturnType<Page["getByTestId"]>,
  testInfo: TestInfo,
  name: string,
) {
  const path = testInfo.outputPath(name);
  const screenshot = await scope.screenshot({ animations: "disabled", path });
  await testInfo.attach(name, { contentType: "image/png", path });
  return screenshot;
}

async function afterAnimationFrame(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

async function saturatedPixelCount(page: Page, screenshot: Buffer): Promise<number> {
  return page.evaluate(async (encoded) => {
    const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${encoded}`)).blob());
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Screenshot decoder unavailable");
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let count = 0;
    for (let offset = 0; offset < pixels.length; offset += 4) {
      const channels = [pixels[offset], pixels[offset + 1], pixels[offset + 2]];
      // Selected scenes retain quiet, blended points (RGB spread ~39).
      // Include those real marks without counting the neutral frame/background.
      if (Math.max(...channels) - Math.min(...channels) > 20) count++;
    }
    return count;
  }, screenshot.toString("base64"));
}

function makeSyntheticScene() {
  const nodeId = (index: number) => `context:${String(index).padStart(4, "0")}`;
  return {
    schema_version: "workspace-graph-scene.v1",
    workspace_scope_considered: true,
    head_mode: "published",
    graph_revision: "synthetic-2k-frontend-only-revision",
    generated_at: "2026-09-28T00:00:00+00:00",
    algorithm_version: "synthetic-frontend-only",
    metric_basis: "synthetic-frontend-only",
    representation: "exact",
    completeness: "complete",
    truncated: false,
    counts: {
      candidate_documents: NODE_COUNT,
      candidate_relations: EDGE_COUNT,
      rendered_documents: NODE_COUNT,
      rendered_clusters: 0,
      rendered_relations: EDGE_COUNT,
    },
    omission_reasons: [],
    nodes: Array.from({ length: NODE_COUNT }, (_, index) => {
      const entityId = `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
      return {
        grain: "document",
        id: nodeId(index),
        entity_kind: "context",
        entity_id: entityId,
        label: `Virtual Document ${String(index).padStart(4, "0")}`,
        path: `/context?target=${entityId}`,
        primary_folder_id: "00000000-0000-4000-8000-000000000011",
        primary_folder_label: "Synthetic folder",
        reference_count: 2,
        evidence_count: 0,
        also_appears_in: [],
      };
    }),
    edges: Array.from({ length: EDGE_COUNT }, (_, index) => {
      const source = index % NODE_COUNT;
      const target = (source + 1 + Math.floor(index / NODE_COUNT) * 73) % NODE_COUNT;
      return {
        id: `edge:${String(index).padStart(5, "0")}`,
        source: nodeId(source),
        target: nodeId(target),
        relation_kind: "wikilink",
        label: "links to",
        layer: "knowledge",
        direction: "directed",
        aggregate: false,
        relation_count: 1,
        cross_folder: false,
      };
    }),
    facet_counts: {
      document_kinds: [{ value: "context", label: "Context", count: NODE_COUNT }],
      relation_kinds: [{ value: "wikilink", label: "Wiki link", count: EDGE_COUNT }],
      folders: [{
        value: "00000000-0000-4000-8000-000000000011",
        label: "Synthetic folder",
        count: NODE_COUNT,
      }],
      quick_filters: [],
    },
    expansion_cursor: null,
    warnings: [],
    capabilities: {
      expand: false,
      evidence: false,
      export: true,
      directory: true,
      compare: false,
      growth: false,
      provenance: false,
    },
  };
}

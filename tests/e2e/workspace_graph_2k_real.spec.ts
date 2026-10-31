import { expect, test, type Locator, type Page, type Request, type TestInfo } from "@playwright/test";
import { writeFile } from "node:fs/promises";

import type { GraphNode, GraphQueryPayload } from "../../frontend/src/shared/api/graph";
import type { WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";
import { backendBaseUrl, currentWorkspace, login } from "./auth";
import { isResizeObserverDeferralReport } from "./fixtures";

const GRAPH_2K_OWNER_EMAIL = "e2e-graph-2k-owner@example.com";
const DOCUMENT_COUNT = 2_000;
const LINK_RELATION_COUNT = 5_057;
const COMBINED_RELATION_COUNT = 5_929;
const COMBINED_CONNECTED_DOCUMENT_COUNT = 1_928;
const OVERVIEW_PATH = "/graph/overview/query";

test.describe("Workspace Knowledge Map real-API 2k (opt-in)", () => {
  // Local runs opt in; the PR CI E2E step enables this flag automatically.
  // The isolated DB fixture and browser cases require the same switch.
  test.skip(
    process.env.DTXWIKI_E2E_GRAPH_2K !== "1",
    "Set DTXWIKI_E2E_GRAPH_2K=1 to seed and run the full-stack 2k case",
  );
  test.use({ viewport: { width: 1440, height: 900 } });

  test("renders exactly 2k real documents and their links in WebGL2", async ({
    page,
  }, testInfo) => {
    test.setTimeout(180_000);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await login(page.request, GRAPH_2K_OWNER_EMAIL);
    const diagnostics = watchBrowserDiagnostics(page);

    // No page.route() or response fulfillment: the browser reads the corpus
    // from the isolated SQLite-backed production overview endpoint.
    const sceneResponse = page.waitForResponse((response) =>
      response.url().includes(OVERVIEW_PATH)
      && response.request().method() === "POST");
    await page.goto("/graph");
    const combinedResponse = await sceneResponse;
    if (!combinedResponse.ok()) {
      throw new Error(`Knowledge Map API ${combinedResponse.status()}: ${(await combinedResponse.text()).slice(0, 500)}`);
    }
    const request = combinedResponse.request().postDataJSON() as {
      lens: string;
      budget: { documents: number; relations: number };
    };
    expect(request.lens).toBe("combined");
    expect(request.budget).toEqual({ documents: DOCUMENT_COUNT, relations: 6_000 });
    const combined = (await combinedResponse.json() as { data: WorkspaceGraphScene }).data;
    expect(combined.representation).toBe("exact");
    expect(combined.completeness).toBe("complete");
    expect(combined.truncated).toBe(false);
    expect(combined.counts).toMatchObject({
      candidate_documents: DOCUMENT_COUNT,
      candidate_relations: COMBINED_RELATION_COUNT,
      rendered_documents: DOCUMENT_COUNT,
      rendered_clusters: 0,
      rendered_relations: COMBINED_RELATION_COUNT,
    });
    expect(combined.nodes).toHaveLength(DOCUMENT_COUNT);
    expect(combined.edges).toHaveLength(COMBINED_RELATION_COUNT);
    expectCurrentFolderTopology(combined);
    expect(combined.edges.some((edge) => edge.relation_kind === "wikilink")).toBe(true);
    expect(combined.edges.some((edge) => edge.relation_kind.startsWith("citation_"))).toBe(true);
    expect(combined.edges.every((edge) => ["wikilink", "citation_page", "citation_source"]
      .includes(edge.relation_kind))).toBe(true);
    expect(new Set(combined.edges.flatMap((edge) => [edge.source, edge.target])).size)
      .toBe(COMBINED_CONNECTED_DOCUMENT_COUNT);
    await expect(page.getByLabel("Lens", { exact: true })).toHaveValue("combined");
    await expect(page.getByTestId("workspace-graph-node-list"))
      .toHaveAttribute("data-total-count", String(COMBINED_CONNECTED_DOCUMENT_COUNT));
    await expect(page.getByRole("button", { name: /^Unlinked \d+$/ }))
      .toHaveAttribute("title", "72 unlinked documents hidden from this map.");
    await expect(page.getByTestId("graph-canvas"))
      .toHaveAttribute("data-map-edge-count", String(COMBINED_RELATION_COUNT));
    await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]'))
      .toBeVisible({ timeout: 60_000 });
    await capture(page, testInfo, "real-api-2k-combined-default.png");

    // Preserve the historical exact Links proof as an explicit user choice,
    // rather than treating the new default as evidence for its old 5,057 count.
    const linksResponse = page.waitForResponse((response) =>
      response.url().includes(OVERVIEW_PATH)
      && response.request().method() === "POST"
      && response.request().postDataJSON()?.lens === "links");
    await page.getByLabel("Lens", { exact: true }).selectOption("links");
    const response = await linksResponse;
    expect(response.ok()).toBe(true);
    const body = await response.json() as { data: WorkspaceGraphScene };
    const scene = body.data;
    expect(scene.representation).toBe("exact");
    expect(scene.completeness).toBe("complete");
    expect(scene.truncated).toBe(false);
    expect(scene.counts).toMatchObject({
      candidate_documents: DOCUMENT_COUNT,
      candidate_relations: LINK_RELATION_COUNT,
      rendered_documents: DOCUMENT_COUNT,
      rendered_clusters: 0,
      rendered_relations: LINK_RELATION_COUNT,
    });
    expect(scene.nodes).toHaveLength(DOCUMENT_COUNT);
    expect(scene.edges).toHaveLength(LINK_RELATION_COUNT);
    expectCurrentFolderTopology(scene);

    const nodeList = page.getByTestId("workspace-graph-node-list");
    const edgeList = page.getByTestId("workspace-graph-edge-list");
    // The real corpus includes 252 unlinked documents. Keep the API's complete
    // 2K proof above, while the first viewport prioritizes its 1,748 connected
    // documents. The display toggle reveals the same authoritative 2K scene.
    await expect(nodeList).toHaveAttribute("data-total-count", "1748");
    await expect(page.getByRole("button", { name: /^Unlinked \d+$/ }))
      .toHaveAttribute("title", "252 unlinked documents hidden from this map.");
    const initialCanvas = page.getByTestId("graph-canvas");
    await expect(initialCanvas).toHaveAttribute("data-map-node-count", "1748");
    await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]')).toBeVisible({ timeout: 60_000 });
    await capture(page, testInfo, "real-api-2k-connected-first-viewport.png");
    await page.getByRole("button", { name: /^Unlinked \d+$/ }).click();
    await expect(nodeList).toHaveAttribute("data-total-count", String(DOCUMENT_COUNT));
    await expect(edgeList).toHaveAttribute("data-total-count", String(LINK_RELATION_COUNT));
    await expectBoundedRows(nodeList, edgeList);

    const canvas = page.getByTestId("graph-canvas");
    await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]'))
      .toBeVisible({ timeout: 60_000 });
    await expect(canvas).toHaveAttribute("data-map-node-count", String(DOCUMENT_COUNT));
    await expect(canvas).toHaveAttribute("data-map-edge-count", String(LINK_RELATION_COUNT));
    await expect(canvas).toHaveAttribute("data-map-community-count", /^[1-9]\d*$/);
    expect((await canvas.boundingBox())?.y).toBeLessThan(700);
    await afterAnimationFrame(page);
    const gpu = await canvas.evaluate((element) => {
      const canvases = element.querySelectorAll("canvas");
      const gl = canvases[0]?.getContext("webgl2");
      return {
        canvasCount: canvases.length,
        webgl2: Boolean(gl),
        contextLost: gl?.isContextLost() ?? true,
        glError: gl?.getError() ?? -1,
        width: canvases[0]?.width ?? 0,
        height: canvases[0]?.height ?? 0,
      };
    });
    expect(gpu).toMatchObject({
      canvasCount: 1,
      webgl2: true,
      contextLost: false,
      glError: 0,
    });
    expect(gpu.width).toBeGreaterThan(0);
    expect(gpu.height).toBeGreaterThan(0);
    await capture(page, testInfo, "real-api-2k-first-viewport.png");
    await capture(canvas, testInfo, "real-api-2k-overview-canvas.png");

    // Counts and a valid WebGL context cannot prove that relationships made
    // it into the native framebuffer. Keep the camera fixed while toggling
    // only this client-side display control, then compare pixels away from
    // node-colored marks against the links-off background.
    const nativeCanvas = canvas.locator("canvas").first();
    const linksOn = await capture(nativeCanvas, testInfo, "real-api-2k-native-links-on.png");
    await page.getByRole("button", { name: "Refine" }).click();
    const displayDialog = page.getByRole("dialog", { name: "Refine data" });
    const showRelationships = displayDialog.getByRole("checkbox", {
      name: "Show relationships",
    });
    await expect(showRelationships).toBeChecked();
    await showRelationships.uncheck();
    await page.keyboard.press("Escape");
    await afterAnimationFrame(page);
    const linksOff = await capture(nativeCanvas, testInfo, "real-api-2k-native-links-off.png");
    const pixelDiff = await compareNativeCanvasPixels(page, linksOn, linksOff);
    expect(pixelDiff.changedPixels).toBeGreaterThan(100);
    expect(pixelDiff.changedOnBackground).toBeGreaterThan(50);
    await page.getByRole("button", { name: "Refine" }).click();
    await showRelationships.check();
    await page.keyboard.press("Escape");
    await afterAnimationFrame(page);

    await nodeList.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    const lastNode = nodeList.locator('li[aria-posinset="2000"][data-graph-node-id]');
    await expect(lastNode).toBeVisible();
    const selectedLabel = await lastNode.locator("strong").textContent();
    expect(selectedLabel).toBeTruthy();
    const selectButton = lastNode.getByRole("button").first();
    await selectButton.click();
    await expect(selectButton).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".workspace-graph-details").getByRole("heading", {
      name: selectedLabel!,
      exact: true,
    })).toBeVisible();
    await expect(page.getByRole("button", { name: "Clear selection" })).toBeVisible();
    await capture(canvas, testInfo, "real-api-2k-selected-canvas.png");

    await edgeList.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await expect(edgeList.locator('li[aria-posinset="5057"][data-graph-edge-id]'))
      .toBeVisible();
    await expectBoundedRows(nodeList, edgeList);
    expect(diagnostics.pageErrors).toEqual([]);
    expect(diagnostics.consoleErrors).toEqual([]);
  });

  // The reader/history route is viewport-independent. The mobile journey also
  // covers Korean folder controls; desktop rendering is exercised above.
  for (const viewport of [{ width: 390, height: 844 }]) {
    test(`opens a real 2k Map document through Context Graph and back at ${viewport.width}px`, async ({ page }, testInfo) => {
      test.setTimeout(180_000);
      await page.setViewportSize(viewport);
      await page.emulateMedia({ reducedMotion: "reduce" });
      await login(page.request, GRAPH_2K_OWNER_EMAIL);
      const diagnostics = watchBrowserDiagnostics(page);
      const failedReads: number[] = [];
      page.on("response", (response) => {
        if (response.url().includes("/api/v1/workspaces/") && response.status() >= 400) {
          failedReads.push(response.status());
        }
      });

      const overviewResponse = page.waitForResponse((response) =>
        response.url().includes(OVERVIEW_PATH) && response.request().method() === "POST");
      await page.goto("/graph");
      const overview = await overviewResponse;
      expect(overview.ok()).toBe(true);
      expect(overview.request().postDataJSON().lens).toBe("combined");
      const scene = (await overview.json()).data as WorkspaceGraphScene;
      expectCurrentFolderTopology(scene);
      const workspaceId = new URL(overview.url()).pathname
        .match(/\/workspaces\/([^/]+)\/graph\/overview\/query$/)?.[1];
      if (!workspaceId) throw new Error("Overview response did not use the real workspace-scoped route");
      expect(scene.counts.candidate_documents).toBe(DOCUMENT_COUNT);
      expect(scene.counts.candidate_relations).toBe(COMBINED_RELATION_COUNT);
      expect(scene.representation).toBe("exact");
      expect(scene.completeness).toBe("complete");
      expect(scene.truncated).toBe(false);
      const connectedIds = new Set(scene.edges.flatMap((edge) => [edge.source, edge.target]));
      expect(connectedIds.size).toBe(COMBINED_CONNECTED_DOCUMENT_COUNT);
      const contextDocument = scene.nodes.find((node) => node.grain === "document"
        && node.entity_kind === "context" && connectedIds.has(node.id));
      if (!contextDocument || contextDocument.grain !== "document") throw new Error("No real linked Context document in 2K fixture");

      // Exercise optional metadata without replacing document geometry in both languages,
      // then preserve the original EN reader/history/keyboard journey below.
      const folderLanguage = viewport.width === 390 ? "ko" : "en";
      if (folderLanguage === "ko") {
        await page.getByRole("button", { name: "Switch language to 한국어" }).click();
        await expect(page.locator("html")).toHaveAttribute("lang", "ko");
      }
      await expectRealFolderContext(page, scene, contextDocument.primary_folder_id, testInfo, folderLanguage);
      if (folderLanguage === "ko") {
        await page.getByRole("button", { name: /English.*언어 변경/ }).click();
        await expect(page.locator("html")).toHaveAttribute("lang", "en");
      }

      await page.getByRole("searchbox", { name: "Find in visible map" }).fill(contextDocument.label);
      await page.getByRole("button", { name: "Open Map details sheet", exact: true }).click();
      const row = page.getByTestId("workspace-graph-node-list")
        .locator(`li[data-graph-node-id="${contextDocument.id}"]`);
      await row.getByRole("button").first().click();
      const detail = page.locator(".workspace-graph-details");
      await expect(detail.getByRole("heading", { name: contextDocument.label, exact: true })).toBeVisible();
      await detail.getByRole("link", { name: "Open", exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`/workspaces/${workspaceId}/context/pages/${contextDocument.entity_id}`));
      await expect(page.getByRole("heading", { name: contextDocument.label, exact: true }).first()).toBeVisible();
      const tabs = page.getByRole("tablist", { name: "Context reader views" });
      await expect(tabs.getByRole("tab")).toHaveCount(4);

      const currentResponse = page.waitForResponse((response) =>
        response.url().endsWith(`/workspaces/${workspaceId}/graph/query`)
        && response.request().method() === "POST"
        && response.request().postDataJSON().time?.mode === "current");
      await tabs.getByRole("tab", { name: /^Graph/ }).click();
      const current = await currentResponse;
      expect(current.ok()).toBe(true);
      const local = (await current.json()).data as GraphQueryPayload;
      expect(local.query).toMatchObject({ depth: 2, budget: { nodes: 250, edges: 750 } });
      expect(local.nodes.some((node) => node.entity_id === contextDocument.entity_id)).toBe(true);
      expect(local.edges.length).toBeGreaterThan(0);
      const graphPanel = page.getByRole("tabpanel", { name: /^Graph/ });
      const localCanvas = graphPanel.getByTestId("graph-canvas");
      await expect(localCanvas.locator("..")).toHaveAttribute("data-renderer-status", "ready");
      await expect(localCanvas.locator("canvas.sigma-nodes")).toBeVisible();
      await expectLocalSceneNavigation(page, graphPanel, local, workspaceId, testInfo, "en");
      await capture(page, testInfo, `real-api-2k-context-${viewport.width}.png`);

      const actionsResponse = page.waitForResponse((response) =>
        response.url().endsWith(`/workspaces/${workspaceId}/graph/query`)
        && response.request().method() === "POST"
        && response.request().postDataJSON().time?.mode === "action");
      await graphPanel.getByLabel("History mode").selectOption("action");
      const actions = await actionsResponse;
      expect(actions.ok()).toBe(true);
      const actionScene = (await actions.json()).data as GraphQueryPayload;
      expect(actionScene.edges.some((edge) => edge.edge_kind === "created")).toBe(true);
      await expect(localCanvas.locator("..")).toHaveAttribute("data-renderer-status", "ready");
      await capture(page, testInfo, `real-api-2k-actions-${viewport.width}.png`);

      await tabs.getByRole("tab", { name: /^History/ }).click();
      await expect(page.getByRole("tabpanel", { name: /^History/ })
        .locator('[aria-current="page"]')).toHaveCount(1);
      // Reader tabs are real history entries. Back traverses History ->
      // Graph -> View before returning to the Map document origin.
      await page.goBack();
      await expect(tabs.getByRole("tab", { name: /^Graph/ })).toHaveAttribute("aria-selected", "true");
      await page.goBack();
      await expect(tabs.getByRole("tab", { name: "View", exact: true })).toHaveAttribute("aria-selected", "true");
      const backToMap = page.waitForResponse((response) =>
        response.url().includes(OVERVIEW_PATH) && response.request().method() === "POST", { timeout: 30_000 });
      await page.goBack();
      const restoredResponse = await backToMap;
      expect(restoredResponse.ok()).toBe(true);
      expect(restoredResponse.request().postDataJSON().lens).toBe("combined");
      const restoredScene = (await restoredResponse.json()).data as WorkspaceGraphScene;
      expect(restoredScene.counts).toEqual(scene.counts);
      expect(restoredScene.nodes.map((node) => node.id).sort()).toEqual(scene.nodes.map((node) => node.id).sort());
      expect(restoredScene.edges.map((edge) => edge.id).sort()).toEqual(scene.edges.map((edge) => edge.id).sort());
      await expect(page.getByRole("heading", { name: "Knowledge Map", exact: true })).toBeVisible();
      // Citations connect 180 documents that are unlinked in Links alone.
      // The first test retains the separate Links count of 1,748.
      await expect(page.getByTestId("workspace-graph-node-list"))
        .toHaveAttribute("data-total-count", String(COMBINED_CONNECTED_DOCUMENT_COUNT));
      await expect(page.getByRole("searchbox", { name: "Find in visible map" })).toHaveValue("");
      const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      expect(horizontalOverflow).toBe(false);
      expect(failedReads).toEqual([]);
      expect(diagnostics.pageErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
    });
  }

  test("Local scene navigation uses the real 2k corpus at 390px Korean forced colors", async ({ page }, testInfo) => {
    test.setTimeout(180_000);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ reducedMotion: "reduce", forcedColors: "active" });
    await page.addInitScript(() => localStorage.setItem("dtxwiki.uiLanguage", "ko"));
    await login(page.request, GRAPH_2K_OWNER_EMAIL);
    const diagnostics = watchBrowserDiagnostics(page);
    const workspace = await currentWorkspace(page.request);
    // Read the same isolated 2K corpus through the production API; this is a
    // Local-only UI case, not another Map journey or a fulfilled browser route.
    const overview = await page.request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}${OVERVIEW_PATH}`,
      { data: {
        schema_version: "workspace-graph-overview.v1", head_mode: "published", lens: "combined",
        document_kinds: ["context", "library"], quick_filters: [],
        folders: { include: [], exclude: [], only: [] }, time: { basis: "document_updated" },
        budget: { documents: DOCUMENT_COUNT, relations: 6_000 },
      } },
    );
    expect(overview.ok()).toBe(true);
    const scene = (await overview.json()).data as WorkspaceGraphScene;
    expect(scene.counts.candidate_documents).toBe(DOCUMENT_COUNT);
    expect(scene.counts.candidate_relations).toBe(COMBINED_RELATION_COUNT);
    const linked = new Set(scene.edges.flatMap((edge) => [edge.source, edge.target]));
    const document = scene.nodes.find((node) => node.grain === "document"
      && node.entity_kind === "context" && linked.has(node.id));
    if (!document || document.grain !== "document") throw new Error("No real linked Context document in 2K fixture");
    await page.goto(`/workspaces/${workspace.id}/context/pages/${document.entity_id}`);
    const currentResponse = page.waitForResponse((response) =>
      response.url().endsWith(`/workspaces/${workspace.id}/graph/query`)
      && response.request().method() === "POST"
      && response.request().postDataJSON().time?.mode === "current");
    await page.getByRole("tablist", { name: "Context 리더 보기" })
      .getByRole("tab", { name: /^그래프/ }).click();
    const current = await currentResponse;
    expect(current.ok()).toBe(true);
    const local = (await current.json()).data as GraphQueryPayload;
    expect(local.query).toMatchObject({ depth: 2, budget: { nodes: 250, edges: 750 } });
    const panel = page.getByRole("tabpanel", { name: /^그래프/ });
    const canvas = panel.getByTestId("graph-canvas");
    await expect(canvas.locator("..")).toHaveAttribute("data-renderer-status", "ready");
    await expect(canvas.locator("canvas.sigma-nodes")).toBeVisible();
    expect(await page.evaluate(() => matchMedia("(forced-colors: active)").matches)).toBe(true);
    expect(await page.evaluate(() => localStorage.getItem("dtxwiki.uiLanguage"))).toBe("ko");
    await expectLocalSceneNavigation(page, panel, local, workspace.id, testInfo, "ko");
    await capture(page, testInfo, "real-api-2k-local-ko-forced-colors-390.png");
    expect(diagnostics.pageErrors).toEqual([]);
    expect(diagnostics.consoleErrors).toEqual([]);
  });
});

const SCENE_NAVIGATION_LABELS = {
  en: {
    members: "Members in this scene", folders: "Folders in this scene",
    neighbors: "Direct neighbors in this scene", empty: "Select a node or relationship to inspect it.",
  },
  ko: {
    members: "이 장면의 소속 항목", folders: "이 장면의 소속 폴더",
    neighbors: "이 장면의 직접 이웃", empty: "확인할 노드 또는 관계를 선택하세요.",
  },
} as const;

const FOLDER_MAP_LABELS = {
  en: {
    view: "View",
    context: "Folder context", openCanvas: "Open canvas fullscreen", canvas: "Knowledge Map canvas",
    closeCanvas: "Close canvas", closeDetails: "Close folder details", export: "Export visible map",
    fit: "Fit graph", pause: "Pause layout", resume: "Resume layout", clear: "Clear selection",
    search: "Find in visible map", details: "Folder members and relationships",
  },
  ko: {
    view: "보기",
    context: "폴더 보조 정보", openCanvas: "캔버스 전체 화면으로 열기", canvas: "지식 지도 캔버스",
    closeCanvas: "캔버스 닫기", closeDetails: "폴더 상세 닫기", export: "표시된 지도 내보내기",
    fit: "그래프 맞춤", pause: "레이아웃 일시 중지", resume: "레이아웃 다시 시작", clear: "선택 해제",
    search: "표시된 지도에서 찾기", details: "폴더 소속과 관계",
  },
} as const;

type MapReadback = Awaited<ReturnType<typeof readMapReadback>>;
type MapPoint = MapReadback["labels"][number];

/** Existing GPU-tracked label DOM and native D3 camera, never a renderer hook. */
async function readMapReadback(canvas: Locator) {
  return canvas.evaluate((element) => {
    const native = element.querySelector<HTMLCanvasElement>("canvas");
    const zoom = (native as (HTMLCanvasElement & {
      __zoom?: { k: number; x: number; y: number };
    }) | null)?.__zoom;
    const labels = Array.from(element.querySelectorAll<HTMLElement>(
      "[data-workspace-map-labels] span[data-node-id]",
    )).filter((label) => label.dataset.fullTitle !== "true" && getComputedStyle(label).display !== "none")
      .map((label) => {
        const transform = new DOMMatrixReadOnly(getComputedStyle(label).transform);
        // Shipped non-full labels are placed at GPU point+(8,-9).
        return { id: label.dataset.nodeId!, x: transform.m41 - 8, y: transform.m42 + 9 };
      }).filter((point) => point.id && Number.isFinite(point.x) && Number.isFinite(point.y))
      .sort((left, right) => left.id.localeCompare(right.id));
    const bounds = element.getBoundingClientRect();
    return {
      camera: zoom ? { k: zoom.k, x: zoom.x, y: zoom.y } : null,
      labels, width: bounds.width, height: bounds.height,
      nodes: Number((element as HTMLElement).dataset.mapNodeCount),
      edges: Number((element as HTMLElement).dataset.mapEdgeCount),
    };
  });
}

async function readStoredMapState(page: Page, revision: string | null = null) {
  return page.evaluate((expectedRevision) => {
    // Read only graph-owned state in the isolated actor's browser, not auth
    // storage or unrelated application data. This is observational, not setup.
    const keys = Object.keys(sessionStorage).filter((entry) =>
      entry.startsWith("dtxwiki:workspace-graph:v2:") && entry.endsWith(expectedRevision === null
        ? ":latest" : encodeURIComponent(expectedRevision)));
    if (keys.length !== 1) throw new Error("Expected one isolated actor/workspace Map state");
    const stored = JSON.parse(sessionStorage.getItem(keys[0])!);
    if (expectedRevision !== null && stored.revision !== expectedRevision) throw new Error("Map state revision mismatch");
    const state = stored.state;
    return { selectedId: state.selectedId as string | null, pinnedIds: [...state.pinnedIds].sort() as string[],
      pinnedPositions: state.pinnedPositions as Record<string, { x: number; y: number }>,
      viewport: state.viewport as { zoom: number; pan: { x: number; y: number } } | null };
  }, revision);
}

async function settleMapReadback(page: Page, canvas: Locator) {
  // A preceding explicit drag/center can outpace the bounded5Hz label reader.
  // Compare fresh GPU-backed frames, not a cached pre-action DOM transform.
  await afterAnimationFrame(page);
  await page.waitForTimeout(250);
  const before = await readMapReadback(canvas);
  const stateBefore = await readStoredMapState(page);
  const nativeIdentity = await canvas.locator("canvas").elementHandle();
  if (!nativeIdentity) throw new Error("No native canvas before neutral focus");
  const panel = canvas.locator("..").locator("..");
  const fit = panel.getByRole("button", { name: /^(Fit graph|그래프 맞춤)$/, exact: true });
  const resume = panel.getByRole("button", { name: /^(Resume layout|레이아웃 다시 시작)$/, exact: true });
  await expect(resume).toBeEnabled();
  // Clear only transient list focus through a shipped toolbar focus action.
  // Clear selection intentionally returns focus to its original list row;
  // without this blur, Links ON could retain a focus star while Refine/Links
  // OFF clears it. Do NOT click Fit or clear a deliberately selected document.
  await fit.focus();
  await expect(fit).toBeFocused();
  await canvas.locator("canvas").scrollIntoViewIfNeeded();
  // The fullscreen map now reaches the viewport corners. A shipped toolbar
  // button is a real neutral pointer target above the canvas at every size.
  await fit.hover();
  await afterAnimationFrame(page);
  // Production labels read back at5Hz. Observe a fresh settled GPU label frame.
  await page.waitForTimeout(250);
  const after = await readMapReadback(canvas);
  expect(await canvas.locator("canvas").evaluate((element, original) => element === original, nativeIdentity)).toBe(true);
  await nativeIdentity.dispose();
  expect(after.nodes).toBe(before.nodes);
  expect(after.edges).toBe(before.edges);
  expect(after.width).toBe(before.width);
  expect(after.height).toBe(before.height);
  expect(before.camera).not.toBeNull();
  expect(after.camera).not.toBeNull();
  expect(after.camera!.k).toBeCloseTo(before.camera!.k, 5);
  expect(after.camera!.x).toBeCloseTo(before.camera!.x, 2);
  expect(after.camera!.y).toBeCloseTo(before.camera!.y, 2);
  const stateAfter = await readStoredMapState(page);
  // Saved viewport delivery is RAF-batched; camera itself is checked above.
  expect(stateAfter.selectedId).toBe(stateBefore.selectedId);
  expect(stateAfter.pinnedIds).toEqual(stateBefore.pinnedIds);
  expect(stateAfter.pinnedPositions).toEqual(stateBefore.pinnedPositions);
  await expect(resume).toBeEnabled();
  const previousPoints = new Map(before.labels.map((point) => [point.id, point]));
  const common = after.labels.filter((point) => previousPoints.has(point.id));
  expect(common.length).toBeGreaterThan(0);
  expect(common.every((point) => {
    const previous = previousPoints.get(point.id)!;
    return Math.hypot(point.x - previous.x, point.y - previous.y) < 0.25;
  }), "Neutral toolbar focus must not move actual observed document positions").toBe(true);
  return after;
}

async function waitStableMapReadback(page: Page, canvas: Locator) {
  let previous: MapReadback | null = null;
  let stable = 0;
  await expect.poll(async () => {
    const current = await readMapReadback(canvas);
    const oldPoints = new Map(previous?.labels.map((point) => [point.id, point]) ?? []);
    const unchanged = previous?.camera && current.camera
      && Math.abs(current.camera.k - previous.camera.k) < 0.000001
      && Math.abs(current.camera.x - previous.camera.x) < 0.001
      && Math.abs(current.camera.y - previous.camera.y) < 0.001
      && current.labels.length >= 3 && current.labels.length === previous.labels.length
      && current.labels.every((point) => {
        const old = oldPoints.get(point.id);
        return old && Math.hypot(point.x - old.x, point.y - old.y) < 0.05;
      });
    stable = unchanged ? stable + 1 : 0;
    previous = current;
    return stable;
  }, { intervals: [100], timeout: 5_000 }).toBeGreaterThanOrEqual(3);
  return settleMapReadback(page, canvas);
}

function expectSameMapGeometry(before: MapReadback, after: MapReadback) {
  expect(after.nodes).toBe(before.nodes);
  expect(after.edges).toBe(before.edges);
  expect(after.width).toBe(before.width);
  expect(after.height).toBe(before.height);
  expect(before.camera).not.toBeNull();
  expect(after.camera).not.toBeNull();
  expect(after.camera!.k).toBeCloseTo(before.camera!.k, 5);
  expect(after.camera!.x).toBeCloseTo(before.camera!.x, 2);
  expect(after.camera!.y).toBeCloseTo(before.camera!.y, 2);
  const points = new Map(after.labels.map((point) => [point.id, point]));
  expect(before.labels.length).toBeGreaterThanOrEqual(3);
  // Every previously observed label is checked. This is not an all2K GPU
  // coordinate claim; native identity and column tests complement this check.
  expect(before.labels.every((point) => {
    const current = points.get(point.id);
    return current !== undefined && Math.hypot(current.x - point.x, current.y - point.y) < 0.25;
  }), "Paused visible document coordinates must survive a metadata-only UI action").toBe(true);
}

/** Camera preparation uses shipped controls/native wheel, never graph hooks. */
async function zoomMapForObservation(
  page: Page, canvas: Locator, direction: "in" | "out", anchor?: { x: number; y: number },
) {
  const before = await readMapReadback(canvas);
  const stateBefore = await readStoredMapState(page);
  const native = canvas.locator("canvas");
  const identity = await native.elementHandle();
  if (!identity) throw new Error("No native Map canvas before observation zoom");
  try {
    if (anchor) {
      const bounds = await native.boundingBox();
      if (!bounds) throw new Error("No native Map bounds for pointer-centered zoom");
      // Keep the intended point/corridor onscreen when a centered toolbar
      // zoom would push it away. This is ordinary native user camera input.
      await page.mouse.move(bounds.x + anchor.x, bounds.y + anchor.y);
      await page.mouse.wheel(0, direction === "in" ? -120 : 120);
    } else {
      const panel = canvas.locator("..").locator("..");
      await panel.getByRole("button", { name: direction === "in"
        ? /^(Zoom in|확대)$/ : /^(Zoom out|축소)$/, exact: true }).click();
    }
    const after = await waitStableMapReadback(page, canvas);
    expect(await native.evaluate((element, original) => element === original, identity)).toBe(true);
    expect(after.nodes).toBe(before.nodes);
    expect(after.edges).toBe(before.edges);
    const stateAfter = await readStoredMapState(page);
    expect(stateAfter.selectedId).toBe(stateBefore.selectedId);
    expect(stateAfter.pinnedIds).toEqual(stateBefore.pinnedIds);
    expect(stateAfter.pinnedPositions).toEqual(stateBefore.pinnedPositions);
    return after;
  } finally {
    await identity.dispose();
  }
}

function observedCrossFolderPairs(readback: MapReadback, scene: WorkspaceGraphScene) {
  const points = new Map(readback.labels.map((point) => [point.id, point]));
  return scene.edges.filter((edge) => edge.relation_kind === "wikilink" && edge.cross_folder
    && points.has(edge.source) && points.has(edge.target)).map((edge) => {
    const source = points.get(edge.source)!; const target = points.get(edge.target)!;
    return { edge, source, target, length: Math.hypot(source.x - target.x, source.y - target.y) };
  }).sort((left, right) => right.edge.relation_count - left.edge.relation_count || left.length - right.length);
}

async function prepareNativeCrossFolderCorridor(
  page: Page, canvas: Locator, scene: WorkspaceGraphScene, testInfo: TestInfo, suffix: string,
) {
  let readback = await settleMapReadback(page, canvas);
  const navigation: { camera: MapReadback["camera"]; wireEdgeId: string | null;
    length: number | null; direction: "in" | "out" | null }[] = [];
  // Find an inspectable real API wire edge BEFORE the fixed-camera oracle.
  // Its length and pixel assertions are not retried or relaxed here.
  for (let attempt = 0; attempt <= 6; attempt += 1) {
    const pairs = observedCrossFolderPairs(readback, scene);
    const eligible = pairs.find((pair) => pair.length >= 60 && pair.length <= 350);
    if (eligible) {
      navigation.push({ camera: readback.camera, wireEdgeId: eligible.edge.id,
        length: eligible.length, direction: null });
      await attachJson(testInfo, `real-api-2k-cross-folder-preparation-${suffix}.json`, {
        entrypoint: "real Zoom controls/native pointer-centered wheel before the fixed-camera corridor oracle",
        navigation, expectedWireEdgeId: eligible.edge.id, endpoints: { source: eligible.source, target: eligible.target },
      });
      return eligible.edge.id;
    }
    const pair = pairs[0];
    const direction = pair && pair.length > 350 ? "out" : "in";
    navigation.push({ camera: readback.camera, wireEdgeId: pair?.edge.id ?? null,
      length: pair?.length ?? null, direction });
    if (attempt === 6) break;
    readback = await zoomMapForObservation(page, canvas, direction, pair
      ? { x: (pair.source.x + pair.target.x) / 2, y: (pair.source.y + pair.target.y) / 2 } : undefined);
  }
  await attachJson(testInfo, `real-api-2k-cross-folder-preparation-failed-${suffix}.json`, { navigation, readback });
  throw new Error("No actual visible 60–350px cross-folder wire edge after bounded real camera preparation");
}

async function prepareNativeDocumentDrag(
  page: Page, canvas: Locator, scene: WorkspaceGraphScene, dragDelta: { x: number; y: number },
  testInfo: TestInfo, suffix: string,
) {
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  const ordinaryLabelWidth = (id: string) => {
    const title = nodes.get(id)?.label ?? "";
    return Math.min(144, (title.length <= 24 ? title.length : 25) * 7 + 4);
  };
  const isDragCandidate = (point: MapPoint, readback: MapReadback) => {
    const node = nodes.get(point.id);
    if (node?.grain !== "document" || node.entity_kind !== "context") return false;
    const width = ordinaryLabelWidth(point.id);
    // Retain the complete ordinary-label and gesture margins. A hover title
    // alone is clamped and must never be used as inferred GPU coordinates.
    if (point.x <= 30 || point.y <= 30
      || point.x >= readback.width - (width + 8 + dragDelta.x + 5)
      || point.y >= readback.height - (18 + dragDelta.y + 5)) return false;
    const labelX = point.x + 8 + dragDelta.x;
    const labelY = point.y - 9 + dragDelta.y;
    return !readback.labels.some((other) => other.id !== point.id
      && labelX < other.x + 8 + ordinaryLabelWidth(other.id) + 5
      && labelX + width + 5 > other.x + 8
      && Math.abs(labelY - (other.y - 9)) < 18);
  };
  let before = await settleMapReadback(page, canvas);
  let expectedId: string | null = null;
  const preparation: { camera: MapReadback["camera"]; expectedId: string | null;
    point: MapPoint | null; nativeHoverIds: string[] }[] = [];
  for (let attempt = 0; attempt <= 6; attempt += 1) {
    const point: MapPoint | undefined = expectedId === null ? before.labels.find((candidate) => isDragCandidate(candidate, before))
      : before.labels.find((candidate) => candidate.id === expectedId && isDragCandidate(candidate, before));
    if (point && before.labels.length >= 3) {
      expectedId ??= point.id;
      const bounds = await canvas.locator("canvas").boundingBox();
      if (!bounds) throw new Error("No native drag bounds");
      await page.mouse.move(bounds.x + point.x, bounds.y + point.y);
      // Native GPU picking and the bounded label readback both must publish
      // this ORIGINAL intended source before any mouse down can create a pin.
      await page.waitForTimeout(300);
      const nativeHoverIds = await canvas.locator("[data-workspace-map-labels] [data-full-title=true][data-node-id]")
        .evaluateAll((labels) => labels.filter((label) => getComputedStyle(label).display !== "none")
          .map((label) => (label as HTMLElement).dataset.nodeId!));
      preparation.push({ camera: before.camera, expectedId, point, nativeHoverIds });
      if (nativeHoverIds.includes(expectedId)) {
        await expect(canvas.locator(`[data-workspace-map-labels] [data-full-title=true][data-node-id="${expectedId}"]`))
          .toBeVisible();
        await attachJson(testInfo, `real-api-2k-native-drag-preparation-${suffix}.json`, {
          entrypoint: "real camera zoom, fresh ordinary GPU label, expected-source native hover BEFORE mouse down",
          expectedId, preparation, before,
        });
        return { dragPoint: point, dragBounds: bounds, beforeDrag: before };
      }
    } else preparation.push({ camera: before.camera, expectedId, point: point ?? null, nativeHoverIds: [] });
    if (attempt === 6) break;
    // A wrong native hover is only a setup observation, never a wrong drag
    // followed by accepting whichever source happened to become pinned.
    before = await zoomMapForObservation(page, canvas, "in", point);
  }
  await attachJson(testInfo, `real-api-2k-native-drag-preparation-failed-${suffix}.json`, { expectedId, preparation, before });
  throw new Error(`No isolated expected Context GPU hover before native drag after bounded zoom: ${expectedId ?? "none"}`);
}

async function expectRealFolderContext(
  page: Page, scene: WorkspaceGraphScene, preferredFolderId: string | null,
  testInfo: TestInfo, language: keyof typeof FOLDER_MAP_LABELS,
) {
  const labels = FOLDER_MAP_LABELS[language];
  const workspaceId = new URL(page.url()).pathname.match(/^\/workspaces\/([^/]+)\/graph$/)?.[1];
  if (!workspaceId) throw new Error("Folder context requires the canonical Workspace Map route");
  const mobile = page.viewportSize()!.width === 390;
  const suffix = `${language}-${page.viewportSize()!.width}`;
  const topology = scene.folder_topology;
  if (!topology) throw new Error("Real folder context requires current visible topology");
  const connected = new Set(scene.edges.flatMap((edge) => [edge.source, edge.target]));
  const memberships = topology.memberships.filter((row) => connected.has(row.node_id));
  const folders = new Map(topology.folders.map((folder) => [folder.id, folder]));
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  const visibleFolders = new Set<string>();
  for (const membership of memberships) {
    let id = membership.folder_id;
    while (id !== null && !visibleFolders.has(id)) {
      visibleFolders.add(id);
      const folder = folders.get(id);
      if (!folder) throw new Error("Visible primary ancestor is absent");
      id = folder.parent_folder_id;
    }
  }
  const folderId = preferredFolderId ?? memberships.find((row) => row.folder_id !== null)?.folder_id;
  if (!folderId || !visibleFolders.has(folderId)) throw new Error("No visible primary folder");
  const folder = folders.get(folderId)!;
  const memberIds = memberships.filter((row) => row.folder_id === folderId).map((row) => row.node_id).sort();
  expect(memberIds.length).toBeGreaterThan(0);
  expect(memberIds.length).toBeLessThanOrEqual(2_000);
  const originalTheme = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  // This shipped DOM ID also permits read-only value checks while the mobile
  // input is in the inert underlay. Fill actions occur only before/after dialog.
  const search = page.locator("#workspace-graph-visible-search");
  await expect(search).toHaveAccessibleName(labels.search);
  let overviewRequests = 0;
  const recordRequest = (request: Request) => {
    if (request.method() === "POST" && request.url().includes(OVERVIEW_PATH)) overviewRequests += 1;
  };
  page.on("request", recordRequest);
  try {
    // Search is outside mobile fullscreen. Set it before opening the real
    // dialog, rather than interacting with its inert underlay.
    if (mobile) await search.fill("note");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    if (mobile) await page.getByRole("button", { name: labels.openCanvas, exact: true }).click();
    const scope = mobile ? page.getByRole("dialog", { name: labels.canvas, exact: true }) : page;
    const canvas = scope.getByTestId("graph-canvas");
    const native = canvas.locator("canvas");
    const toggle = scope.locator("button.workspace-graph-folder-toggle");
    const clickFolderToggle = async () => {
      if (!mobile) {
        const view = page.locator(".workspace-graph-view-options");
        if (await view.getAttribute("open") === null) await view.locator("summary").click();
      }
      await expect(toggle).toHaveAccessibleName(labels.context);
      await toggle.click();
      // Leave the View disclosure before navigating the sidebar beneath it.
      // Exercise its shipped Escape/focus behavior rather than force-clicking
      // a folder index that the still-open popup legitimately overlays.
      if (!mobile) {
        await toggle.press("Escape");
        await expect(page.locator(".workspace-graph-view-options")).not.toHaveAttribute("open", "");
        await expect(page.locator(".workspace-graph-view-options > summary")).toBeFocused();
      }
    };
    await expect(canvas.locator("..")).toHaveAttribute("data-renderer-status", "ready");
    await expect(native).toHaveCount(1);
    await expect(native).toBeVisible();
    await expect(canvas).toHaveAttribute("data-map-node-count", String(connected.size));
    await expect(canvas).toHaveAttribute("data-map-edge-count", String(scene.edges.length));
    await expect(canvas).toHaveAttribute("data-map-community-count", /^[1-9]\d*$/);
    await expect(canvas.locator("[data-workspace-map-folder-islands]")).toHaveCount(0);
    await expect(canvas).not.toHaveAttribute("data-map-folder-count");
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    if (mobile) {
      const bounds = await canvas.boundingBox();
      expect(bounds).not.toBeNull();
      expect(bounds!.y).toBeGreaterThanOrEqual(0);
      expect(bounds!.height).toBeGreaterThan(160);
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
    }
    await scope.getByRole("button", { name: labels.pause, exact: true }).click();
    await expect(scope.getByRole("button", { name: labels.resume, exact: true })).toBeEnabled();
    await scope.getByRole("button", { name: labels.fit, exact: true }).click();
    await expect.poll(async () => (await readMapReadback(canvas)).labels.length,
      { intervals: [100], timeout: 5_000 }).toBeGreaterThanOrEqual(3);
    const nativeIdentity = await native.elementHandle();
    if (!nativeIdentity) throw new Error("No actual native canvas");

    const assertNativeIdentity = async () => {
      expect(await nativeIdentity.evaluate((element) => element.isConnected)).toBe(true);
      expect(await native.evaluate((element, original) => element === original, nativeIdentity)).toBe(true);
      await expect(canvas).toHaveAttribute("data-map-node-count", String(connected.size));
      await expect(canvas).toHaveAttribute("data-map-edge-count", String(scene.edges.length));
      await expect(native).toHaveCount(1);
      await expect(canvas.locator("[data-workspace-map-folder-islands]")).toHaveCount(0);
      await expect(canvas).not.toHaveAttribute("data-map-folder-count");
      await expect(scope.getByRole("button", { name: labels.resume, exact: true })).toBeEnabled();
    };
    const exportBaselines = new Map<string, unknown>();
    const assertExport = async (phase: string) => {
      // Native columns retain the whole connected scene, while the shipped
      // list/export contains the current Find-in-map subset. Derive its
      // canonical IDs independently from the actor-visible wire response;
      // Folder context must not alter either representation.
      const query = (await search.inputValue()).trim().toLocaleLowerCase();
      const expectedNodes = scene.nodes.filter((node) => connected.has(node.id)
        && (!query || [node.label,
          node.grain === "document" ? node.primary_folder_label : null,
          node.grain === "document" ? node.entity_kind : "cluster"]
          .filter(Boolean).join(" ").toLocaleLowerCase().includes(query)));
      const expectedIds = new Set(expectedNodes.map((node) => node.id));
      const expectedEdges = scene.edges.filter((edge) => expectedIds.has(edge.source) && expectedIds.has(edge.target));
      const event = page.waitForEvent("download");
      await scope.getByRole("button", { name: labels.export, exact: true }).click();
      const download = await event;
      expect(download.suggestedFilename()).toMatch(/^dtxwiki-knowledge-map-.*\.json$/);
      const stream = await download.createReadStream();
      if (!stream) throw new Error("Actual source export has no stream");
      const chunks: Buffer[] = [];
      for await (const chunk of stream) chunks.push(Buffer.from(chunk));
      const payload = JSON.parse(Buffer.concat(chunks).toString("utf8")) as {
        graph_revision: string; nodes: { id: string; entityKind: string; grain: string }[];
        edges: { id: string; source: string; target: string }[];
      };
      await attachJson(testInfo, `real-api-2k-folder-context-export-${phase}-${suffix}.json`, payload);
      expect(payload.graph_revision).toBe(scene.graph_revision);
      expect(payload.nodes.map((node) => node.id).sort()).toEqual([...expectedIds].sort());
      expect(payload.edges.map((edge) => edge.id).sort()).toEqual(expectedEdges.map((edge) => edge.id).sort());
      const sourceProof: { id: string; entityId: string; entityKind: string; path: string }[] = [];
      for (const node of payload.nodes) {
        const original = nodes.get(node.id);
        if (!original || original.grain !== "document") throw new Error("Export invented a non-document source");
        if (node.grain !== "document"
          || node.entityKind !== (original.entity_kind === "context" ? "context_page" : "library_item")) {
          throw new Error("Export changed the original source kind");
        }
        const sourceUrl = new URL(original.path, backendBaseUrl);
        if (sourceUrl.origin !== new URL(backendBaseUrl).origin
          || sourceUrl.pathname !== `/workspaces/${workspaceId}/${original.entity_kind === "context" ? "context/pages" : "library/items"}/${original.entity_id}`
          || sourceUrl.search !== "") {
          throw new Error("Export source ID does not resolve to its canonical original document URL");
        }
        sourceProof.push({ id: original.id, entityId: original.entity_id,
          entityKind: original.entity_kind, path: original.path });
      }
      const originalEdges = new Map(expectedEdges.map((edge) => [edge.id, edge]));
      for (const edge of payload.edges) {
        const original = originalEdges.get(edge.id);
        if (!original || edge.source !== original.source || edge.target !== original.target) {
          throw new Error("Export changed canonical relation endpoints");
        }
      }
      if (exportBaselines.has(query)) expect(payload).toEqual(exportBaselines.get(query));
      else exportBaselines.set(query, payload);
      await attachJson(testInfo, `real-api-2k-folder-context-export-sources-${phase}-${suffix}.json`, {
        search: query, sourceProof, expectedEdgeIds: [...originalEdges.keys()].sort(),
        scope: "Export IDs resolve to original actor-visible API sources; paths are wire provenance, not added export fields",
      });
    };
    let initialCorridorEdgeId: string | undefined;
    if (!mobile) {
      initialCorridorEdgeId = await prepareNativeCrossFolderCorridor(page, canvas, scene, testInfo, `default-${suffix}`);
      await expectNativeCrossFolderCorridor(page, canvas, scene, testInfo, `default-${suffix}`, initialCorridorEdgeId);
    }
    const initial = await waitStableMapReadback(page, canvas);
    await assertExport("baseline-off");
    await clickFolderToggle();
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await assertNativeIdentity();
    expectSameMapGeometry(initial, await settleMapReadback(page, canvas));
    if (!mobile) await expectNativeCrossFolderCorridor(page, canvas, scene, testInfo, `context-on-${suffix}`, initialCorridorEdgeId);
    await assertExport("on");
    await capture(canvas, testInfo, `real-api-2k-folder-context-default-${suffix}.png`);
    await clickFolderToggle();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await assertNativeIdentity();
    expectSameMapGeometry(initial, await settleMapReadback(page, canvas));
    await assertExport("off");

    // A native drag establishes a NONEMPTY original-source pin. An empty
    // persisted pin list cannot demonstrate pin continuity.
    if (!mobile) await search.fill("note");
    const dragDelta = { x: 18, y: 12 };
    const { dragPoint, dragBounds, beforeDrag } = await prepareNativeDocumentDrag(
      page, canvas, scene, dragDelta, testInfo, suffix,
    );
    await page.mouse.down();
    await page.mouse.move(dragBounds.x + dragPoint.x + dragDelta.x,
      dragBounds.y + dragPoint.y + dragDelta.y, { steps: 10 });
    await page.mouse.up();
    // Mouse hover uses a clamped full-title label. Leave the native canvas so
    // the original ordinary GPU-backed label can measure the completed drag.
    const afterDrag = await settleMapReadback(page, canvas);
    expect(afterDrag.camera).not.toBeNull();
    expect(afterDrag.camera!.k).toBeCloseTo(beforeDrag.camera!.k, 5);
    expect(afterDrag.camera!.x).toBeCloseTo(beforeDrag.camera!.x, 2);
    expect(afterDrag.camera!.y).toBeCloseTo(beforeDrag.camera!.y, 2);
    await expect.poll(async () => (await readStoredMapState(page, scene.graph_revision)).pinnedIds)
      .toContain(dragPoint.id);
    await expect.poll(async () => {
      const moved = (await readMapReadback(canvas)).labels.find((point) => point.id === dragPoint.id);
      return moved ? Math.hypot(moved.x - dragPoint.x, moved.y - dragPoint.y) : 0;
    }).toBeGreaterThan(4);
    const moved = (await readMapReadback(canvas)).labels.find((point) => point.id === dragPoint.id)!;
    await page.mouse.click(dragBounds.x + moved.x, dragBounds.y + moved.y);
    await expect.poll(async () => (await readStoredMapState(page, scene.graph_revision)).selectedId)
      .toBe(dragPoint.id);
    await scope.getByRole("button", { name: labels.fit, exact: true }).click();
    const baseline = await waitStableMapReadback(page, canvas);
    const baselineState = await readStoredMapState(page, scene.graph_revision);
    expect(baselineState.pinnedIds).toContain(dragPoint.id);
    expect(baselineState.selectedId).toBe(dragPoint.id);
    await assertExport("baseline-off-selected-pinned");
    const assertContinuity = async (phase: string) => {
      await assertNativeIdentity();
      await expect(search).toHaveValue("note");
      expect(await readStoredMapState(page, scene.graph_revision)).toEqual(baselineState);
      const after = await settleMapReadback(page, canvas);
      expectSameMapGeometry(baseline, after);
      expect(overviewRequests).toBe(0);
      await attachJson(testInfo, `real-api-2k-folder-context-continuity-${phase}-${suffix}.json`, {
        entrypoint: "real Pause layout, native document drag/pin/click, visible search and Folder context UI",
        scope: "same native canvas and every observed GPU-backed document label; not all2K coordinate readback",
        before: baseline, after, state: baselineState, search: "note", overviewRequests,
      });
    };
    await clickFolderToggle();
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await assertContinuity("on-selected-pinned");
    await assertExport("on-selected-pinned");
    const index = scope.locator(".workspace-graph-folder-index");
    await index.locator("summary").click();
    expect((await index.locator("[data-map-folder-id]").evaluateAll((rows) =>
      rows.map((row) => row.getAttribute("data-map-folder-id")))).sort()).toEqual([...visibleFolders].sort());
    const folderButton = index.locator(`[data-map-folder-id="${folderId}"]`);
    await folderButton.focus();
    await page.keyboard.press("Enter");
    await expect(folderButton).toHaveAttribute("aria-pressed", "true");
    const details = scope.getByRole("region", { name: labels.details, exact: true });
    await expect(details).toHaveAttribute("data-map-folder-details", folderId);
    await expect(details.getByRole("heading", { name: folder.label, exact: true })).toBeVisible();
    expect((await details.locator("[data-map-folder-member]").evaluateAll((rows) =>
      rows.map((row) => row.getAttribute("data-map-folder-member")))).sort()).toEqual(memberIds);
    await assertContinuity("folder-details");
    await capture(details, testInfo, `real-api-2k-folder-context-details-${suffix}.png`);
    if (folder.parent_folder_id) {
      const parent = folders.get(folder.parent_folder_id)!;
      const parentLabel = language === "ko" ? `부모 폴더: ${parent.label}` : `Parent folder: ${parent.label}`;
      await details.getByRole("button", { name: parentLabel, exact: true }).focus();
      await page.keyboard.press("Enter");
      await expect(details).toHaveAttribute("data-map-folder-details", parent.id);
      await expect(details.getByRole("heading", { name: parent.label, exact: true })).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(details.getByRole("button", { name: labels.closeDetails, exact: true })).toBeFocused();
      await assertContinuity("parent-navigation");
      const child = details.locator(".workspace-map-folder-children")
        .getByRole("button", { name: folder.label, exact: true });
      await child.focus();
      await page.keyboard.press("Enter");
      await expect(details).toHaveAttribute("data-map-folder-details", folder.id);
      await expect(details.getByRole("heading", { name: folder.label, exact: true })).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(details.getByRole("button", { name: labels.closeDetails, exact: true })).toBeFocused();
      await assertContinuity("child-navigation");
    }
    const related = details.locator(".workspace-map-folder-bundles button").first();
    if (await related.count()) {
      const label = await related.innerText();
      await related.focus();
      await page.keyboard.press("Enter");
      await expect(details.getByRole("heading", { name: label, exact: true })).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(details.getByRole("button", { name: labels.closeDetails, exact: true })).toBeFocused();
      await assertContinuity("related-folder-navigation");
      // Return through the shipped index, without changing document selection.
      await folderButton.focus();
      await page.keyboard.press("Enter");
      await expect(details).toHaveAttribute("data-map-folder-details", folderId);
      await assertContinuity("folder-index-return");
    }
    await details.getByRole("button", { name: labels.closeDetails, exact: true }).click();
    await expect(scope.locator("[data-map-folder-details]")).toHaveCount(0);
    await assertContinuity("close-details");
    await clickFolderToggle();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await expect(scope.locator(".workspace-graph-folder-index")).toHaveCount(0);
    await assertContinuity("off-selected-pinned");
    await assertExport("off-selected-pinned");
    await clickFolderToggle();
    await index.locator("summary").click();
    await folderButton.focus();
    await page.keyboard.press("Enter");
    const sourceId = memberIds.find((id) => nodes.get(id)?.grain === "document");
    if (!sourceId) throw new Error("No real document member source");
    const source = nodes.get(sourceId)!;
    if (source.grain !== "document") throw new Error("Member is not an original source");
    const member = details.locator(`[data-map-folder-member="${sourceId}"]`);
    await expect(member.getByRole("link")).toHaveAttribute("href", source.path);
    await member.getByRole("button").click();
    await expect(scope.locator("[data-map-folder-details]")).toHaveCount(0);
    await expect.poll(async () => (await readStoredMapState(page, scene.graph_revision)).selectedId).toBe(sourceId);
    expect((await readStoredMapState(page, scene.graph_revision)).pinnedIds).toContain(dragPoint.id);
    await assertNativeIdentity();
    await attachJson(testInfo, `real-api-2k-folder-context-member-source-${suffix}.json`, {
      folderId, directMemberIds: memberIds, originalSourceNodeId: sourceId, originalSourcePath: source.path,
      state: await readStoredMapState(page, scene.graph_revision), overviewRequests,
    });
    await capture(canvas, testInfo, `real-api-2k-folder-context-source-selected-${suffix}.png`);
    await scope.getByRole("button", { name: labels.clear, exact: true }).click();
    if (mobile) {
      await scope.getByRole("button", { name: labels.closeCanvas, exact: true }).click();
      await search.fill("");
      await page.locator(".workspace-graph-view-options > summary").click();
      await page.getByRole("button", { name: labels.context, exact: true }).click();
    } else {
      await search.fill("");
      // Real CSS/system-media remounts must preserve original source IDs and
      // pins, and the new engine must truly inherit pause; no extra pause click.
      for (const [phase, theme, forced] of [
        ["dark", "dark", false], ["forced", "dark", true], ["restored", originalTheme, false],
      ] as const) {
        const beforeTheme = await settleMapReadback(page, canvas);
        const beforeThemeState = await readStoredMapState(page, scene.graph_revision);
        const previous = await native.elementHandle();
        if (!previous) throw new Error("No pre-theme native canvas");
        await page.evaluate((next) => {
          if (next === null) document.documentElement.removeAttribute("data-theme");
          else document.documentElement.setAttribute("data-theme", next);
        }, theme);
        await page.emulateMedia({ forcedColors: forced ? "active" : "none" });
        await expect.poll(() => previous.evaluate((element) => element.isConnected)).toBe(false);
        await previous.dispose();
        await expect(canvas.locator("..")).toHaveAttribute("data-renderer-status", "ready");
        await expect(native).toHaveCount(1);
        await expect(canvas).toHaveAttribute("data-map-node-count", String(connected.size));
        await expect(canvas).toHaveAttribute("data-map-edge-count", String(scene.edges.length));
        await expect(canvas).toHaveAttribute("data-map-community-count", /^[1-9]\d*$/);
        await expect(scope.getByRole("button", { name: labels.resume, exact: true })).toBeEnabled();
        const paused = await waitStableMapReadback(page, canvas);
        expectSameMapGeometry(beforeTheme, paused);
        await page.waitForTimeout(400);
        expectSameMapGeometry(paused, await readMapReadback(canvas));
        expect(beforeTheme.camera).not.toBeNull();
        expect(paused.camera).not.toBeNull();
        expect(paused.camera!.k).toBeCloseTo(beforeTheme.camera!.k, 5);
        expect(paused.camera!.x).toBeCloseTo(beforeTheme.camera!.x, 2);
        expect(paused.camera!.y).toBeCloseTo(beforeTheme.camera!.y, 2);
        const beforeFitState = await readStoredMapState(page, scene.graph_revision);
        expect(beforeFitState).toEqual(beforeThemeState);
        expect(beforeFitState.pinnedIds).toContain(dragPoint.id);
        await assertExport(`theme-${phase}`);
        await capture(canvas, testInfo, `real-api-2k-folder-context-theme-${phase}-${suffix}.png`);
        await attachJson(testInfo, `real-api-2k-folder-context-theme-${phase}-${suffix}.json`, {
          entrypoint: "html[data-theme]/forced-color media emulation, not user settings control",
          theme, forced, beforeTheme, pausedBefore: paused, pausedAfter: await readMapReadback(canvas),
          stateBefore: beforeThemeState, stateAfter: beforeFitState, overviewRequests,
        });
        // A member action intentionally centers its original document. Preserve
        // and prove that camera through remount first, then use shipped Fit to
        // establish an overview with observable cross-folder endpoints. This
        // explicit navigation may change camera/screen coordinates, not pins,
        // selection, source counts, pause, search or data requests.
        await scope.getByRole("button", { name: labels.fit, exact: true }).click();
        const fitted = await waitStableMapReadback(page, canvas);
        const fittedState = await readStoredMapState(page, scene.graph_revision);
        expect(fittedState.selectedId).toBe(beforeFitState.selectedId);
        expect(fittedState.pinnedIds).toEqual(beforeFitState.pinnedIds);
        expect(fittedState.pinnedPositions).toEqual(beforeFitState.pinnedPositions);
        expect(fitted.nodes).toBe(paused.nodes);
        expect(fitted.edges).toBe(paused.edges);
        await expect(search).toHaveValue("");
        await expect(scope.getByRole("button", { name: labels.resume, exact: true })).toBeEnabled();
        expect(overviewRequests).toBe(0);
        await attachJson(testInfo, `real-api-2k-folder-context-theme-fit-${phase}-${suffix}.json`, {
          entrypoint: "real Fit graph after theme camera/pin/pause continuity was verified",
          scope: "intentional overview navigation; camera and screen coordinates may change, original saved pins may not",
          theme, forced, before: paused, after: fitted, stateBefore: beforeFitState,
          stateAfter: fittedState, search: "", overviewRequests,
        });
        const themeCorridorEdgeId = await prepareNativeCrossFolderCorridor(page, canvas, scene, testInfo, `${phase}-context-on-${suffix}`);
        await expectNativeCrossFolderCorridor(page, canvas, scene, testInfo, `${phase}-context-on-${suffix}`, themeCorridorEdgeId);
      }
      await clickFolderToggle();
    }
    expect(overviewRequests, "Folder context, inspection, drag/pin, display and export must not re-query data").toBe(0);
    await expect(page.getByTestId("workspace-graph-node-list"))
      .toHaveAttribute("data-total-count", String(connected.size));
    await nativeIdentity.dispose();
  } finally {
    page.off("request", recordRequest);
    await page.evaluate((theme) => {
      if (theme === null) document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", theme);
    }, originalTheme);
    await page.emulateMedia({ reducedMotion: "reduce", forcedColors: "none" });
  }
}

async function expectNativeCrossFolderCorridor(
  page: Page, canvas: Locator, scene: WorkspaceGraphScene, testInfo: TestInfo, suffix: string,
  expectedWireEdgeId?: string,
) {
  const before = await settleMapReadback(page, canvas);
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  const candidates = observedCrossFolderPairs(before, scene).filter((row) => row.length >= 60 && row.length <= 350);
  const pair = expectedWireEdgeId === undefined ? candidates[0]
    : candidates.find((candidate) => candidate.edge.id === expectedWireEdgeId);
  if (!pair) throw new Error("No actual visible cross-folder wire edge for native corridor proof");
  const sourceNode = nodes.get(pair.edge.source)!;
  const targetNode = nodes.get(pair.edge.target)!;
  expect(sourceNode.grain).toBe("document");
  expect(targetNode.grain).toBe("document");
  if (sourceNode.grain !== "document" || targetNode.grain !== "document") throw new Error("Cross corridor source is aggregated");
  expect(sourceNode.primary_folder_id).not.toBe(targetNode.primary_folder_id);
  const native = canvas.locator("canvas");
  const identity = await native.elementHandle();
  if (!identity) throw new Error("No cross-link native canvas");
  const on = await capture(native, testInfo, `real-api-2k-cross-folder-on-${suffix}.png`);
  const setLinks = async (visible: boolean) => {
    await page.getByRole("button", { name: "Refine", exact: true }).click();
    const control = page.getByRole("dialog", { name: "Refine data", exact: true })
      .getByRole("checkbox", { name: "Show relationships", exact: true });
    if (visible) await control.check(); else await control.uncheck();
    await page.keyboard.press("Escape");
    expect(await native.evaluate((element, original) => element === original, identity)).toBe(true);
    expectSameMapGeometry(before, await settleMapReadback(page, canvas));
  };
  await setLinks(false);
  const off = await capture(native, testInfo, `real-api-2k-cross-folder-off-${suffix}.png`);
  await setLinks(true);
  const restored = await capture(native, testInfo, `real-api-2k-cross-folder-restored-${suffix}.png`);
  const pixelProof = await page.evaluate(async ({ images, pair, width, height }) => {
    const decode = async (encoded: string) => {
      const image = new Image(); image.src = `data:image/png;base64,${encoded}`; await image.decode();
      const canvas = document.createElement("canvas"); canvas.width = image.width; canvas.height = image.height;
      const context = canvas.getContext("2d")!; context.drawImage(image, 0, 0);
      return { pixels: context.getImageData(0, 0, canvas.width, canvas.height).data,
        width: canvas.width, height: canvas.height };
    };
    const [on, off, restored] = await Promise.all(images.map(decode));
    if (on.width !== off.width || on.height !== off.height
      || restored.width !== off.width || restored.height !== off.height) throw new Error("Native corridor image size changed");
    const colors = new Map<string, number>();
    for (let i = 0; i < off.pixels.length; i += 4) {
      const key = `${off.pixels[i]},${off.pixels[i + 1]},${off.pixels[i + 2]}`;
      colors.set(key, (colors.get(key) ?? 0) + 1);
    }
    const surface = [...colors].sort((left, right) => right[1] - left[1])[0][0].split(",").map(Number);
    const mask = (x: number, y: number, ink: Uint8ClampedArray) => {
      if (x < 0 || y < 0 || x >= off.width || y >= off.height) return false;
      const offset = (y * off.width + x) * 4;
      return surface.every((value, channel) => Math.abs(off.pixels[offset + channel] - value) < 20)
        && surface.some((_value, channel) => Math.abs(ink[offset + channel] - off.pixels[offset + channel]) > 12);
    };
    const samples = Array.from({ length: 64 }, (_, index) => {
      const t = 0.2 + (index / 63) * 0.6;
      const x = Math.round((pair.source.x + (pair.target.x - pair.source.x) * t) * off.width / width);
      const y = Math.round((pair.source.y + (pair.target.y - pair.source.y) * t) * off.height / height);
      let onInk = false; let restoredInk = false;
      for (let dx = -2; dx <= 2; dx += 1) for (let dy = -2; dy <= 2; dy += 1) {
        onInk ||= mask(x + dx, y + dy, on.pixels);
        restoredInk ||= mask(x + dx, y + dy, restored.pixels);
      }
      return { t, x, y, onInk, restoredInk };
    });
    return { surface, samples, onCoverage: samples.filter((sample) => sample.onInk).length / samples.length,
      restoredCoverage: samples.filter((sample) => sample.restoredInk).length / samples.length };
  }, { images: [on, off, restored].map((image) => image.toString("base64")),
    pair: { source: pair.source, target: pair.target }, width: before.width, height: before.height });
  await attachJson(testInfo, `real-api-2k-cross-folder-corridor-${suffix}.json`, {
    source: "actual unselected production API cross_folder wikilink and real GPU-backed label endpoints",
    scope: "interior native edge corridor, fixed camera, Links OFF negative and ON restored positive; not weighted-force proof",
    wireEdge: pair.edge, sourcePrimaryFolderId: sourceNode.primary_folder_id,
    targetPrimaryFolderId: targetNode.primary_folder_id, endpoints: { source: pair.source, target: pair.target },
    before, after: await readMapReadback(canvas), pixelProof,
  });
  expect(pixelProof.onCoverage, "The default actual cross-folder edge corridor must paint continuously").toBeGreaterThanOrEqual(0.5);
  expect(pixelProof.restoredCoverage, "The same cross-folder corridor must return after real Links ON").toBeGreaterThanOrEqual(0.5);
  await identity.dispose();
}

function expectCurrentFolderTopology(scene: WorkspaceGraphScene) {
  const topology = scene.folder_topology;
  if (!topology) throw new Error("Real overview did not return current folder inputs");
  // Check every wire row, but record only one Playwright assertion. A plain
  // expect per row creates tens of thousands of traced steps whose parent-step
  // lookup scans the growing step history, blocking the runner rather than
  // measuring the production API or renderer.
  const failures: string[] = [];
  const check = (condition: boolean, message: string) => {
    if (!condition) failures.push(message);
  };
  check(topology.schema_version === "workspace-graph-folders.v1", "folder schema version");
  const folders = new Map(topology.folders.map((folder) => [folder.id, folder]));
  check(folders.size === topology.folders.length, "folder IDs must be unique");
  const membershipKeys = new Set<string>();
  const membersByNode = new Map<string, number>();
  const membersByFolder = new Map<string, number>();
  const membershipKey = (nodeId: string, folderId: string | null, kind: string) =>
    JSON.stringify([nodeId, folderId, kind]);
  for (const row of topology.memberships) {
    const key = membershipKey(row.node_id, row.folder_id, row.entity_kind);
    check(Number.isSafeInteger(row.member_count) && row.member_count > 0,
      `membership ${key}: positive safe member count`);
    check(!membershipKeys.has(key), `membership ${key}: unique node/folder/kind`);
    membershipKeys.add(key);
    membersByNode.set(row.node_id, (membersByNode.get(row.node_id) ?? 0) + row.member_count);
    if (row.folder_id !== null) {
      check(folders.has(row.folder_id), `membership ${key}: folder must exist`);
      membersByFolder.set(row.folder_id,
        (membersByFolder.get(row.folder_id) ?? 0) + row.member_count);
    }
  }
  for (const node of scene.nodes) {
    check(membersByNode.get(node.id) === (node.grain === "document" ? 1 : node.member_count),
      `node ${node.id}: membership total must equal represented members`);
    if (node.grain === "document") {
      check(membershipKeys.has(membershipKey(node.id, node.primary_folder_id, node.entity_kind)),
        `node ${node.id}: current primary folder and entity kind`);
    }
  }
  const closure = new Set<string>();
  for (const id of membersByFolder.keys()) {
    const branch = new Set<string>();
    let current: string | null = id;
    while (current !== null && !closure.has(current)) {
      if (branch.has(current)) {
        check(false, `folder ${id}: ancestor cycle at ${current}`);
        break;
      }
      branch.add(current);
      const folder = folders.get(current);
      if (!folder) {
        check(false, `folder ${id}: ancestor ${current} must be in the visible response`);
        break;
      }
      current = folder.parent_folder_id;
    }
    for (const folderId of branch) closure.add(folderId);
  }
  check(closure.size === folders.size && [...folders.keys()].every((id) => closure.has(id)),
    "visible folders must equal the primary-membership ancestor closure");
  for (const folder of folders.values()) {
    check(folder.direct_member_count === (membersByFolder.get(folder.id) ?? 0),
      `folder ${folder.id}: exact direct member count`);
  }
  const edges = new Map(scene.edges.map((edge) => [edge.id, edge]));
  const counts = new Map<string, number>();
  const weights = new Map<string, number>();
  for (const row of topology.edge_slices) {
    const edge = edges.get(row.edge_id);
    check(Number.isSafeInteger(row.relation_count) && row.relation_count > 0,
      `slice ${row.edge_id}: positive safe relation count`);
    check(Number.isSafeInteger(row.occurrence_weight) && row.occurrence_weight > 0,
      `slice ${row.edge_id}: positive safe occurrence weight`);
    if (!edge) {
      check(false, `slice ${row.edge_id}: authored relationship must exist`);
      continue;
    }
    check(membershipKeys.has(membershipKey(edge.source, row.source_folder_id, row.source_entity_kind)),
      `slice ${row.edge_id}: correlated source folder and entity kind`);
    check(membershipKeys.has(membershipKey(edge.target, row.target_folder_id, row.target_entity_kind)),
      `slice ${row.edge_id}: correlated target folder and entity kind`);
    counts.set(edge.id, (counts.get(edge.id) ?? 0) + row.relation_count);
    weights.set(edge.id, (weights.get(edge.id) ?? 0) + row.occurrence_weight);
  }
  for (const edge of scene.edges) {
    check(counts.get(edge.id) === edge.relation_count,
      `edge ${edge.id}: exact summed relation count`);
    check(weights.get(edge.id) === edge.occurrence_weight,
      `edge ${edge.id}: exact summed occurrence weight`);
    check(Number.isSafeInteger(edge.occurrence_weight) && edge.occurrence_weight! > 0,
      `edge ${edge.id}: positive safe occurrence weight`);
  }
  expect(failures, "Every real overview folder topology row must satisfy the wire invariants")
    .toEqual([]);
}

async function expectLocalSceneNavigation(
  page: Page,
  panel: Locator,
  scene: GraphQueryPayload,
  workspaceId: string,
  testInfo: TestInfo,
  language: keyof typeof SCENE_NAVIGATION_LABELS,
) {
  const labels = SCENE_NAVIGATION_LABELS[language];
  const suffix = `${language}-${page.viewportSize()!.width}`;
  const details = panel.locator(".graph-details");
  const canvasFrame = panel.getByTestId("graph-canvas").locator("..");
  const initialCanvasBox = await canvasFrame.boundingBox();
  expect(initialCanvasBox).not.toBeNull();
  const expectCanvasSizeRetained = async () => {
    const box = await canvasFrame.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.height, "a growing Details list must not resize the Local canvas").toBeCloseTo(initialCanvasBox!.height, 0);
    expect(box!.width).toBeCloseTo(initialCanvasBox!.width, 0);
  };
  const expectDetailsAtTop = async () => {
    await expect.poll(() => details.evaluate((element) => element.scrollTop)).toBe(0);
    const heading = await details.locator("h5").boundingBox();
    const box = await details.boundingBox();
    const actions = await details.locator(".graph-details-actions").boundingBox();
    expect(heading).not.toBeNull();
    expect(box).not.toBeNull();
    expect(actions).not.toBeNull();
    // The bounded mobile sheet puts actions before the untruncated title.
    // A wrapping full title may need its own scrolling; initial controls must
    // be available without requiring a title-sized always-open sidebar.
    expect(actions!.y).toBeGreaterThanOrEqual(box!.y);
    expect(actions!.y + actions!.height).toBeLessThanOrEqual(box!.y + box!.height);
    expect(heading!.y).toBeGreaterThanOrEqual(box!.y);
    expect(heading!.y).toBeLessThan(box!.y + box!.height);
  };
  const expectAllDetailsActionsPainted = async () => {
    // Inspect before Tab/click/scroll: even a disabled Expand must be fully
    // visible, and locator actionability must not scroll a clipped row into view.
    const controls = await details.evaluate((element) => {
      const panel = element.getBoundingClientRect();
      const reader = element.closest(".graph-view")!.getBoundingClientRect();
      return Array.from(element.querySelectorAll<HTMLElement>(
        ".graph-details-actions a,.graph-details-actions button,.graph-details-context-actions a,.graph-details-context-actions button",
      )).map((control) => {
        const bounds = control.getBoundingClientRect();
        const points = [[bounds.left + 4, bounds.top + 4], [bounds.right - 4, bounds.top + 4],
          [bounds.left + 4, bounds.bottom - 4], [bounds.right - 4, bounds.bottom - 4],
          [bounds.left + bounds.width / 2, bounds.top + bounds.height / 2]];
        return {
          label: control.getAttribute("aria-label") ?? control.textContent?.trim(),
          width: bounds.width, height: bounds.height,
          contained: bounds.left >= Math.max(panel.left, reader.left, 0) - 1
            && bounds.right <= Math.min(panel.right, reader.right, innerWidth) + 1
            && bounds.top >= Math.max(panel.top, reader.top, 0) - 1
            && bounds.bottom <= Math.min(panel.bottom, reader.bottom, innerHeight) + 1,
          painted: points.every(([x, y]) => {
            const hit = document.elementFromPoint(x, y);
            return hit !== null && control.contains(hit);
          }),
        };
      });
    });
    expect(controls.length).toBeGreaterThan(0);
    expect(controls.filter((control) => control.width < 44 || control.height < 44
      || !control.contained || !control.painted), "All Details actions must be complete native targets before any scroll")
      .toEqual([]);
    return controls;
  };
  const rows = panel.locator(".graph-relationship-list > details").first().locator(":scope > ul > li");
  const mainLabels = await rows.locator(":scope > button.graph-list-select strong").allTextContents();
  // Fresh browser defaults show the whole current response. This assertion
  // prevents deriving expectations from endpoints absent from the real DOM.
  expect([...mainLabels].sort()).toEqual(scene.nodes.map((node) => node.label).sort());
  const mainCueRows = await expectLocalNodeCues(
    page, rows.locator(":scope > button.graph-list-select"), scene.nodes,
  );
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  // Independent wire-kind oracle, deliberately not the production navigation
  // helper: unknown kinds and dangling/self endpoints are not rendered links.
  const visibleKinds = new Set([
    "wikilink", "citation_page", "citation_source", "ask_citation", "co_access", "tree",
    "searched", "returned", "retrieved", "included", "cited", "created", "edited",
    "reviewed", "approved", "rejected", "request_changes", "published", "restored",
    "superseded", "merged", "promoted", "tool_produced", "referenced",
  ]);
  const edges = scene.edges.filter((edge) => {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    return source && target && visibleKinds.has(edge.edge_kind)
      && !(source.entity_kind === target.entity_kind && source.entity_id === target.entity_id);
  });
  const relatedNodes = (ids: string[]) => [...new Set(ids)].map((id) => nodes.get(id)!);
  const membersOf = (id: string) => relatedNodes(edges
    .filter((edge) => edge.edge_kind === "tree" && edge.source === id).map((edge) => edge.target));
  const neighborsOf = (id: string) => relatedNodes(edges.flatMap((edge) =>
    edge.source === id ? [edge.target] : edge.target === id ? [edge.source] : []));
  const foldersOf = (id: string) => relatedNodes(edges.filter((edge) =>
    edge.edge_kind === "tree" && edge.target === id
    && nodes.get(edge.source)?.entity_kind === "workspace_tree_node"
    && nodes.get(edge.source)?.family === "structural").map((edge) => edge.source));
  const uniqueLabel = (node: GraphNode) => mainLabels.filter((label) => label === node.label).length === 1;
  const folder = scene.nodes.filter((node) => node.entity_kind === "workspace_tree_node"
    && node.family === "structural" && uniqueLabel(node) && membersOf(node.id).length > 0)
    .sort((left, right) => membersOf(right.id).length - membersOf(left.id).length)[0];
  expect(folder, "real Local scene must contain a folder and at least one direct rendered member").toBeTruthy();
  if (!folder) throw new Error("Real Local scene has no testable folder membership");
  const members = membersOf(folder.id);
  const contextNode = scene.nodes.find((node) => node.entity_kind === "context_page"
    && uniqueLabel(node) && foldersOf(node.id).length > 0
    && neighborsOf(node.id).length >= 2 && neighborsOf(node.id).every(uniqueLabel));
  expect(contextNode, "real Local scene must expose a placed document with two direct neighbors").toBeTruthy();
  if (!contextNode) throw new Error("Real Local scene has no testable document navigation");
  const rowFor = (node: GraphNode) => rows.nth(mainLabels.indexOf(node.label));
  const selectMain = async (node: GraphNode) => {
    await rowFor(node).locator(":scope > button.graph-list-select").click();
    await expect(details.locator("h5")).toHaveText(node.label);
  };
  const listFor = (scope: Locator, name: string) => scope.getByRole("list", { name, exact: true });
  const expectList = async (list: Locator, expected: GraphNode[]) => {
    await expect(list.locator(":scope > li > button")).toHaveCount(expected.length);
    await expect(list.locator(":scope > li > button[tabindex='0']")).toHaveCount(expected.length ? 1 : 0);
    await expect(list.locator(":scope > li > button[tabindex='-1']"))
      .toHaveCount(Math.max(0, expected.length - 1));
    expect((await list.locator("strong").allTextContents()).sort())
      .toEqual(expected.map((node) => node.label).sort());
    await expectLocalNodeCues(page, list.locator(":scope > li > button"), expected);
  };
  const expectEmptyDetails = async () => {
    // Keep the stable aside DOM, but a blank inspector must not take canvas
    // space, focus stops or a complementary landmark in the accessible tree.
    await expect(details).toBeHidden();
    await expect(details).toHaveAttribute("hidden", "");
    await expect(details.locator("h5")).toHaveCount(0);
    await expect(details.getByText(labels.empty, { exact: true })).toHaveCount(1);
    await expect(details.locator("button,a")).toHaveCount(0);
    await expect(panel.getByRole("complementary")).toHaveCount(0);
  };
  const requestPaths: string[] = [];
  const observeQuery = (request: import("@playwright/test").Request) => {
    if (request.method() === "POST" && request.url().endsWith(`/workspaces/${workspaceId}/graph/query`)) {
      requestPaths.push(new URL(request.url()).pathname);
    }
  };
  page.on("request", observeQuery);
  try {
    const mainButtons = rows.locator(":scope > button.graph-list-select");
    // The list's original wire rows remain complete, but only one is a Tab
    // entry. Arrow navigation must not select or issue a query.
    await expect(rows.locator(":scope > button[tabindex='0']")).toHaveCount(1);
    await mainButtons.first().focus();
    await page.keyboard.press("End");
    await expect(mainButtons.last()).toBeFocused();
    await expectEmptyDetails();
    await page.keyboard.press("Home");
    await expect(mainButtons.first()).toBeFocused();
    await expectEmptyDetails();
    await selectMain(folder);
    const memberList = listFor(details, labels.members);
    await expectList(memberList, members);
    await expectCanvasSizeRetained();
    await expectDetailsAtTop();
    const folderDetailsSize = await details.evaluate((element) => ({
      height: element.getBoundingClientRect().height,
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
    }));
    expect(folderDetailsSize.height).toBeLessThanOrEqual(initialCanvasBox!.height + 1);
    expect(folderDetailsSize.scrollHeight, "large-folder navigation must remain internally scrollable")
      .toBeGreaterThan(folderDetailsSize.clientHeight);
    await panel.locator(".graph-local-stage").scrollIntoViewIfNeeded();
    await capture(page, testInfo, `real-api-2k-local-members-${suffix}.png`);

    await details.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    expect(await details.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
    await selectMain(contextNode);
    await expectDetailsAtTop();
    const neighbors = neighborsOf(contextNode.id);
    const neighborList = listFor(details, labels.neighbors);
    await expectList(neighborList, neighbors);
    const incomingFolders = details.locator("details.graph-scene-navigation")
      .filter({ has: page.locator("summary", { hasText: labels.folders }) });
    await incomingFolders.locator(":scope > summary").click();
    await expectList(listFor(incomingFolders, labels.folders), foldersOf(contextNode.id));
    await expectCanvasSizeRetained();
    await panel.locator(".graph-local-stage").scrollIntoViewIfNeeded();
    await capture(page, testInfo, `real-api-2k-local-neighbors-${suffix}.png`);

    const keyboardResults: Array<{ key: string; selected: string; focus: string }> = [];
    for (const key of ["Enter", "Space"]) {
      await selectMain(contextNode);
      const buttons = listFor(details, labels.neighbors).locator(":scope > li > button");
      await buttons.first().focus(); // Enter once; subsequent movement is native keyboard input.
      await page.keyboard.press("ArrowDown");
      await expect(buttons.nth(1)).toBeFocused();
      await page.keyboard.press("ArrowUp");
      await expect(buttons.first()).toBeFocused();
      if (key === "Space") await page.keyboard.press("ArrowDown");
      const selected = await buttons.nth(key === "Enter" ? 0 : 1).locator("strong").innerText();
      await page.keyboard.press(key);
      await expect(details.locator("h5")).toHaveText(selected);
      await expect(details).toBeFocused();
      await expectDetailsAtTop();
      await expectAllDetailsActionsPainted();
      await capture(page, testInfo, `real-api-2k-local-all-actions-${suffix}-${key}.png`);
      keyboardResults.push({ key, selected, focus: ".graph-details[tabindex=-1]" });
      // No manual focus, intermediate Tab, or click may mask the disappearing
      // neighbor before Escape. The Page capture above does not reveal a
      // locator or move focus/scroll.
      await page.keyboard.press("Escape");
      await expectEmptyDetails();
    }

    await selectMain(contextNode);
    const pointerNeighbor = listFor(details, labels.neighbors).locator(":scope > li > button").first();
    const pointerLabel = await pointerNeighbor.locator("strong").innerText();
    await pointerNeighbor.click();
    await expect(details.locator("h5")).toHaveText(pointerLabel);
    await expect(details).not.toBeFocused();
    // Membership now lives in selected Details, not repeated in every main
    // folder row. Keyboard activation deliberately hands off to Details.
    await selectMain(folder);
    const memberButton = memberList.locator(":scope > li > button").first();
    const memberLabel = await memberButton.locator("strong").innerText();
    await memberButton.focus();
    await page.keyboard.press("Enter");
    await expect(details.locator("h5")).toHaveText(memberLabel);
    await expect(details).toBeFocused();
    await expectAllDetailsActionsPainted();
    await page.keyboard.press("Tab");
    await expect(details.locator(".graph-details-actions > a, .graph-details-actions > button").first())
      .toBeFocused();
    await page.keyboard.press("Escape");
    await expectEmptyDetails();

    await expectCanvasSizeRetained();
    await selectMain(folder);
    const summaries = await details.locator(".graph-scene-navigation > summary").evaluateAll((elements) =>
      elements.filter((element) => element.getClientRects().length > 0).map((element) => ({
        label: element.textContent?.trim(), height: element.getBoundingClientRect().height,
      })));
    expect(summaries.length).toBeGreaterThan(0);
    for (const summary of summaries) expect(summary.height).toBeGreaterThanOrEqual(44);
    await memberButton.click();
    await expect(details.locator("h5")).toHaveText(memberLabel);
    await expect(details).not.toBeFocused();
    await page.keyboard.press("Escape");
    await expectEmptyDetails();

    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await afterAnimationFrame(page);
    expect(requestPaths, "scene list focus/selection must not issue new graph queries").toEqual([]);
    await attachJson(testInfo, `real-api-2k-local-navigation-${suffix}.json`, {
      language, viewport: page.viewportSize(), forcedColors: await page.evaluate(() => matchMedia("(forced-colors: active)").matches),
      renderedNodes: mainLabels.length, recognizedSceneEdges: edges.length,
      mainCueRows,
      folder: { id: folder.id, label: folder.label, directMembers: members.map((node) => node.id) },
      document: { id: contextNode.id, label: contextNode.label, directNeighbors: neighbors.map((node) => node.id) },
      keyboardResults, summaries, navigationQueryRequests: requestPaths,
      initialCanvasBox, folderDetailsSize,
    });
  } finally {
    page.off("request", observeQuery);
  }
}

function localWireCueKind(node: GraphNode) {
  // Independent API-kind oracle: family=document alone cannot distinguish
  // real Library versions/artifacts from Context pages and their versions.
  if (node.entity_kind === "context_page" || node.entity_kind === "context_page_version") return "context";
  if (["library_item_version", "library_artifact", "library_item"].includes(node.entity_kind)) return "library";
  return node.family === "structural" ? "folder" : "conversation";
}

async function expectLocalNodeCues(page: Page, buttons: Locator, expected: GraphNode[]) {
  await expect(buttons).toHaveCount(expected.length);
  const mode = await page.evaluate(() => matchMedia("(forced-colors: active)").matches ? "forced"
    : document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  const palettes = {
    light: { context: "rgb(124, 58, 237)", library: "rgb(15, 159, 143)", conversation: "rgb(234, 88, 12)", folder: "rgb(100, 116, 139)" },
    dark: { context: "rgb(139, 92, 246)", library: "rgb(13, 148, 136)", conversation: "rgb(234, 88, 12)", folder: "rgb(148, 163, 184)" },
  } as const;
  const actual = await buttons.evaluateAll((elements) => elements.map((button) => {
    const cue = button.querySelector(":scope > svg[data-node-visual-kind]");
    let surface = "";
    for (let ancestor: Element | null = cue ?? button; ancestor; ancestor = ancestor.parentElement) {
      const background = getComputedStyle(ancestor).backgroundColor;
      if (background !== "transparent" && !/^rgba\([^)]*,\s*0(?:\.0+)?\)$/.test(background)) { surface = background; break; }
    }
    return {
      label: button.querySelector("strong")?.textContent ?? "",
      kind: cue?.getAttribute("data-node-visual-kind") ?? "missing",
      hidden: cue?.getAttribute("aria-hidden"),
      // forced-color-adjust:none preserves the SVG geometry, but its unused
      // inherited color is not CanvasText. Read the auto-adjusted graph root.
      foreground: getComputedStyle(button.closest(".graph-view") ?? button).color, surface,
      legacy: Boolean(button.querySelector(".graph-node-swatch")),
      shapes: cue ? Array.from(cue.querySelectorAll("rect,circle")).map((shape) => ({
        name: shape.tagName.toLowerCase(), fill: getComputedStyle(shape).fill, stroke: getComputedStyle(shape).stroke,
        radius: Number(shape.getAttribute("r")),
      })) : [],
    };
  }));
  // Titles are intentionally not an identity key. A multiset retains every
  // (title, kind) occurrence even when Context and Library share one title.
  expect(actual.map((row) => JSON.stringify([row.label, row.kind])).sort())
    .toEqual(expected.map((node) => JSON.stringify([node.label, localWireCueKind(node)])).sort());
  for (const row of actual) {
    const kind = row.kind as ReturnType<typeof localWireCueKind>;
    const color = mode === "forced" ? row.foreground : palettes[mode][kind];
    expect(row.hidden).toBe("true");
    expect(row.legacy).toBe(false);
    expect(row.shapes.map((shape) => shape.name)).toEqual(kind === "library" ? ["rect"]
      : kind === "conversation" && mode === "forced" ? ["circle", "circle", "circle"] : ["circle"]);
    expect(row.shapes[0].fill).toBe(kind === "folder" ? "none" : color);
    if (kind === "folder") expect(row.shapes[0].stroke).toBe(color);
    if (kind === "conversation" && mode === "forced") {
      expect(row.shapes[1].fill).toBe(row.surface);
      expect(row.shapes[2].fill).toBe(color);
      expect(row.shapes[0].radius).toBeGreaterThan(row.shapes[1].radius);
      expect(row.shapes[1].radius).toBeGreaterThan(row.shapes[2].radius);
    }
  }
  return actual;
}

async function expectBoundedRows(
  nodeList: ReturnType<Page["getByTestId"]>,
  edgeList: ReturnType<Page["getByTestId"]>,
) {
  expect(await nodeList.locator("li[data-graph-node-id]").count()).toBeLessThan(30);
  expect(await edgeList.locator("li[data-graph-edge-id]").count()).toBeLessThan(30);
}

function watchBrowserDiagnostics(page: Page) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => {
    if (!isResizeObserverDeferralReport(error.message)) pageErrors.push(error.message);
  });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  return { pageErrors, consoleErrors };
}

/** Keep passing-run JSON evidence even when retain-on-failure discards traces. */
async function attachJson(testInfo: TestInfo, name: string, value: unknown) {
  const path = testInfo.outputPath(name);
  await writeFile(path, JSON.stringify(value, null, 2), "utf8");
  await testInfo.attach(name, { contentType: "application/json", path });
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

async function compareNativeCanvasPixels(page: Page, linksOn: Buffer, linksOff: Buffer) {
  return page.evaluate(async ([onBase64, offBase64]) => {
    const load = async (encoded: string) => {
      const image = new Image();
      image.src = `data:image/png;base64,${encoded}`;
      await image.decode();
      return image;
    };
    const [onImage, offImage] = await Promise.all([load(onBase64), load(offBase64)]);
    if (onImage.width !== offImage.width || onImage.height !== offImage.height) {
      throw new Error("Native canvas dimensions changed while toggling relationships");
    }
    const pixelData = (image: HTMLImageElement) => {
      const canvas = document.createElement("canvas");
      canvas.width = image.width;
      canvas.height = image.height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("2D screenshot decoder unavailable");
      context.drawImage(image, 0, 0);
      return context.getImageData(0, 0, canvas.width, canvas.height).data;
    };
    const onPixels = pixelData(onImage);
    const offPixels = pixelData(offImage);
    let changedPixels = 0;
    let changedOnBackground = 0;
    for (let offset = 0; offset < onPixels.length; offset += 4) {
      const delta = Math.max(
        Math.abs(onPixels[offset] - offPixels[offset]),
        Math.abs(onPixels[offset + 1] - offPixels[offset + 1]),
        Math.abs(onPixels[offset + 2] - offPixels[offset + 2]),
      );
      if (delta < 8) continue;
      changedPixels += 1;
      if (
        offPixels[offset] >= 245
        && offPixels[offset + 1] >= 245
        && offPixels[offset + 2] >= 245
      ) {
        changedOnBackground += 1;
      }
    }
    return { changedPixels, changedOnBackground };
  }, [linksOn.toString("base64"), linksOff.toString("base64")] as const);
}

async function afterAnimationFrame(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

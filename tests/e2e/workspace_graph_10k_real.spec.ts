import { expect, test, type Locator, type Page, type Request, type Response, type TestInfo } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";

import type { GraphRenderScene } from "../../frontend/src/features/graph/shared/rendererTypes";
import type { GraphQueryPayload, GraphQueryRequest } from "../../frontend/src/shared/api/graph";
import type { components } from "../../frontend/src/shared/api/types.gen";
import type { WorkspaceGraphDocumentNode, WorkspaceGraphOverviewRequest, WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";
import { backendBaseUrl, currentWorkspace, e2ePassword, login } from "./auth";
import { createOverviewResponseReader } from "./workspace_graph_response_reader";
import { expectNativeCounts, installNativeDrawMeter, readNativeDraw } from "./workspace_graph_native_draw";

const OWNER_EMAIL = "e2e-graph-10k-owner@example.com";
const DOCUMENT_COUNT = 10_000;
const RELATION_COUNT = 30_895;
const CONNECTED_DOCUMENT_COUNT = 9_660;
const DEFAULT_BUDGET = { documents: 2_000, relations: 6_000 };
const PC_BUDGET = { documents: 10_000, relations: 100_000 };
const OVERVIEW_PATH = "/graph/overview/query";
const ANDROID_USER_AGENT = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36";
type WorkspaceMapExport = GraphRenderScene & {
  graph_revision: string; representation: string; completeness: string;
};
// Trace is a worker fixture, so describe.use cannot override it. Only this
// extended test type disables credential recording; the base cases keep theirs.
const accountTransitionTest = test.extend({ trace: "off" });

// Only the existing run-e2e.mjs fresh-SQLite wrapper supplies this fixture.
// No interception, synthetic browser scene, direct endpoint budget override,
// or server-authorization claim is used by the browser cases below.
test.describe("Workspace Knowledge Map real-API PC10K (opt-in)", () => {
  test.skip(process.env.DTXWIKI_E2E_GRAPH_10K !== "1",
    "Set DTXWIKI_E2E_GRAPH_10K=1 through npm run test:e2e to seed this independent corpus");
  test.use({ viewport: { width: 1440, height: 900 } });

  test("explicit PC choice preserves all 10K authored identities and Combined topology", async ({ page, browserName }, testInfo) => {
    test.skip(browserName !== "chromium", "This native WebGL capacity acceptance targets Chromium");
    test.setTimeout(240_000);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await login(page.request, OWNER_EMAIL);
    const workspace = await currentWorkspace(page.request);
    const diagnostics = watchBrowserDiagnostics(page);
    const overviewRequests = watchOverviewRequests(page);
    const responseReader = await createOverviewResponseReader(page, workspace.id);
    try {
      const initialRead = waitForOverview(page, DEFAULT_BUDGET);
      await page.goto("/graph");
      const initialResponse = await initialRead;
      expectOverviewRequest(initialResponse.request(), workspace.id, DEFAULT_BUDGET);
      const initial = await responseReader.read(initialResponse);
      expectCandidateUniverse(initial);
      expect(initial.representation).toBe("aggregated");
      expect(initial.nodes.length).toBeLessThanOrEqual(DEFAULT_BUDGET.documents);
      expect(initial.edges.length).toBeLessThanOrEqual(DEFAULT_BUDGET.relations);
      await expect(page.getByLabel("Lens", { exact: true })).toHaveValue("combined");
      await expectRendererReady(page);

      const view = page.locator("details.workspace-graph-view-options");
      await view.locator("summary").click();
      const scale = page.getByRole("combobox", { name: "Map node limit", exact: true });
      await expect(scale).toHaveValue("2k");
      const fullRead = waitForOverview(page, PC_BUDGET);
      await scale.selectOption("10k");
      const response = await fullRead;
      expectOverviewRequest(response.request(), workspace.id, PC_BUDGET);
      const scene = await responseReader.read(response);
      expectExactAuthoredScene(scene);
      await expect(scale).toHaveValue("10k");
      await view.locator("summary").click();

      const nodeList = page.getByTestId("workspace-graph-node-list");
      const edgeList = page.getByTestId("workspace-graph-edge-list");
      const canvas = page.getByTestId("graph-canvas");
      await expect(nodeList).toHaveAttribute("data-total-count", String(CONNECTED_DOCUMENT_COUNT));
      await expect(canvas).toHaveAttribute("data-map-node-count", String(CONNECTED_DOCUMENT_COUNT));
      const showUnlinked = page.getByRole("button", { name: /^Unlinked \d+$/ });
      await expect(showUnlinked).toHaveAttribute("title", "340 unlinked documents hidden from this map.");
      const readsBeforeDisplay = overviewRequests.length;
      await showUnlinked.click();
      await expect(nodeList).toHaveAttribute("data-total-count", String(DOCUMENT_COUNT));
      await expect(edgeList).toHaveAttribute("data-total-count", String(RELATION_COUNT));
      await expect(canvas).toHaveAttribute("data-map-node-count", String(DOCUMENT_COUNT));
      await expect(canvas).toHaveAttribute("data-map-edge-count", String(RELATION_COUNT));
      await expectRendererReady(page);
      await afterAnimationFrame(page);
      expect(overviewRequests).toHaveLength(readsBeforeDisplay);
      const gpu = await expectLiveWebGL(canvas);
      await expectBoundedRows(nodeList, edgeList);

      // Download the production visible-map export, not a renderer test hook.
      // Compare every identity/endpoint/kind to the exact production API scene.
      const exported = await downloadVisibleMap(page);
      expect(exported.graph_revision).toBe(scene.graph_revision);
      expect(exported.representation).toBe("exact");
      expect(exported.completeness).toBe("complete");
      expectVisibleExportIdentity(scene, exported);

      // End must reach the last original wire ID, not a renamed/sample row.
      const lastNode = scene.nodes.at(-1);
      if (!lastNode || lastNode.grain !== "document") throw new Error("Exact scene has no last document");
      await nodeList.locator("button.workspace-graph-list-select").first().focus();
      await page.keyboard.press("End");
      const lastNodeRow = nodeList.locator(`li[aria-posinset="${DOCUMENT_COUNT}"]`);
      await expect(lastNodeRow).toHaveAttribute("data-graph-node-id", lastNode.id);
      const lastNodeButton = lastNodeRow.locator("button.workspace-graph-list-select");
      await expect(lastNodeButton).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(lastNodeButton).toHaveAttribute("aria-pressed", "true");
      await expect(page.locator(".workspace-graph-details").getByRole("heading", {
        name: lastNode.label, exact: true,
      })).toBeVisible();
      const lastEdge = scene.edges.at(-1);
      if (!lastEdge) throw new Error("Exact scene has no last authored relationship");
      await edgeList.locator("button.workspace-graph-list-select").first().focus();
      await page.keyboard.press("End");
      const lastEdgeRow = edgeList.locator(`li[aria-posinset="${RELATION_COUNT}"]`);
      await expect(lastEdgeRow).toHaveAttribute("data-graph-edge-id", lastEdge.id);
      await expect(lastEdgeRow.locator("button.workspace-graph-list-select")).toBeFocused();
      await expectBoundedRows(nodeList, edgeList);
      await capture(page, testInfo, "real-api-pc10k-original-id-end.png");
      await capture(canvas, testInfo, "real-api-pc10k-native-canvas.png");

      await view.locator("summary").click();
      const boundedRead = waitForOverview(page, DEFAULT_BUDGET);
      await scale.selectOption("2k");
      const boundedResponse = await boundedRead;
      expectOverviewRequest(boundedResponse.request(), workspace.id, DEFAULT_BUDGET);
      const bounded = await responseReader.read(boundedResponse);
      expectCandidateUniverse(bounded);
      expect(bounded.representation).toBe("aggregated");
      expect(bounded.nodes.length).toBeLessThanOrEqual(DEFAULT_BUDGET.documents);
      expect(bounded.edges.length).toBeLessThanOrEqual(DEFAULT_BUDGET.relations);
      const boundedCanvasEdges = expectAggregatedSelfLoopPolicy(bounded);
      await expectRendererReady(page);
      await expect(canvas).toHaveAttribute("data-map-node-count", String(bounded.nodes.length));
      await expect(canvas).toHaveAttribute("data-map-edge-count", String(boundedCanvasEdges));
      await expect(nodeList).toHaveAttribute("data-total-count", String(bounded.nodes.length));
      await expect(edgeList).toHaveAttribute("data-total-count", String(bounded.edges.length));
      await expect(scale).toHaveValue("2k");
      await view.locator("summary").click();
      expectAggregatedExportIdentity(bounded, await downloadVisibleMap(page));
      expect(overviewRequests.map((request) => request.budget)).toEqual([DEFAULT_BUDGET, PC_BUDGET, DEFAULT_BUDGET]);
      expect(diagnostics.pageErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
      await attachJson(testInfo, "real-api-pc10k-evidence.json", {
        source: "isolated synthetic.v3 seed260917 production API, visible export and native Chromium WebGL2",
        scope: "exact identity/topology/budget/list and GL-health proof; not native edge-ink, physics or server-authorization proof",
        requests: overviewRequests, counts: scene.counts, relationKinds: relationKindCounts(scene),
        topology: { folders: scene.folder_topology!.folders.length, memberships: scene.folder_topology!.memberships.length,
          edgeSlices: scene.folder_topology!.edge_slices.length },
        originalLastNodeId: lastNode.id, originalLastEdgeId: lastEdge.id, gpu,
        sameOriginalUiResponses: responseReader.evidence(),
        boundedReturn: { apiListExportEdges: bounded.edges.length, canvasEdges: boundedCanvasEdges,
          aggregateSelfLoopIds: bounded.edges.filter((edge) => edge.source === edge.target).map((edge) => edge.id) },
      });
    } finally {
      await responseReader.dispose();
    }
  });

  test("wide Android touch device remains 2K and rotation alone issues no overview read", async ({ browser, browserName }, testInfo) => {
    // isMobile is unsupported by Firefox; create the Android context only
    // after this engine gate rather than altering the shared project config.
    test.skip(browserName !== "chromium", "Android native WebGL policy acceptance targets Chromium");
    test.setTimeout(180_000);
    const context = await browser.newContext({
      baseURL: backendBaseUrl, viewport: { width: 1180, height: 820 },
      userAgent: ANDROID_USER_AGENT, isMobile: true, hasTouch: true,
      reducedMotion: "reduce",
    });
    let responseReader: Awaited<ReturnType<typeof createOverviewResponseReader>> | null = null;
    try {
      const page = await context.newPage();
      await login(page.request, OWNER_EMAIL);
      const workspace = await currentWorkspace(page.request);
      const diagnostics = watchBrowserDiagnostics(page);
      const overviewRequests = watchOverviewRequests(page);
      responseReader = await createOverviewResponseReader(page, workspace.id);
      const initialRead = waitForOverview(page, DEFAULT_BUDGET);
      await page.goto("/graph");
      const response = await initialRead;
      expectOverviewRequest(response.request(), workspace.id, DEFAULT_BUDGET);
      const scene = await responseReader.read(response);
      expectCandidateUniverse(scene);
      expect(scene.representation).toBe("aggregated");
      expect(scene.nodes.length).toBeLessThanOrEqual(DEFAULT_BUDGET.documents);
      expect(scene.edges.length).toBeLessThanOrEqual(DEFAULT_BUDGET.relations);
      const canvasEdges = expectAggregatedSelfLoopPolicy(scene);
      await expectRendererReady(page);
      const canvas = page.getByTestId("graph-canvas");
      await expect(canvas).toHaveAttribute("data-map-node-count", String(scene.nodes.length));
      await expect(canvas).toHaveAttribute("data-map-edge-count", String(canvasEdges));
      await expect(page.getByTestId("workspace-graph-node-list"))
        .toHaveAttribute("data-total-count", String(scene.nodes.length));
      await expect(page.getByTestId("workspace-graph-edge-list"))
        .toHaveAttribute("data-total-count", String(scene.edges.length));
      const gpu = await expectLiveWebGL(canvas);
      const view = page.locator("details.workspace-graph-view-options");
      await view.locator("summary").click();
      await expect(page.getByRole("combobox", { name: "Map node limit", exact: true })).toHaveCount(0);
      await expect(view.getByText("Mobile and unrecognized devices use a maximum of 2,000 map nodes.", { exact: true }))
        .toBeVisible();
      const signals = await page.evaluate(() => ({
        userAgent: navigator.userAgent, maxTouchPoints: navigator.maxTouchPoints,
        width: window.innerWidth, height: window.innerHeight,
      }));
      expect(signals.userAgent).toContain("Android");
      expect(signals.maxTouchPoints).toBeGreaterThan(0);
      expect(signals.width).toBeGreaterThan(640);
      await view.locator("summary").click();
      expectAggregatedExportIdentity(scene, await downloadVisibleMap(page));
      await view.locator("summary").click();
      const readsBeforeRotation = overviewRequests.length;
      await page.setViewportSize({ width: 820, height: 1180 });
      await afterAnimationFrame(page);
      // A bounded 300ms late-query observation after the resize effects, not
      // a performance benchmark or an unlimited no-future-query guarantee.
      await page.waitForTimeout(300);
      await expectRendererReady(page);
      expect(overviewRequests).toHaveLength(readsBeforeRotation);
      expect(overviewRequests.map((request) => request.budget)).toEqual([DEFAULT_BUDGET]);
      await expect(page.getByRole("combobox", { name: "Map node limit", exact: true })).toHaveCount(0);
      expect(Number(await canvas.getAttribute("data-map-node-count"))).toBeLessThanOrEqual(DEFAULT_BUDGET.documents);
      expect(Number(await canvas.getAttribute("data-map-edge-count"))).toBeLessThanOrEqual(DEFAULT_BUDGET.relations);
      await expect(canvas).toHaveAttribute("data-map-node-count", String(scene.nodes.length));
      await expect(canvas).toHaveAttribute("data-map-edge-count", String(canvasEdges));
      const rotatedGpu = await expectLiveWebGL(canvas);
      expect(diagnostics.pageErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
      await capture(page, testInfo, "real-api-mobile-2k-wide-rotation.png");
      await attachJson(testInfo, "real-api-mobile-2k-evidence.json", {
        source: "real Android UA/isMobile/hasTouch context and isolated production overview requests",
        scope: "client device-budget and rotation with a 300ms late-query observation only; not server permissions, performance or unlimited no-future-query proof",
        signals, rotatedViewport: page.viewportSize(), requests: overviewRequests, counts: scene.counts, gpu, rotatedGpu,
        sameOriginalUiResponses: responseReader.evidence(),
        apiListExportEdges: scene.edges.length, canvasEdges,
        aggregateSelfLoopIds: scene.edges.filter((edge) => edge.source === edge.target).map((edge) => edge.id),
      });
    } finally {
      await responseReader?.dispose();
      await context.close();
    }
  });

  // View/Graph/History and browser Back are covered by the real 2K reader
  // journey. PC10K retains canonical focus and the new-mount 2K policy below.


  test("keyboard Explore from 10K Context uses its real Local canonical link to return to a focused 2K Map", async ({ page, browserName }, testInfo) => {
    test.skip(browserName !== "chromium", "This real-route native WebGL navigation acceptance targets Chromium");
    test.setTimeout(240_000);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await login(page.request, OWNER_EMAIL);
    const workspace = await currentWorkspace(page.request);
    const diagnostics = watchBrowserDiagnostics(page);
    const failedReads = watchFailedWorkspaceReads(page);
    const overviewRequests = watchOverviewRequests(page);
    const localRequests = watchLocalRequests(page, workspace.id);
    const responseReader = await createOverviewResponseReader(page, workspace.id);
    try {
      const { initial, full, document, gpu } = await selectReal10kContextDocument(page, workspace.id, responseReader);
      const explore = page.locator(".workspace-graph-details").getByRole("link", { name: "Explore", exact: true });
      await expect(explore).toHaveAttribute("href", `/workspaces/${workspace.id}/context/pages/${document.entity_id}?tab=graph`);
      const detailRead = waitForContextDetail(page, workspace.id, document.entity_id);
      const currentRead = waitForLocalQuery(page, workspace.id, document.entity_id, "current");
      await explore.focus();
      await expect(explore).toBeFocused();
      await page.keyboard.press("Enter");
      const detail = await expectRealContextDetail(await detailRead, workspace.id, document);
      const local = await expectRealLocalQuery(await currentRead, workspace.id, document.entity_id, "current");
      await expectContextRoute(page, workspace.id, document.entity_id, "graph");
      const tabs = page.getByRole("tablist", { name: "Context reader views", exact: true });
      await expect(tabs.getByRole("tab", { name: /^Graph/ })).toHaveAttribute("aria-selected", "true");
      const graphPanel = page.getByRole("tabpanel", { name: /^Graph/ });
      await expectLocalRendererReady(graphPanel);
      const seed = local.nodes.find((node) => node.is_seed && node.entity_kind === "context_page"
        && node.entity_id === document.entity_id);
      if (!seed) throw new Error("The real Explore Local scene omitted the canonical Context seed entity");
      const originalSeedButton = graphPanel.locator(".graph-relationship-list")
        .getByRole("list", { name: "Nodes", exact: true })
        .locator(`button.graph-list-select[data-local-graph-id="${seed.id}"]`);
      await originalSeedButton.click();
      await expect(originalSeedButton).toHaveAttribute("aria-pressed", "true");
      const showInMap = graphPanel.getByRole("complementary", { name: "Details", exact: true })
        .getByRole("link", { name: "Show in Knowledge Map", exact: true });
      // Map focus is the canonical Context entity UUID, not either engine's
      // wire ID, a tree placement ID, slug or a replacement test-only route.
      await expect(showInMap).toHaveAttribute("href", `/workspaces/${workspace.id}/graph?focus=${document.entity_id}`);
      await capture(page, testInfo, "real-api-pc10k-local-canonical-map-link.png");
      const focusRead = waitForOverview(page, DEFAULT_BUDGET);
      await showInMap.click();
      const focusResponse = await focusRead;
      expectOverviewRequest(focusResponse.request(), workspace.id, DEFAULT_BUDGET);
      expect(focusResponse.request().postDataJSON().focus_entity_id).toBe(document.entity_id);
      const focused = await responseReader.read(focusResponse);
      await expect(page).toHaveURL(new RegExp(`/graph\\?focus=${document.entity_id}$`));
      const focusedCanvasEdges = expectNavigationColumnEligibility(focused);
      await expectNavigationMapReady(page, focused, focusedCanvasEdges);
      const focus = focused.focus;
      if (!focus || focus.status !== "document") throw new Error("The original Context document already visible at 2K must remain a document focus");
      const focusedNode = focused.nodes.find((node) => node.id === focus.node_id);
      if (!focusedNode || focusedNode.grain !== "document") throw new Error("The real document focus named no original document in its visible scene");
      expect(focusedNode.entity_kind).toBe("context");
      expect(focusedNode.entity_id).toBe(document.entity_id);
      expect(focusedNode.id).toBe(document.id);
      const focusLabel = page.getByTestId("graph-canvas")
        .locator(`[data-workspace-map-labels] span[data-node-id="${focusedNode.id}"]`);
      const readFocusPoint = () => focusLabel.evaluate((label) => {
        const element = label as HTMLElement;
        const bounds = element.closest(".graph-canvas")!.getBoundingClientRect();
        const x = Number(element.dataset.pointX), y = Number(element.dataset.pointY);
        return { id: element.dataset.nodeId, x, y, width: bounds.width, height: bounds.height,
          unclampedPointAvailable: element.hasAttribute("data-point-x") && element.hasAttribute("data-point-y") };
      });
      await expect.poll(async () => {
        const point = await readFocusPoint();
        return point.unclampedPointAvailable && Number.isFinite(point.x) && Number.isFinite(point.y)
          && point.x > 16 && point.x < point.width - 16 && point.y > 16 && point.y < point.height - 16;
      }, { message: "The original focused GPU point, not its clamped full title, must be visible before any manual Center/Fit/Zoom" }).toBe(true);
      const actualFocusPoint = await readFocusPoint();
      await expect(page.getByTestId("workspace-graph-node-list")
        .locator(`li[data-graph-node-id="${focus.node_id}"] button.workspace-graph-list-select`))
        .toHaveAttribute("aria-pressed", "true");
      await expect(page.locator(".workspace-graph-details").getByRole("heading", { name: focusedNode.label, exact: true })).toBeVisible();
      await expect(page.getByRole("searchbox", { name: "Find in visible map", exact: true })).toHaveValue("");
      await expectDefaultMapScale(page);
      expectNavigationExportIdentity(focused, await downloadVisibleMap(page));
      expect(overviewRequests.map((request) => request.budget)).toEqual([DEFAULT_BUDGET, PC_BUDGET, DEFAULT_BUDGET]);
      expect(overviewRequests.map((request) => request.focus_entity_id ?? null)).toEqual([null, null, document.entity_id]);
      expect(localRequests.map((request) => request.time?.mode)).toEqual(["current"]);
      expect(failedReads).toEqual([]);
      expect(diagnostics.pageErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
      await capture(page, testInfo, "real-api-pc10k-context-focused-default2k.png");
      await attachJson(testInfo, "real-api-pc10k-context-focus-evidence.json", {
        source: "keyboard activation of the actual Map Explore anchor and actual Context Local Show in Knowledge Map anchor",
        scope: "same-owner canonical entity navigation, new-mount 2K policy and response-driven aggregate identities; not server permission or fixed unfocused geometry proof",
        requests: overviewRequests, localRequests, initialCounts: initial.counts, departureCounts: full.counts, focusedCounts: focused.counts,
        canonicalContextId: document.entity_id, originalMapNodeId: document.id, originalLocalSeedId: seed.id, focus,
        actualFocusPoint,
        contextDetail: { id: detail.id, workspaceId: detail.workspace_id, title: detail.title },
        focusApiListExportEdges: focused.edges.length, focusCanvasEdges: focusedCanvasEdges,
        aggregateSelfLoopIds: focused.edges.filter((edge) => edge.source === edge.target).map((edge) => edge.id), gpu,
        sameOriginalUiResponses: responseReader.evidence(),
      });
    } finally {
      await responseReader.dispose();
    }
  });

  test("actual same-actor workspace switching isolates 10K and retains only returned 2K-visible state before explicit PC choice", async ({ page, browserName }, testInfo) => {
    test.skip(browserName !== "chromium", "This real workspace transition targets Chromium native WebGL");
    test.setTimeout(240_000);
    // Only the standard wrapper owns this disposable database. No operator
    // workspace or pre-existing database is mutated by secondary setup.
    expect(process.env.DTXWIKI_E2E_DATABASE_PATH).toBeTruthy();
    await page.emulateMedia({ reducedMotion: "reduce" });
    const ownerLogin = await login(page.request, OWNER_EMAIL);
    const actorId = (await ownerLogin.json()).data.user.user_id as string;
    const primary = await currentWorkspace(page.request);
    const title = `10K scope ${testInfo.workerIndex} ${Date.now()}`;
    const create = await page.request.post(`${backendBaseUrl}/api/v1/workspaces`, { data: { title } });
    expect(create.ok()).toBe(true);
    const secondary = (await create.json()).data as { id: string; title: string };
    expect(secondary.id).not.toBe(primary.id);
    const policies = await page.request.get(`${backendBaseUrl}/api/v1/workspaces/${secondary.id}/settings/policies`);
    expect(policies.ok()).toBe(true);
    const graphPolicy = (await policies.json()).data.graph as { revision: string };
    const enabled = await page.request.patch(`${backendBaseUrl}/api/v1/workspaces/${secondary.id}/settings/policies/batch`, {
      data: { group: "graph", expected_revision: graphPolicy.revision, changes: { workspace_map_enabled: true } },
    });
    expect(enabled.ok()).toBe(true);
    await installNativeDrawMeter(page);
    const diagnostics = watchBrowserDiagnostics(page);
    const failedReads = watchFailedWorkspaceReads(page);
    const requests = watchOverviewRequests(page);
    const departureReader = await createOverviewResponseReader(page, primary.id);
    let secondaryReader: Awaited<ReturnType<typeof createOverviewResponseReader>> | null = null;
    let returnReader: Awaited<ReturnType<typeof createOverviewResponseReader>> | null = null;
    try {
      const { full, document } = await selectReal10kContextDocument(page, primary.id, departureReader);
      const drag = await pinOriginalAccountDocument(page, document, testInfo);
      const before = (await readAccountMapState(page, actorId, primary.id))!;
      expect(before.selectedId).toBe(document.id);
      expect(before.pinnedIds).toContain(document.id);
      const oldIds = new Set(full.nodes.map(node => node.id));
      const oldCanvas = await page.getByTestId("graph-canvas").locator("canvas").elementHandle();
      if (!oldCanvas) throw new Error("No departing original 10K native renderer");
      const timeOrigin = await page.evaluate(() => performance.timeOrigin);
      const departureEvidence = departureReader.evidence();
      await departureReader.dispose();

      // Each workspace owns its existing bounded three-response reader. No
      // extra API fetch is substituted for either original UI response.
      secondaryReader = await createOverviewResponseReader(page, secondary.id);
      const secondaryResponse = await openAccountWorkspaceMapThroughUi(page, secondary);
      expectOverviewRequest(secondaryResponse.request(), secondary.id, DEFAULT_BUDGET);
      const secondaryScene = await secondaryReader.read(secondaryResponse);
      expect(secondaryScene.nodes.length).toBeLessThanOrEqual(DEFAULT_BUDGET.documents);
      expect(secondaryScene.edges.length).toBeLessThanOrEqual(DEFAULT_BUDGET.relations);
      expect(secondaryScene.nodes.some(node => oldIds.has(node.id))).toBe(false);
      await expect.poll(() => oldCanvas.evaluate(element => element.isConnected)).toBe(false);
      await oldCanvas.dispose();
      await expectDefaultMapScale(page);
      await expect(page.getByRole("searchbox", { name: "Find in visible map", exact: true })).toHaveValue("");
      await expect(page.locator(".workspace-graph-details")).toHaveCount(0);
      await expect(page.getByTestId("workspace-graph-node-list").locator('[aria-pressed="true"]')).toHaveCount(0);
      if (secondaryScene.nodes.length) {
        const links = expectNavigationColumnEligibility(secondaryScene);
        await expectNavigationMapReady(page, secondaryScene, links);
        await expectNativeCounts(page, secondaryScene.nodes.length, links);
      } else {
        await expect(page.getByTestId("graph-canvas")).toHaveCount(0);
      }
      await expect.poll(async () => (await readAccountMapState(page, actorId, secondary.id))?.viewerId).toBe(actorId);
      const secondaryState = (await readAccountMapState(page, actorId, secondary.id))!;
      expect(secondaryState).toMatchObject({ viewerId: actorId, selectedId: null, pinnedIds: [], pinnedPositions: {}, viewport: null });
      await capture(page, testInfo, "real-api-pc10k-secondary-workspace-default2k.png");
      const secondaryEvidence = secondaryReader.evidence();
      await secondaryReader.dispose();

      returnReader = await createOverviewResponseReader(page, primary.id);
      const boundedResponse = await openAccountWorkspaceMapThroughUi(page, primary);
      expectOverviewRequest(boundedResponse.request(), primary.id, DEFAULT_BUDGET);
      const bounded = await returnReader.read(boundedResponse);
      await expectNavigationMapReady(page, bounded, expectNavigationColumnEligibility(bounded));
      await expectDefaultMapScale(page);
      // The current contract retires a selected wire ID omitted by the
      // authoritative 2K response. Pins and the camera belong to the scale that
      // made them: the bounded 2K scene gets none of the 10K coordinates, and the
      // explicit 10K choice below gets its own back.
      const boundedIds = new Set(bounded.nodes.map(node => node.id));
      const retainedIntent = {
        selectedId: before.selectedId && boundedIds.has(before.selectedId) ? before.selectedId : null,
        pinnedIds: [] as string[],
        pinnedPositions: {} as AccountMapState["pinnedPositions"],
      };
      await expect.poll(async () => {
        const state = await readAccountMapState(page, actorId, primary.id);
        return state ? { selectedId: state.selectedId, pinnedIds: state.pinnedIds, pinnedPositions: state.pinnedPositions } : null;
      }).toEqual(retainedIntent);
      const boundedState = (await readAccountMapState(page, actorId, primary.id))!;
      const view = page.locator("details.workspace-graph-view-options");
      await view.locator("summary").click();
      const explicitRead = waitForOverview(page, PC_BUDGET);
      await page.getByRole("combobox", { name: "Map node limit", exact: true }).selectOption("10k");
      const returnedResponse = await explicitRead;
      expectOverviewRequest(returnedResponse.request(), primary.id, PC_BUDGET);
      const returned = await returnReader.read(returnedResponse);
      expectExactAuthoredScene(returned);
      await view.locator("summary").click();
      await expectNavigationMapReady(page, returned, RELATION_COUNT);
      await expectNativeCounts(page, DOCUMENT_COUNT, RELATION_COUNT);
      expect(new Set(returned.nodes.map(node => node.id))).toEqual(oldIds);
      const relationIdentity = (edge: WorkspaceGraphScene["edges"][number]) => JSON.stringify([
        edge.id, edge.source, edge.target, edge.direction, edge.relation_kind, edge.relation_count, edge.occurrence_weight,
      ]);
      expect(new Set(returned.edges.map(relationIdentity))).toEqual(new Set(full.edges.map(relationIdentity)));
      if (retainedIntent.selectedId === document.id) {
        await expect(page.locator(".workspace-graph-details").getByRole("heading", { name: document.label, exact: true })).toBeVisible();
      } else {
        await expect(page.locator(".workspace-graph-details")).toHaveCount(0);
      }
      await expect.poll(async () => (await readAccountMapState(page, actorId, primary.id))?.selectedId).toBe(retainedIntent.selectedId);
      expect(boundedState.pinnedIds).toEqual([]);
      // The 10K view is saved after a short debounce, so poll for the pins it restored.
      await expect.poll(async () => (await readAccountMapState(page, actorId, primary.id))?.pinnedIds).toEqual(before.pinnedIds);
      const restored = (await readAccountMapState(page, actorId, primary.id))!;
      expect(restored.pinnedPositions).toEqual(before.pinnedPositions);
      expect(restored.viewerId).toBe(actorId);
      expect(await page.evaluate(() => performance.timeOrigin)).toBe(timeOrigin);
      expect(requests.map(request => request.budget)).toEqual([DEFAULT_BUDGET, PC_BUDGET, DEFAULT_BUDGET, DEFAULT_BUDGET, PC_BUDGET]);
      expect(failedReads).toEqual([]);
      expect(diagnostics.pageErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
      await capture(page, testInfo, "real-api-pc10k-workspace-return-explicit10k.png");
      await attachJson(testInfo, "real-api-pc10k-workspace-transition-evidence.json", {
        source: "same Page actual workspace switcher, versioned standard setup APIs and original UI overview responses",
        scope: "same-actor workspace isolation, authoritative 2K-visible intent retention and fresh explicit PC scale; pins belong to the scale that made them; not hidden-document authorization proof",
        actorId, primaryId: primary.id, secondaryId: secondary.id, originalId: document.id,
        requests, before, secondaryState, boundedState, retainedIntent, restored, drag, native: await readNativeDraw(page),
        sameOriginalUiResponses: [...departureEvidence, ...secondaryEvidence, ...returnReader.evidence()],
      });
    } finally {
      await departureReader.dispose();
      await secondaryReader?.dispose();
      await returnReader?.dispose();
    }
  });

  test.describe("actual account transitions without credential trace retention", () => {
    accountTransitionTest("actual same-browser owner-viewer-owner sign-ins purge 10K Map state and return to fresh 2K", async ({ page, request, browserName, trace }, testInfo) => {
      test.skip(browserName !== "chromium", "This actual account transition acceptance targets Chromium native WebGL");
      test.setTimeout(240_000);
      expect(trace, "This credential-flow case must never retain a trace").toBe("off");
      await page.emulateMedia({ reducedMotion: "reduce" });
      const ownerLogin = await login(page.request, OWNER_EMAIL);
      const ownerId = (await ownerLogin.json()).data.user.user_id as string;
      const workspace = await currentWorkspace(page.request);
      await installNativeDrawMeter(page);
      const diagnostics = watchBrowserDiagnostics(page);
      const failedReads = watchFailedWorkspaceReads(page);
      const overviewRequests = watchOverviewRequests(page);
      const responseReader = await createOverviewResponseReader(page, workspace.id);
      const viewerEmail = `e2e-graph-10k-viewer-${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}@example.com`;
      let ownerReturnReader: Awaited<ReturnType<typeof createOverviewResponseReader>> | null = null;
      let viewerId: string | null = null;
      let membershipAdded = false;
      try {
        // The independent API fixture owns setup cookies. Browser account
        // transitions below must exclusively use the actual login/logout UI.
        const signup = await request.post(`${backendBaseUrl}/api/v1/auth/signup`, {
          data: { email: viewerEmail, password: e2ePassword },
        });
        expect(signup.ok()).toBe(true);
        viewerId = (await signup.json()).data.user.user_id as string;
        expect(viewerId).not.toBe(ownerId);
        const member = await page.request.post(`${backendBaseUrl}/api/v1/workspaces/${workspace.id}/members`, {
          data: { email: viewerEmail, role: "viewer", confirm_first_share: true },
        });
        expect(member.ok()).toBe(true);
        expect((await member.json()).data.member).toMatchObject({ user_id: viewerId, role: "viewer" });
        membershipAdded = true;

        const { initial, full, document } = await selectReal10kContextDocument(page, workspace.id, responseReader);
        const timeOrigin = await page.evaluate(() => performance.timeOrigin);
        await page.evaluate(() => sessionStorage.setItem("e2e:graph-account-marker", "unrelated"));
        const drag = await pinOriginalAccountDocument(page, document, testInfo);
        const themeRoundtrip = await retainPinned10kThroughThemes(page, ownerId, workspace.id, document, full, testInfo);
        expect(overviewRequests.map((item) => item.budget)).toEqual([DEFAULT_BUDGET, PC_BUDGET]);
        const ownerState = await readAccountMapState(page, ownerId, workspace.id);
        expect(ownerState).not.toBeNull();
        expect(ownerState!.viewerId).toBe(ownerId);
        expect(ownerState!.selectedId).toBe(document.id);
        expect(ownerState!.pinnedIds).toContain(document.id);
        expect(ownerState!.pinnedPositions[document.id]).toMatchObject({ x: expect.any(Number), y: expect.any(Number) });
        expect(ownerState!.viewportEngine).toBe("cosmos");
        expectFiniteAccountViewport(ownerState!.viewport);
        await expect(page.getByRole("searchbox", { name: "Find in visible map", exact: true })).toHaveValue(document.label);
        await capture(page, testInfo, "real-api-pc10k-owner-before-actual-logout.png");

        await test.step("Owner signs out through the real Account UI", () => signOutAccountThroughUi(page, timeOrigin));
        const viewerSessionId = await test.step("Viewer signs in through the actual credential form", () =>
          signInAccountThroughUi(page, viewerEmail, timeOrigin));
        expect(viewerSessionId).toBe(viewerId);
        const viewerRead = await test.step("Viewer chooses the same workspace and opens its default Map", () =>
          openAccountWorkspaceMapThroughUi(page, workspace));
        expectOverviewRequest(viewerRead.request(), workspace.id, DEFAULT_BUDGET);
        expectFreshAccountOverviewRequest(viewerRead.request());
        const viewerScene = await responseReader.read(viewerRead);
        expectCandidateUniverse(viewerScene);
        expectAccountPublishedUniverse(viewerScene, initial);
        const viewerState = await expectFreshAccountMap(page, viewerId, workspace.id, viewerScene, ownerState!, drag.changedNativeViewport);
        const viewerWorkspaceRead = await page.request.get(`${backendBaseUrl}/api/v1/workspaces`);
        expect(viewerWorkspaceRead.ok()).toBe(true);
        expect((await viewerWorkspaceRead.json()).data.workspaces.find((item: { id: string }) => item.id === workspace.id))
          .toMatchObject({ id: workspace.id, role: "viewer" });
        await capture(page, testInfo, "real-api-pc10k-viewer-fresh-default2k.png");

        // Keep the original bounded three-read CDP oracle intact. The fourth
        // UI read gets its own public CDP session on this SAME Page/realm.
        const firstUiResponseEvidence = responseReader.evidence();
        await responseReader.dispose();
        ownerReturnReader = await createOverviewResponseReader(page, workspace.id);

        await test.step("Viewer signs out through the real Account UI", () => signOutAccountThroughUi(page, timeOrigin));
        expect(await test.step("Owner returns through the actual credential form", () =>
          signInAccountThroughUi(page, OWNER_EMAIL, timeOrigin))).toBe(ownerId);
        const ownerReturnRead = await test.step("Returned owner chooses the same workspace and opens its default Map", () =>
          openAccountWorkspaceMapThroughUi(page, workspace));
        expectOverviewRequest(ownerReturnRead.request(), workspace.id, DEFAULT_BUDGET);
        expectFreshAccountOverviewRequest(ownerReturnRead.request());
        const ownerReturnScene = await ownerReturnReader.read(ownerReturnRead);
        expectCandidateUniverse(ownerReturnScene);
        expectAccountPublishedUniverse(ownerReturnScene, initial);
        const returnedOwnerState = await expectFreshAccountMap(page, ownerId, workspace.id, ownerReturnScene, ownerState!, drag.changedNativeViewport);
        expect(await readAccountMapState(page, viewerId, workspace.id)).toBeNull();
        expect(await page.evaluate(() => performance.timeOrigin)).toBe(timeOrigin);
        expect(overviewRequests.map((item) => item.budget)).toEqual([DEFAULT_BUDGET, PC_BUDGET, DEFAULT_BUDGET, DEFAULT_BUDGET]);
        expect(failedReads).toEqual([]);
        expect(diagnostics.pageErrors).toEqual([]);
        expect(diagnostics.consoleErrors).toEqual([]);
        await capture(page, testInfo, "real-api-pc10k-owner-return-fresh-default2k.png");
        await attachJson(testInfo, "real-api-pc10k-actual-account-transition-evidence.json", {
          source: "same Page actual account logout/login forms, original production overview responses and native original-document drag",
          scope: "actor-scoped scene/UI/storage/camera reset in one all-published synthetic workspace; not hidden-document privacy or target-hardware performance proof",
          workspaceId: workspace.id, ownerId, viewerId, timeOrigin,
          originalDocument: { id: document.id, entityId: document.entity_id, label: document.label },
          requests: overviewRequests, departureCounts: full.counts, viewerCounts: viewerScene.counts, ownerReturnCounts: ownerReturnScene.counts,
          ownerState, viewerState, returnedOwnerState, drag, themeRoundtrip,
          sameOriginalUiResponses: [...firstUiResponseEvidence, ...ownerReturnReader.evidence()],
        });
      } finally {
        await responseReader.dispose();
        await ownerReturnReader?.dispose();
        if (membershipAdded && viewerId && testInfo.status !== "timedOut") {
          await login(request, OWNER_EMAIL);
          const removed = await request.delete(`${backendBaseUrl}/api/v1/workspaces/${workspace.id}/members/${viewerId}`);
          expect(removed.ok(), "Remove only this case's synthetic viewer membership from the wrapper-owned temporary database").toBe(true);
        }
      }
    });
  });
});

type AccountMapState = {
  viewerId: string; selectedId: string | null; pinnedIds: string[];
  pinnedPositions: Record<string, { x: number; y: number }>;
  viewport: { zoom: number; pan: { x: number; y: number } } | null;
  viewportEngine?: "cosmos"; lens: string;
  display: { show_edges: boolean; show_labels: boolean; show_orphans: boolean };
  dataFilters: { search: string; document_kinds: string[]; time_basis: string;
    time_from: string | null; time_to: string | null; quick_filters: string[]; facet_rules: unknown[] };
};

async function readAccountMapState(page: Page, actorId: string, workspaceId: string) {
  return page.evaluate(({ actor, workspace }) => {
    const key = `dtxwiki:workspace-graph:v2:${encodeURIComponent(actor)}:${encodeURIComponent(workspace)}:latest`;
    const stored = sessionStorage.getItem(key);
    return stored === null ? null : JSON.parse(stored).state as AccountMapState;
  }, { actor: actorId, workspace: workspaceId });
}

function expectFiniteAccountViewport(viewport: AccountMapState["viewport"]) {
  expect(viewport).not.toBeNull();
  expect(Number.isFinite(viewport!.zoom) && viewport!.zoom > 0).toBe(true);
  expect(Number.isFinite(viewport!.pan.x) && Number.isFinite(viewport!.pan.y)).toBe(true);
}

async function readAccountNativeViewport(canvas: Locator) {
  return canvas.locator("canvas").evaluate((element) => {
    const zoom = (element as HTMLCanvasElement & { __zoom?: { k: number; x: number; y: number } }).__zoom;
    return zoom ? { nativeZoom: zoom.k, nativeX: zoom.x, nativeY: zoom.y } : null;
  });
}

async function retainPinned10kThroughThemes(
  page: Page, actorId: string, workspaceId: string, document: WorkspaceGraphDocumentNode,
  scene: WorkspaceGraphScene, testInfo: TestInfo,
) {
  const canvas = page.getByTestId("graph-canvas");
  const before = (await readAccountMapState(page, actorId, workspaceId))!;
  expect(before.selectedId).toBe(document.id);
  expect(before.pinnedIds).toContain(document.id);
  const camera = await readAccountNativeViewport(canvas);
  expect(camera).not.toBeNull();
  const originalTheme = await page.evaluate(() => window.document.documentElement.getAttribute("data-theme"));
  const otherTheme = originalTheme === "dark" ? "light" : "dark";
  const nodeList = page.getByTestId("workspace-graph-node-list");
  const edgeList = page.getByTestId("workspace-graph-edge-list");
  const listedCounts = { nodes: await nodeList.getAttribute("data-total-count"), edges: await edgeList.getAttribute("data-total-count") };
  expect(listedCounts.nodes).toBe("1");
  const listedIds = {
    nodes: await nodeList.locator("li").evaluateAll(rows => rows.map(row => row.getAttribute("data-graph-node-id"))),
    edges: await edgeList.locator("li").evaluateAll(rows => rows.map(row => row.getAttribute("data-graph-edge-id"))),
  };
  expect(listedIds.nodes).toEqual([document.id]);
  const phases = [];
  // This account journey intentionally keeps its existing reduced-motion
  // setup. Actual user Pause/Resume remounts have their own normal-motion test.
  await expect(page.getByRole("button", { name: "Layout motion is off", exact: true })).toBeDisabled();
  for (const [phase, theme] of [["opposite", otherTheme], ["restored", originalTheme]] as const) {
    const previous = await canvas.locator("canvas").elementHandle();
    if (!previous) throw new Error("No pinned native 10K renderer before palette remount");
    await page.evaluate(next => {
      if (next === null) window.document.documentElement.removeAttribute("data-theme");
      else window.document.documentElement.setAttribute("data-theme", next);
    }, theme);
    await expect.poll(() => previous.evaluate(element => element.isConnected)).toBe(false);
    await previous.dispose();
    // Search narrows the operable list, never the original native scene.
    // Requiring an unfiltered 10K list here would contradict search retention.
    await expectRendererReady(page);
    await expect(canvas).toHaveAttribute("data-map-node-count", String(scene.nodes.length));
    await expect(canvas).toHaveAttribute("data-map-edge-count", String(RELATION_COUNT));
    await expect(nodeList).toHaveAttribute("data-total-count", listedCounts.nodes!);
    await expect(edgeList).toHaveAttribute("data-total-count", listedCounts.edges!);
    expect(await nodeList.locator("li").evaluateAll(rows => rows.map(row => row.getAttribute("data-graph-node-id")))).toEqual(listedIds.nodes);
    expect(await edgeList.locator("li").evaluateAll(rows => rows.map(row => row.getAttribute("data-graph-edge-id")))).toEqual(listedIds.edges);
    await expect(nodeList.locator(`li[data-graph-node-id="${document.id}"] button.workspace-graph-list-select`)).toHaveAttribute("aria-pressed", "true");
    await expectLiveWebGL(canvas);
    await expectNativeCounts(page, DOCUMENT_COUNT, RELATION_COUNT);
    await expect(page.getByRole("button", { name: "Layout motion is off", exact: true })).toBeDisabled();
    await expect(page.locator(".workspace-graph-details").getByRole("heading", { name: document.label, exact: true })).toBeVisible();
    await expect(page.getByRole("searchbox", { name: "Find in visible map", exact: true })).toHaveValue(document.label);
    const nextCamera = await readAccountNativeViewport(canvas);
    expect(nextCamera).not.toBeNull();
    expect(nextCamera!.nativeZoom).toBeCloseTo(camera!.nativeZoom, 5);
    expect(nextCamera!.nativeX).toBeCloseTo(camera!.nativeX, 2);
    expect(nextCamera!.nativeY).toBeCloseTo(camera!.nativeY, 2);
    const next = (await readAccountMapState(page, actorId, workspaceId))!;
    expect(next.selectedId).toBe(before.selectedId);
    expect(next.pinnedIds).toEqual(before.pinnedIds);
    expect(next.pinnedPositions).toEqual(before.pinnedPositions);
    expect(next.dataFilters).toEqual(before.dataFilters);
    phases.push({ phase, theme, camera: nextCamera, state: next, native: await readNativeDraw(page) });
    await capture(canvas, testInfo, `real-api-pc10k-pinned-theme-${phase}.png`);
  }
  return { entrypoint: "html[data-theme] native renderer remount, not Settings UI", before, camera, listedCounts, listedIds, phases };
}

async function pinOriginalAccountDocument(page: Page, document: WorkspaceGraphDocumentNode, testInfo: TestInfo) {
  const canvas = page.getByTestId("graph-canvas");
  const initialViewport = await readAccountNativeViewport(canvas);
  expect(initialViewport).not.toBeNull();
  await page.locator(".workspace-graph-details").getByRole("button", { name: "Center", exact: true }).click();
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  await page.locator(".workspace-graph-selection-clear").click();
  await afterAnimationFrame(page); // finish Clear selection's intentional row-focus handoff first
  const fit = page.getByRole("button", { name: "Fit graph", exact: true });
  await fit.focus();
  await expect(fit).toBeFocused();
  await canvas.locator("canvas").scrollIntoViewIfNeeded();
  await page.mouse.move(0, 0);
  const label = canvas.locator(`[data-workspace-map-labels] span[data-node-id="${document.id}"]`);
  const fullTitle = canvas.locator(`[data-workspace-map-labels] span[data-full-title=true][data-node-id="${document.id}"]`);
  const preparation: { attempt: number; point: { x: number; y: number }; hoveredIds: string[];
    hoverWaitMs: number; nativePointerHit: boolean }[] = [];
  let point: { x: number; y: number } | null = null;
  let bounds: { x: number; y: number; width: number; height: number } | null = null;
  for (let attempt = 0; attempt <= 6; attempt += 1) {
    await afterAnimationFrame(page);
    await page.waitForTimeout(300); // bounded5Hz production label readback, not inferred title placement
    await expect(fullTitle).toHaveCount(0);
    await expect(label).toBeVisible();
    point = await label.evaluate((element) => {
      if (!element.hasAttribute("data-point-x") || !element.hasAttribute("data-point-y")) {
        throw new Error("The original document label has no native GPU point readback");
      }
      return { x: Number((element as HTMLElement).dataset.pointX), y: Number((element as HTMLElement).dataset.pointY) };
    });
    bounds = await canvas.locator("canvas").boundingBox();
    if (!bounds || !Number.isFinite(point.x) || !Number.isFinite(point.y)
      || point.x < 24 || point.y < 24 || point.x > bounds.width - 42 || point.y > bounds.height - 36) {
      throw new Error(`The original intended document has no in-bounds GPU point before native drag: ${document.id}`);
    }
    const nativePointerHit = await canvas.locator("canvas").evaluate((element, target) =>
      window.document.elementFromPoint(target.x, target.y) === element,
    { x: bounds.x + point.x, y: bounds.y + point.y });
    expect(nativePointerHit, "Original native GPU point must be a real canvas pointer target").toBe(true);
    await page.mouse.move(bounds.x + point.x, bounds.y + point.y);
    // Native picking waits several render frames and an asynchronous GPU
    // readback. Keep the same original point under the pointer until that
    // bounded preparation resolves; leaving after 300ms cancels pending picks.
    const hoverStarted = Date.now();
    let hoveredIds: string[] = [];
    for (let sample = 0; sample <= 15; sample += 1) {
      if (sample > 0) await page.waitForTimeout(100);
      hoveredIds = await canvas.locator("[data-workspace-map-labels] span[data-full-title=true][data-node-id]")
        .evaluateAll((labels) => labels.filter((element) => getComputedStyle(element).display !== "none")
          .map((element) => (element as HTMLElement).dataset.nodeId!));
      if (hoveredIds.includes(document.id)) break;
    }
    preparation.push({ attempt, point, hoveredIds, hoverWaitMs: Date.now() - hoverStarted, nativePointerHit });
    if (hoveredIds.includes(document.id)) {
      await expect(fullTitle).toBeVisible();
      break;
    }
    if (attempt === 6) {
      await attachJson(testInfo, "real-api-pc10k-account-original-drag-preparation-failed.json", { originalId: document.id, preparation });
      throw new Error(`Native hover never picked the original intended document after bounded zoom: ${document.id}`);
    }
    await page.mouse.wheel(0, -240);
    await page.mouse.move(0, 0);
    await fit.focus();
  }
  if (!point || !bounds) throw new Error("No original document native drag preparation");
  await page.mouse.down();
  await page.mouse.move(bounds.x + point.x + 18, bounds.y + point.y + 12, { steps: 10 });
  await page.mouse.up();
  await page.mouse.move(0, 0);
  const startPoint = point;
  await expect.poll(async () => label.evaluate((element, start) => Math.hypot(
    Number((element as HTMLElement).dataset.pointX) - start.x,
    Number((element as HTMLElement).dataset.pointY) - start.y,
  ), startPoint), { message: "The intended original GPU point must actually move before any list recenter" }).toBeGreaterThan(4);
  // Restore the SAME original list selection, never accept a neighbor hit.
  const row = page.getByTestId("workspace-graph-node-list").locator(`li[data-graph-node-id="${document.id}"]`);
  await row.locator("button.workspace-graph-list-select").click();
  await expect(row.locator("button.workspace-graph-list-select")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".workspace-graph-details").getByRole("heading", { name: document.label, exact: true })).toBeVisible();
  const originalStored = () => page.evaluate((id) => Object.keys(sessionStorage)
    .filter((key) => key.startsWith("dtxwiki:workspace-graph:v2:") && key.endsWith(":latest"))
    .map((key) => JSON.parse(sessionStorage.getItem(key)!).state as AccountMapState)
    .find((state) => state.selectedId === id && state.pinnedIds.includes(id)) ?? null, document.id);
  await expect.poll(async () => Boolean(await originalStored())).toBe(true);
  const state = (await originalStored())!;
  expect(state.pinnedIds).toContain(document.id);
  expect(Number.isFinite(state.pinnedPositions[document.id]?.x) && Number.isFinite(state.pinnedPositions[document.id]?.y)).toBe(true);
  expectFiniteAccountViewport(state.viewport);
  const changedNativeViewport = await readAccountNativeViewport(canvas);
  expect(changedNativeViewport).not.toBeNull();
  expect(changedNativeViewport!.nativeZoom > 0 && Object.values(changedNativeViewport!).every(Number.isFinite)).toBe(true);
  expect(changedNativeViewport).not.toEqual(initialViewport);
  const evidence = { originalId: document.id, initialViewport, changedNativeViewport, preparation,
    originalPinnedPosition: state.pinnedPositions[document.id] };
  await attachJson(testInfo, "real-api-pc10k-account-original-drag-evidence.json", evidence);
  return evidence;
}

async function signOutAccountThroughUi(page: Page, timeOrigin: number) {
  // The actual English translation is Account, not Shell's My page fallback.
  const account = page.getByRole("banner").locator('a[href="/account"]');
  await expect(account).toHaveAccessibleName("Account");
  await account.click();
  const logoutRead = page.waitForResponse((response) => response.url() === `${backendBaseUrl}/api/v1/auth/logout`
    && response.request().method() === "POST");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  expect((await logoutRead).ok()).toBe(true);
  await expect(page).toHaveURL((url) => url.pathname === "/login"
    && (url.searchParams.get("next") === null || url.searchParams.get("next") === "/account"));
  expect(await page.evaluate(() => ({
    mapKeys: Object.keys(sessionStorage).filter((key) => key.startsWith("dtxwiki:workspace-graph:")),
    unrelated: sessionStorage.getItem("e2e:graph-account-marker"),
    workspaceId: localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    workspaceSlug: localStorage.getItem("dtxwiki.selectedWorkspaceSlug"),
  }))).toEqual({ mapKeys: [], unrelated: "unrelated", workspaceId: null, workspaceSlug: null });
  expect(await page.evaluate(() => performance.timeOrigin)).toBe(timeOrigin);
}

async function signInAccountThroughUi(page: Page, email: string, timeOrigin: number) {
  // The auth guard may preserve /account when sign-out unmounts its route.
  // Follow that actual safe next intent, not an assumed dashboard redirect.
  const next = new URL(page.url()).searchParams.get("next") ?? "/dashboard";
  expect(["/account", "/dashboard"]).toContain(next);
  await page.locator(".auth-form input[name=email]").fill(email);
  await page.locator(".auth-form input[name=password]").fill(e2ePassword);
  const loginRead = page.waitForResponse((response) => response.url() === `${backendBaseUrl}/api/v1/auth/login`
    && response.request().method() === "POST");
  await page.locator(".auth-form button[type=submit]").click();
  const response = await loginRead;
  expect(response.ok()).toBe(true);
  const actorId = (await response.json()).data.user.user_id as string;
  await expect(page).toHaveURL((url) => url.pathname === next && url.search === "");
  expect(await page.evaluate(() => performance.timeOrigin)).toBe(timeOrigin);
  return actorId;
}

async function openAccountWorkspaceMapThroughUi(page: Page, workspace: { id: string; title: string }) {
  await page.locator(".workspace-switcher-button").click();
  const switcher = page.getByRole("dialog", { name: "Workspace switcher", exact: true });
  await switcher.locator(".workspace-menu-item").filter({ has: page.getByText(workspace.title, { exact: true }) }).click();
  await expect(switcher).not.toBeVisible();
  await expect(page.locator(".workspace-switcher-button")).toHaveAttribute("aria-label", `Switch workspace, current: ${workspace.title}`);
  const read = waitForOverview(page, DEFAULT_BUDGET);
  await page.locator(`.sidebar-nav a[href="/workspaces/${workspace.id}/graph"]`).click();
  await expect(page).toHaveURL(/\/graph$/);
  return read;
}

function expectFreshAccountOverviewRequest(request: Request) {
  expect(request.postDataJSON()).toMatchObject({ lens: "combined", head_mode: "published", document_kinds: ["context", "library"],
    quick_filters: [], folders: { include: [], exclude: [], only: [] }, time: { basis: "document_updated" } });
  expect(request.postDataJSON()).not.toHaveProperty("focus_entity_id");
  expect(request.postDataJSON()).not.toHaveProperty("search");
  expect(request.postDataJSON().time).not.toHaveProperty("from");
  expect(request.postDataJSON().time).not.toHaveProperty("to");
}

function expectAccountPublishedUniverse(scene: WorkspaceGraphScene, initial: WorkspaceGraphScene) {
  // All authored documents in this fixture are published and unrestricted.
  // Same IDs across actors prove the same workspace scene, not hidden-data privacy.
  expect(scene.nodes.map((node) => node.id).sort()).toEqual(initial.nodes.map((node) => node.id).sort());
  expect(scene.edges.map((edge) => [edge.id, edge.source, edge.target, edge.relation_kind]).sort())
    .toEqual(initial.edges.map((edge) => [edge.id, edge.source, edge.target, edge.relation_kind]).sort());
}

async function expectFreshAccountMap(
  page: Page, actorId: string, workspaceId: string, scene: WorkspaceGraphScene, previous: AccountMapState,
  previousNativeViewport: Awaited<ReturnType<typeof readAccountNativeViewport>>,
) {
  await expectNavigationMapReady(page, scene, expectNavigationColumnEligibility(scene));
  await expectDefaultMapScale(page);
  await expect(page.getByRole("searchbox", { name: "Find in visible map", exact: true })).toHaveValue("");
  await expect(page.getByLabel("Lens", { exact: true })).toHaveValue("combined");
  await expect(page.getByTestId("workspace-graph-node-list").locator('button.workspace-graph-list-select[aria-pressed="true"]')).toHaveCount(0);
  await expect(page.locator(".workspace-graph-selection-clear")).toHaveCount(0);
  await expect(page.locator(".workspace-graph-details")).toHaveCount(0);
  await expect.poll(async () => Boolean(await readAccountMapState(page, actorId, workspaceId))).toBe(true);
  const state = (await readAccountMapState(page, actorId, workspaceId))!;
  expect(state).toMatchObject({ viewerId: actorId, selectedId: null, pinnedIds: [], pinnedPositions: {}, lens: "combined",
    display: { show_edges: true, show_labels: true, show_orphans: false },
    dataFilters: { search: "", document_kinds: ["context", "library"], time_basis: "document_updated", time_from: null,
      time_to: null, quick_filters: [], facet_rules: [] } });
  expect(state.viewport).not.toEqual(previous.viewport);
  if (state.viewport) expectFiniteAccountViewport(state.viewport);
  const nativeViewport = await readAccountNativeViewport(page.getByTestId("graph-canvas"));
  expect(nativeViewport).not.toBeNull();
  expect(nativeViewport!.nativeZoom > 0 && Object.values(nativeViewport!).every(Number.isFinite)).toBe(true);
  expect(nativeViewport, "Fresh actor Map must not keep the old native camera even if saved viewport is null")
    .not.toEqual(previousNativeViewport);
  const actorKeys = await page.evaluate(() => Object.keys(sessionStorage).filter((key) => key.startsWith("dtxwiki:workspace-graph:v2:")));
  expect(actorKeys.every((key) => key.startsWith(`dtxwiki:workspace-graph:v2:${encodeURIComponent(actorId)}:${encodeURIComponent(workspaceId)}:`))).toBe(true);
  expectNavigationExportIdentity(scene, await downloadVisibleMap(page));
  return { state, nativeViewport };
}

async function selectReal10kContextDocument(
  page: Page, workspaceId: string, responseReader: Awaited<ReturnType<typeof createOverviewResponseReader>>,
) {
  const initialRead = waitForOverview(page, DEFAULT_BUDGET);
  await page.goto("/graph");
  const initialResponse = await initialRead;
  expectOverviewRequest(initialResponse.request(), workspaceId, DEFAULT_BUDGET);
  expect(initialResponse.request().postDataJSON()).not.toHaveProperty("focus_entity_id");
  const initial = await responseReader.read(initialResponse);
  expectNavigationColumnEligibility(initial);
  await expectRendererReady(page);
  const view = page.locator("details.workspace-graph-view-options");
  await view.locator("summary").click();
  const scale = page.getByRole("combobox", { name: "Map node limit", exact: true });
  await expect(scale).toHaveValue("2k");
  const fullRead = waitForOverview(page, PC_BUDGET);
  await scale.selectOption("10k");
  const fullResponse = await fullRead;
  expectOverviewRequest(fullResponse.request(), workspaceId, PC_BUDGET);
  const full = await responseReader.read(fullResponse);
  expectExactAuthoredScene(full);
  await expect(scale).toHaveValue("10k");
  await view.locator("summary").click();
  await page.getByRole("button", { name: /^Unlinked \d+$/ }).click();
  await expect(page.getByTestId("graph-canvas")).toHaveAttribute("data-map-node-count", String(DOCUMENT_COUNT));
  await expect(page.getByTestId("graph-canvas")).toHaveAttribute("data-map-edge-count", String(RELATION_COUNT));
  await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", String(DOCUMENT_COUNT));
  await expect(page.getByTestId("workspace-graph-edge-list")).toHaveAttribute("data-total-count", String(RELATION_COUNT));
  await expectRendererReady(page);
  const gpu = await expectLiveWebGL(page.getByTestId("graph-canvas"));
  await expectBoundedRows(page.getByTestId("workspace-graph-node-list"), page.getByTestId("workspace-graph-edge-list"));
  // Select a linked original Context document which is ALSO a document (not
  // a cluster member) in the initial 2K response. Otherwise legitimate 2K
  // aggregation could retire the old 10K wire ID and make restoration invalid.
  const initialDocuments = new Set(initial.nodes.filter((node) => node.grain === "document").map((node) => node.id));
  const connected = new Set(full.edges.flatMap((edge) => [edge.source, edge.target]));
  const document = full.nodes.find((node): node is WorkspaceGraphDocumentNode => node.grain === "document"
    && node.entity_kind === "context" && connected.has(node.id) && initialDocuments.has(node.id));
  if (!document) throw new Error("No linked original Context document survived both initial 2K and exact 10K scenes");
  expect(document.id).toBe(`document:context:${document.entity_id}`);
  expect(document.path).toBe(`/workspaces/${workspaceId}/context/pages/${document.entity_id}`);
  await page.getByRole("searchbox", { name: "Find in visible map", exact: true }).fill(document.label);
  const row = page.getByTestId("workspace-graph-node-list").locator(`li[data-graph-node-id="${document.id}"]`);
  await row.locator("button.workspace-graph-list-select").click();
  await expect(row.locator("button.workspace-graph-list-select")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".workspace-graph-details").getByRole("heading", { name: document.label, exact: true })).toBeVisible();
  return { initial, full, document, gpu };
}

function waitForContextDetail(page: Page, workspaceId: string, entityId: string) {
  const url = `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${entityId}`;
  return page.waitForResponse((response) => response.url() === url && response.request().method() === "GET");
}

async function expectRealContextDetail(response: Response, workspaceId: string, document: WorkspaceGraphDocumentNode) {
  expect(response.url()).toBe(`${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${document.entity_id}`);
  expect(response.ok()).toBe(true);
  const detail = (await response.json()).data as components["schemas"]["ContextPageDetail"];
  expect(detail).toMatchObject({ id: document.entity_id, workspace_id: workspaceId, title: document.label });
  return detail;
}

async function expectContextRoute(page: Page, workspaceId: string, entityId: string, tab: "view" | "graph" | "history") {
  await expect(page).toHaveURL((url) => url.pathname === `/workspaces/${workspaceId}/context/pages/${entityId}`
    && (tab === "view" ? !url.searchParams.has("tab") : url.searchParams.get("tab") === tab));
}

function waitForLocalQuery(page: Page, workspaceId: string, entityId: string, mode: "current" | "action") {
  const url = `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/graph/query`;
  return page.waitForResponse((response) => {
    if (response.url() !== url || response.request().method() !== "POST") return false;
    const body = response.request().postDataJSON() as GraphQueryRequest;
    return body.time?.mode === mode && body.seeds.length === 1
      && body.seeds[0].kind === "context_page" && body.seeds[0].id === entityId;
  });
}

async function expectRealLocalQuery(response: Response, workspaceId: string, entityId: string, mode: "current" | "action") {
  expect(response.url()).toBe(`${backendBaseUrl}/api/v1/workspaces/${workspaceId}/graph/query`);
  expect(response.ok()).toBe(true);
  const body = response.request().postDataJSON() as GraphQueryRequest;
  const depth = mode === "current" ? 2 : 1;
  const budget = mode === "current" ? { nodes: 250, edges: 750 } : { nodes: 50, edges: 150 };
  const expected = { schema_version: "dtxwiki.graph-query.v1", scope: "local", preset: "all", direction: "both", depth,
    seeds: [{ kind: "context_page", id: entityId }], layers: ["knowledge", "evidence", "activity", "action"],
    time: { mode }, budget, cursor: null };
  expect(body).toMatchObject(expected);
  expect(body).not.toHaveProperty("head_mode");
  const local = (await response.json()).data as GraphQueryPayload;
  expect(local.query).toMatchObject(expected);
  expect(local.nodes.length).toBeLessThanOrEqual(budget.nodes);
  expect(local.edges.length).toBeLessThanOrEqual(budget.edges);
  expect(new Set(local.nodes.map((node) => node.id)).size).toBe(local.nodes.length);
  expect(new Set(local.edges.map((edge) => edge.id)).size).toBe(local.edges.length);
  const nodeIds = new Set(local.nodes.map((node) => node.id));
  expect(local.edges.every((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))).toBe(true);
  return local;
}

async function expectLocalRendererReady(graphPanel: Locator) {
  const canvas = graphPanel.getByTestId("graph-canvas");
  await expect(canvas.locator("..")).toHaveAttribute("data-renderer-status", "ready");
  await expect(canvas.locator("canvas.sigma-nodes")).toBeVisible();
}

function watchLocalRequests(page: Page, workspaceId: string) {
  const requests: GraphQueryRequest[] = [];
  page.on("request", (request) => {
    if (request.url() === `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/graph/query` && request.method() === "POST") {
      requests.push(request.postDataJSON() as GraphQueryRequest);
    }
  });
  return requests;
}

function watchFailedWorkspaceReads(page: Page) {
  const failed: Array<{ path: string; status: number }> = [];
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/api/v1/workspaces/") && response.status() >= 400) failed.push({ path, status: response.status() });
  });
  return failed;
}

function expectNavigationColumnEligibility(scene: WorkspaceGraphScene) {
  expectCandidateUniverse(scene);
  expect(scene.representation).toBe("aggregated");
  expect(scene.nodes.length).toBeGreaterThan(0);
  expect(scene.nodes.length).toBeLessThanOrEqual(DEFAULT_BUDGET.documents);
  expect(scene.edges.length).toBeLessThanOrEqual(DEFAULT_BUDGET.relations);
  expect(scene.counts.rendered_documents + scene.counts.rendered_clusters).toBe(scene.nodes.length);
  expect(scene.counts.rendered_relations).toBe(scene.edges.length);
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  const semanticIds = new Set(scene.nodes.map((node) => node.grain === "document" ? `${node.entity_kind}:${node.entity_id}` : node.id));
  const edgeIds = new Set<string>();
  const failures: string[] = [];
  const check = (condition: boolean, message: string) => { if (!condition && failures.length < 30) failures.push(message); };
  check(nodes.size === scene.nodes.length && semanticIds.size === scene.nodes.length, "unique original scene wire and semantic node IDs");
  for (const node of scene.nodes) {
    check(Boolean(node.id), "nonempty original scene node ID");
    if (node.grain === "document") check(node.id === `document:${node.entity_kind}:${node.entity_id}`, `canonical document wire ID ${node.id}`);
  }
  for (const edge of scene.edges) {
    check(Boolean(edge.id) && !edgeIds.has(edge.id), `unique original scene edge ${edge.id}`);
    edgeIds.add(edge.id);
    check(nodes.has(edge.source) && nodes.has(edge.target), `visible original scene endpoints ${edge.id}`);
    check(["wikilink", "citation_page", "citation_source"].includes(edge.relation_kind), `supported original scene kind ${edge.id}`);
    check(Number.isSafeInteger(edge.relation_count) && edge.relation_count > 0 && edge.relation_count <= 0xffffffff,
      `valid original scene relation count ${edge.id}`);
    const weight = edge.occurrence_weight ?? edge.relation_count;
    check(Number.isSafeInteger(weight) && weight > 0, `valid original scene occurrence weight ${edge.id}`);
    if (edge.source === edge.target) check(edge.aggregate && nodes.get(edge.source)?.grain === "cluster", `aggregate-only self-loop ${edge.id}`);
  }
  expect(failures, "Dynamic Canvas eligibility must exclude only actual aggregate self-loops, not lost or renamed API rows").toEqual([]);
  return scene.edges.filter((edge) => edge.source !== edge.target).length;
}

async function expectNavigationMapReady(page: Page, scene: WorkspaceGraphScene, canvasEdges: number) {
  await expect(page.getByRole("heading", { name: "Knowledge Map", exact: true })).toBeVisible();
  await expectRendererReady(page);
  const canvas = page.getByTestId("graph-canvas");
  await expect(canvas).toHaveAttribute("data-map-node-count", String(scene.nodes.length));
  await expect(canvas).toHaveAttribute("data-map-edge-count", String(canvasEdges));
  const nodeList = page.getByTestId("workspace-graph-node-list");
  const edgeList = page.getByTestId("workspace-graph-edge-list");
  await expect(nodeList).toHaveAttribute("data-total-count", String(scene.nodes.length));
  await expect(edgeList).toHaveAttribute("data-total-count", String(scene.edges.length));
  await expectBoundedRows(nodeList, edgeList);
  await expectLiveWebGL(canvas);
}

async function expectDefaultMapScale(page: Page) {
  const view = page.locator("details.workspace-graph-view-options");
  await view.locator("summary").click();
  await expect(page.getByRole("combobox", { name: "Map node limit", exact: true })).toHaveValue("2k");
  await view.locator("summary").click();
}

function expectNavigationExportIdentity(scene: WorkspaceGraphScene, exported: WorkspaceMapExport) {
  expect(exported.graph_revision).toBe(scene.graph_revision);
  expect(exported.representation).toBe(scene.representation);
  expect(exported.completeness).toBe(scene.completeness);
  expect(exported.nodes).toHaveLength(scene.nodes.length);
  expect(exported.edges).toHaveLength(scene.edges.length);
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  const edges = new Map(scene.edges.map((edge) => [edge.id, edge]));
  const nodeIds = new Set<string>();
  const edgeIds = new Set<string>();
  const failures: string[] = [];
  const check = (condition: boolean, message: string) => { if (!condition && failures.length < 30) failures.push(message); };
  for (const node of exported.nodes) {
    const original = nodes.get(node.id);
    if (!original) { check(false, `unknown navigation export node ${node.id}`); continue; }
    const grain = original.grain === "cluster" ? original.cluster_kind : "document";
    const entityKind = original.grain === "cluster" ? "cluster" : original.entity_kind === "context" ? "context_page" : "library_item";
    check(node.label === original.label && node.grain === grain && node.entityKind === entityKind, `original navigation export node ${node.id}`);
    check(!nodeIds.has(node.id), `unique navigation export node ${node.id}`);
    nodeIds.add(node.id);
  }
  for (const edge of exported.edges) {
    const original = edges.get(edge.id);
    check(Boolean(original && edge.source === original.source && edge.target === original.target
      && edge.edgeKind === original.relation_kind && edge.layer === original.layer && edge.count === original.relation_count
      && edge.directed === String(original.direction === "directed") && edge.aggregate === String(original.aggregate)
      && edge.crossFolder === String(original.cross_folder)), `original navigation export relationship ${edge.id}`);
    check(!edgeIds.has(edge.id), `unique navigation export relationship ${edge.id}`);
    edgeIds.add(edge.id);
  }
  check(nodeIds.size === nodes.size && edgeIds.size === edges.size, "complete navigation export node and relationship identities");
  expect(failures, "The navigation export must preserve every actual response identity and self-loop, independent of focused aggregation counts")
    .toEqual([]);
}


async function downloadVisibleMap(page: Page): Promise<WorkspaceMapExport> {
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export visible map", exact: true }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toMatch(/^dtxwiki-knowledge-map-.*\.json$/);
  expect(await download.failure()).toBeNull();
  const downloadPath = await download.path();
  if (!downloadPath) throw new Error("Visible-map export did not produce a local download");
  return JSON.parse(await readFile(downloadPath, "utf8")) as WorkspaceMapExport;
}

function waitForOverview(page: Page, budget: { documents: number; relations: number }) {
  return page.waitForResponse((response) => {
    if (!response.url().includes(OVERVIEW_PATH) || response.request().method() !== "POST") return false;
    const request = response.request().postDataJSON() as WorkspaceGraphOverviewRequest;
    return request.lens === "combined" && !request.expansion
      && request.budget.documents === budget.documents && request.budget.relations === budget.relations;
  });
}

function expectOverviewRequest(request: Request, workspaceId: string, budget: { documents: number; relations: number }) {
  expect(new URL(request.url()).pathname).toBe(`/api/v1/workspaces/${workspaceId}${OVERVIEW_PATH}`);
  const body = request.postDataJSON() as WorkspaceGraphOverviewRequest;
  expect(body).toMatchObject({ lens: "combined", head_mode: "published", budget });
  expect(body.expansion).toBeUndefined();
}

function expectCandidateUniverse(scene: WorkspaceGraphScene) {
  expect(scene.counts).toMatchObject({ candidate_documents: DOCUMENT_COUNT, candidate_relations: RELATION_COUNT });
}

function relationKindCounts(scene: WorkspaceGraphScene) {
  const counts: Record<string, number> = {};
  for (const edge of scene.edges) counts[edge.relation_kind] = (counts[edge.relation_kind] ?? 0) + 1;
  return counts;
}

function expectAggregatedSelfLoopPolicy(scene: WorkspaceGraphScene) {
  expect(scene.representation).toBe("aggregated");
  expect(scene.edges).toHaveLength(5_997);
  const selfLoops = scene.edges.filter((edge) => edge.source === edge.target);
  const nonSelfLoops = scene.edges.filter((edge) => edge.source !== edge.target);
  expect(selfLoops, "Only five aggregate self-loop strokes are omitted by the existing Cosmos column policy")
    .toHaveLength(5);
  expect(nonSelfLoops).toHaveLength(5_992);
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  const semanticKeys = new Set(scene.nodes.map((node) => node.grain === "document"
    ? `${node.entity_kind}:${node.entity_id}` : node.id));
  const edgeIds = new Set<string>();
  const failures: string[] = [];
  const check = (condition: boolean, message: string) => { if (!condition && failures.length < 30) failures.push(message); };
  check(nodes.size === scene.nodes.length && semanticKeys.size === scene.nodes.length,
    "every node has a unique wire ID and semantic identity; no column-node deduplication");
  for (const node of scene.nodes) {
    check(Boolean(node.id && (node.grain === "cluster" || node.entity_id)), `valid column node ${node.id}`);
  }
  for (const edge of scene.edges) {
    check(Boolean(edge.id) && !edgeIds.has(edge.id), `unique column relationship ID ${edge.id}`);
    edgeIds.add(edge.id);
    check(nodes.has(edge.source) && nodes.has(edge.target), `no dangling column endpoints ${edge.id}`);
    check(["wikilink", "citation_page", "citation_source"].includes(edge.relation_kind), `supported column kind ${edge.id}`);
    check(Number.isSafeInteger(edge.relation_count) && edge.relation_count > 0 && edge.relation_count <= 0xffffffff,
      `valid Uint32 column relation count ${edge.id}`);
    const weight = edge.occurrence_weight ?? edge.relation_count;
    check(Number.isSafeInteger(weight) && weight > 0, `valid column occurrence weight ${edge.id}`);
    if (edge.source === edge.target) {
      check(edge.aggregate && nodes.get(edge.source)?.grain === "cluster",
        `omitted stroke must be an aggregate cluster self-loop ${edge.id}`);
    }
  }
  // The eligibility checks above are independent of buildWorkspaceMapColumns:
  // every non-loop passes its ID/endpoint/kind/count gates, and no duplicate
  // node or edge can explain the difference. API/list/export retain all 5,997.
  expect(failures, "The exact five-edge Canvas difference must consist solely of aggregate self-loops").toEqual([]);
  return nonSelfLoops.length;
}

function expectAggregatedExportIdentity(scene: WorkspaceGraphScene, exported: WorkspaceMapExport) {
  expect(exported.graph_revision).toBe(scene.graph_revision);
  expect(exported.representation).toBe(scene.representation);
  expect(exported.completeness).toBe(scene.completeness);
  expect(exported.nodes).toHaveLength(scene.nodes.length);
  expect(exported.edges).toHaveLength(scene.edges.length);
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  const edges = new Map(scene.edges.map((edge) => [edge.id, edge]));
  const nodeIds = new Set<string>();
  const edgeIds = new Set<string>();
  const failures: string[] = [];
  const check = (condition: boolean, message: string) => { if (!condition && failures.length < 30) failures.push(message); };
  for (const node of exported.nodes) {
    const original = nodes.get(node.id);
    if (!original) {
      check(false, `unknown exported aggregate node ${node.id}`);
      continue;
    }
    const grain = original.grain === "cluster" ? original.cluster_kind : "document";
    const entityKind = original.grain === "cluster" ? "cluster"
      : original.entity_kind === "context" ? "context_page" : "library_item";
    check(node.label === original.label && node.grain === grain && node.entityKind === entityKind,
      `original exported aggregate node ${node.id}`);
    check(!nodeIds.has(node.id), `duplicate exported aggregate node ${node.id}`);
    nodeIds.add(node.id);
  }
  for (const edge of exported.edges) {
    const original = edges.get(edge.id);
    check(Boolean(original && edge.source === original.source && edge.target === original.target
      && edge.edgeKind === original.relation_kind && edge.layer === original.layer && edge.count === original.relation_count
      && edge.directed === String(original.direction === "directed") && edge.aggregate === String(original.aggregate)
      && edge.crossFolder === String(original.cross_folder)), `original exported aggregate relationship ${edge.id}`);
    check(!edgeIds.has(edge.id), `duplicate exported aggregate relationship ${edge.id}`);
    edgeIds.add(edge.id);
  }
  check(nodeIds.size === nodes.size && edgeIds.size === edges.size, "complete aggregate API/list/export identities, including all self-loops");
  expect(failures, "The aggregate visible export must retain every API relationship, including all five Canvas-omitted self-loops")
    .toEqual([]);
}

function expectExactAuthoredScene(scene: WorkspaceGraphScene) {
  expectCandidateUniverse(scene);
  expect(scene).toMatchObject({ representation: "exact", completeness: "complete", truncated: false, omission_reasons: [] });
  expect(scene.counts).toMatchObject({ rendered_documents: DOCUMENT_COUNT, rendered_clusters: 0, rendered_relations: RELATION_COUNT });
  expect(scene.nodes).toHaveLength(DOCUMENT_COUNT);
  expect(scene.edges).toHaveLength(RELATION_COUNT);
  expect(scene.nodes.filter((node) => node.grain === "document" && node.entity_kind === "context")).toHaveLength(9_000);
  expect(scene.nodes.filter((node) => node.grain === "document" && node.entity_kind === "library")).toHaveLength(1_000);
  expect(relationKindCounts(scene)).toEqual({ wikilink: 26_394, citation_source: 3_596, citation_page: 905 });
  expect(new Set(scene.edges.flatMap((edge) => [edge.source, edge.target])).size).toBe(CONNECTED_DOCUMENT_COUNT);
  const topology = scene.folder_topology;
  if (!topology) throw new Error("Exact 10K overview omitted current folder topology");
  expect(topology.schema_version).toBe("workspace-graph-folders.v1");
  expect(topology.memberships).toHaveLength(DOCUMENT_COUNT);
  expect(topology.edge_slices).toHaveLength(RELATION_COUNT);

  // One bounded assertion for all rows: per-row Playwright expects create
  // tens of thousands of traced steps and measure the runner, not the app.
  const failures: string[] = [];
  const check = (condition: boolean, message: string) => { if (!condition && failures.length < 30) failures.push(message); };
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  const edges = new Map(scene.edges.map((edge) => [edge.id, edge]));
  const folders = new Map(topology.folders.map((folder) => [folder.id, folder]));
  check(nodes.size === DOCUMENT_COUNT, "unique original document IDs");
  check(edges.size === RELATION_COUNT, "unique original relationship IDs");
  check(folders.size === topology.folders.length, "unique folder IDs");
  const memberships = new Set<string>();
  const directCounts = new Map<string, number>();
  const visibleFolders = new Set<string>();
  for (const row of topology.memberships) {
    const node = nodes.get(row.node_id);
    check(Boolean(node && node.grain === "document" && node.primary_folder_id === row.folder_id
      && node.entity_kind === row.entity_kind && row.member_count === 1), `original primary membership ${row.node_id}`);
    check(!memberships.has(row.node_id), `duplicate membership ${row.node_id}`);
    memberships.add(row.node_id);
    if (row.folder_id !== null) {
      directCounts.set(row.folder_id, (directCounts.get(row.folder_id) ?? 0) + 1);
      const branch = new Set<string>();
      let folderId: string | null = row.folder_id;
      while (folderId !== null && !visibleFolders.has(folderId)) {
        const folder = folders.get(folderId);
        if (!folder || branch.has(folderId)) {
          check(false, `missing/cyclic primary folder ancestor ${folderId}`);
          break;
        }
        branch.add(folderId);
        folderId = folder.parent_folder_id;
      }
      for (const id of branch) visibleFolders.add(id);
    }
  }
  check(memberships.size === nodes.size, "every original document has one primary membership");
  check(visibleFolders.size === folders.size, "folder set equals visible primary ancestor closure");
  for (const folder of folders.values()) {
    check(folder.direct_member_count === (directCounts.get(folder.id) ?? 0), `exact direct folder count ${folder.id}`);
  }
  for (const node of nodes.values()) {
    check(node.grain === "document" && node.id === `document:${node.entity_kind}:${node.entity_id}`, `unrenamed entity ID ${node.id}`);
  }
  const endpointTriples = new Set<string>();
  for (const edge of edges.values()) {
    const expectedLayer = edge.relation_kind === "wikilink" ? "knowledge" : "evidence";
    const triple = JSON.stringify([edge.source, edge.target, edge.relation_kind]);
    const digest = createHash("sha256").update(`${edge.source}|${edge.target}|${edge.relation_kind}|${edge.layer}`).digest("hex").slice(0, 24);
    check(edge.id === `relation:${edge.relation_kind}:${digest}`, `stable original wire relationship ID ${edge.id}`);
    check(nodes.has(edge.source) && nodes.has(edge.target) && edge.source !== edge.target, `original endpoints ${edge.id}`);
    check(!endpointTriples.has(triple), `distinct authored logical relationship ${edge.id}`);
    endpointTriples.add(triple);
    check(edge.layer === expectedLayer && edge.direction === "directed" && !edge.aggregate
      && edge.relation_count === 1 && edge.occurrence_weight === 1, `lossless relationship kind/count/layer ${edge.id}`);
  }
  const slicedEdges = new Set<string>();
  for (const row of topology.edge_slices) {
    const edge = edges.get(row.edge_id);
    const source = edge && nodes.get(edge.source);
    const target = edge && nodes.get(edge.target);
    check(Boolean(source && source.grain === "document" && source.primary_folder_id === row.source_folder_id
      && source.entity_kind === row.source_entity_kind), `correlated source slice ${row.edge_id}`);
    check(Boolean(target && target.grain === "document" && target.primary_folder_id === row.target_folder_id
      && target.entity_kind === row.target_entity_kind), `correlated target slice ${row.edge_id}`);
    check(Boolean(edge && row.relation_count === edge.relation_count && row.occurrence_weight === edge.occurrence_weight),
      `exact edge-slice totals ${row.edge_id}`);
    check(!slicedEdges.has(row.edge_id), `duplicate exact slice ${row.edge_id}`);
    slicedEdges.add(row.edge_id);
  }
  check(slicedEdges.size === edges.size, "every original relationship has its exact correlated folder slice");
  expect(failures, "Every real 10K wire ID, endpoint, kind and topology row must survive").toEqual([]);
}

function expectVisibleExportIdentity(scene: WorkspaceGraphScene, exported: GraphRenderScene) {
  expect(exported.nodes).toHaveLength(DOCUMENT_COUNT);
  expect(exported.edges).toHaveLength(RELATION_COUNT);
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  const edges = new Map(scene.edges.map((edge) => [edge.id, edge]));
  const exportedNodeIds = new Set<string>();
  const exportedEdgeIds = new Set<string>();
  const failures: string[] = [];
  const check = (condition: boolean, message: string) => { if (!condition && failures.length < 30) failures.push(message); };
  for (const node of exported.nodes) {
    const original = nodes.get(node.id);
    check(Boolean(original && original.grain === "document" && node.label === original.label && node.grain === "document"
      && node.entityKind === (original.entity_kind === "context" ? "context_page" : "library_item")), `export original node ${node.id}`);
    check(!exportedNodeIds.has(node.id), `duplicate exported node ${node.id}`);
    exportedNodeIds.add(node.id);
  }
  for (const edge of exported.edges) {
    const original = edges.get(edge.id);
    check(Boolean(original && edge.source === original.source && edge.target === original.target
      && edge.edgeKind === original.relation_kind && edge.layer === original.layer && edge.count === original.relation_count
      && edge.directed === "true" && edge.aggregate === "false" && edge.crossFolder === String(original.cross_folder)),
      `export original relationship ${edge.id}`);
    check(!exportedEdgeIds.has(edge.id), `duplicate exported relationship ${edge.id}`);
    exportedEdgeIds.add(edge.id);
  }
  check(exportedNodeIds.size === nodes.size && exportedEdgeIds.size === edges.size, "complete original visible identity coverage");
  expect(failures, "The production visible-map export must preserve every original wire identity and relationship").toEqual([]);
}

function watchOverviewRequests(page: Page) {
  const requests: WorkspaceGraphOverviewRequest[] = [];
  page.on("request", (request) => {
    if (request.url().includes(OVERVIEW_PATH) && request.method() === "POST") {
      requests.push(request.postDataJSON() as WorkspaceGraphOverviewRequest);
    }
  });
  return requests;
}

function watchBrowserDiagnostics(page: Page) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
  return { pageErrors, consoleErrors };
}

async function expectRendererReady(page: Page) {
  await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]')).toBeVisible({ timeout: 90_000 });
  const canvas = page.getByTestId("graph-canvas");
  await expect(canvas).toHaveAttribute("data-map-community-layout-status", "applied", { timeout: 90_000 });
  await expect(canvas).toHaveAttribute("data-map-community-count", /^[1-9]\d*$/);
}

async function expectLiveWebGL(canvas: Locator) {
  const gpu = await canvas.evaluate((element) => {
    const canvases = element.querySelectorAll("canvas");
    const gl = canvases[0]?.getContext("webgl2");
    return { canvasCount: canvases.length, webgl2: Boolean(gl), contextLost: gl?.isContextLost() ?? true,
      glError: gl?.getError() ?? -1, width: canvases[0]?.width ?? 0, height: canvases[0]?.height ?? 0 };
  });
  expect(gpu).toMatchObject({ canvasCount: 1, webgl2: true, contextLost: false, glError: 0 });
  expect(gpu.width).toBeGreaterThan(0);
  expect(gpu.height).toBeGreaterThan(0);
  return gpu;
}

async function expectBoundedRows(nodeList: Locator, edgeList: Locator) {
  expect(await nodeList.locator("li[data-graph-node-id]").count()).toBeLessThan(30);
  expect(await edgeList.locator("li[data-graph-edge-id]").count()).toBeLessThan(30);
}

async function afterAnimationFrame(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

async function capture(scope: Page | Locator, testInfo: TestInfo, name: string) {
  const path = testInfo.outputPath(name);
  await scope.screenshot({ animations: "disabled", path });
  await testInfo.attach(name, { contentType: "image/png", path });
}

async function attachJson(testInfo: TestInfo, name: string, value: unknown) {
  const path = testInfo.outputPath(name);
  await writeFile(path, JSON.stringify(value, null, 2), "utf8");
  await testInfo.attach(name, { contentType: "application/json", path });
}

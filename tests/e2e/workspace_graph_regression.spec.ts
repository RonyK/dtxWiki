import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { currentWorkspace, login, signUp } from "./auth";

const GRAPH_OWNER_EMAIL = "e2e-graph-owner@example.com";
const GRAPH_QUERY_PATH = "/graph/overview/query";
// build_knowledge_40: 32 Context ring links, eight shortcuts, eight Library
// citations, and one excluded self-link. Combined is the current default.
const KNOWLEDGE_CONTEXT_COUNT = 32;
const KNOWLEDGE_LIBRARY_COUNT = 8;
const KNOWLEDGE_SHORTCUT_COUNT = 8;
const KNOWLEDGE_DOCUMENT_COUNT = KNOWLEDGE_CONTEXT_COUNT + KNOWLEDGE_LIBRARY_COUNT;
const KNOWLEDGE_WIKILINK_COUNT = KNOWLEDGE_CONTEXT_COUNT + KNOWLEDGE_SHORTCUT_COUNT;
const KNOWLEDGE_COMBINED_RELATION_COUNT = KNOWLEDGE_WIKILINK_COUNT + KNOWLEDGE_LIBRARY_COUNT;

type WorkspaceGraphScene = {
  completeness: string;
  omission_reasons: string[];
  warnings: string[];
  capabilities: { compare: boolean };
  nodes: Array<{ id: string; grain: string; entity_kind: string; entity_id: string;
    label: string; reference_count: number; evidence_count: number }>;
  edges: Array<{ id: string; source: string; target: string; relation_kind: string;
    layer: string; direction: string; aggregate: boolean; relation_count: number; occurrence_weight: number }>;
  counts: {
    candidate_documents: number;
    candidate_relations: number;
    rendered_documents: number;
    rendered_relations: number;
  };
};

test.describe("Workspace Knowledge Map real-route regression", () => {
  test.describe("1440x900 desktop", () => {
    test.use({
      viewport: { width: 1440, height: 900 },
    });

    test("renders knowledge-40 and keeps Refine/Compare boundaries truthful", async ({
      page,
    }, testInfo) => {
      test.setTimeout(60_000);
      const diagnostics = watchBrowserDiagnostics(page);
      await page.emulateMedia({ reducedMotion: "reduce" });
      await login(page.request, GRAPH_OWNER_EMAIL);

      const initialScene = await openKnowledgeMap(page);
      expectKnowledgeRelations(initialScene, "combined");
      expect(initialScene.completeness).toBe("complete");
      await expect(page.getByRole("combobox", { name: "Lens" })).toHaveValue("combined");
      await expect(page.getByTestId("workspace-graph-node-list").locator(":scope > li"))
        .toHaveCount(KNOWLEDGE_DOCUMENT_COUNT);
      await expect(page.getByTestId("workspace-graph-edge-list").locator(":scope > li"))
        .toHaveCount(KNOWLEDGE_COMBINED_RELATION_COUNT);
      await expect(page.getByRole("button", { name: /^Unlinked \d+$/ })).toHaveCount(0);

      // Combined connects all eight Library sources. Preserve the original
      // orphan regression by explicitly choosing Links, not by hiding citations.
      await selectKnowledgeLinks(page);

      await expect(page.getByTestId("workspace-graph-node-list").locator(":scope > li"))
        .toHaveCount(32);
      await expect(page.getByRole("button", { name: /^Unlinked \d+$/ }))
        .toHaveAttribute("title", "8 unlinked documents hidden from this map.");
      await page.getByRole("button", { name: /^Unlinked \d+$/ }).click();

      await expect(page.getByTestId("workspace-graph-node-list").locator(":scope > li"))
        .toHaveCount(40);
      await expect(page.getByTestId("workspace-graph-edge-list").locator(":scope > li"))
        .toHaveCount(40);
      await expectRendererReady(page);
      // The worker's community attribute is set before Cosmos's scheduled
      // camera fit and paint. Capture the settled WebGL frame, not that gap.
      await afterAnimationFrame(page);
      await captureViewport(page, testInfo, "knowledge-40-desktop-overview.png");
      await captureMapCanvas(page, testInfo, "knowledge-40-desktop-webgl-canvas.png");

      await selectKnowledgeDocument00(page);
      await page.getByRole("button", { name: "Zoom in" }).click();
      await page.getByRole("button", { name: "Zoom in" }).click();
      await afterAnimationFrame(page);
      await captureViewport(page, testInfo, "knowledge-40-desktop-label-detail.png");

      await openAdvancedDataFilters(page);
      const timeTrigger = page.getByRole("button", { name: "Time", exact: true });
      // The filters sit at the end of the Map details sidebar, which scrolls on its own.
      const detailsScroller = page.locator(".inspector-panel-body");
      await detailsScroller.evaluate((element) => {
        const maximum = element.scrollHeight - element.clientHeight;
        element.scrollTop = maximum;
      });
      const scrollMetricsBeforeModal = await readScrollMetrics(detailsScroller);
      expect(scrollMetricsBeforeModal.scrollTop).toBeGreaterThan(0);
      await timeTrigger.evaluate((element) => {
        element.focus({ preventScroll: true });
        (element as HTMLButtonElement).click();
      });
      const refineDialog = page.getByRole("dialog", { name: "Refine data" });
      await expect(refineDialog).toBeVisible();
      const timeGroup = refineDialog.getByRole("group", { name: "Time" });
      await expect(timeGroup.getByRole("combobox")).toBeFocused();
      await expectModalIsolation(page, refineDialog);
      const scrollMetricsWhileModal = await readScrollMetrics(detailsScroller);
      expect(scrollMetricsWhileModal).toMatchObject({
        clientHeight: scrollMetricsBeforeModal.clientHeight,
        scrollHeight: scrollMetricsBeforeModal.scrollHeight,
        scrollTop: scrollMetricsBeforeModal.scrollTop,
      });
      await captureViewport(page, testInfo, "knowledge-40-desktop-refine-time.png");

      await page.keyboard.press("Escape");
      await expect(refineDialog).toBeHidden();
      await expect(timeTrigger).toBeFocused();
      await expectModalRestored(page);
      const scrollMetricsAfterModal = await readScrollMetrics(detailsScroller);
      expect(scrollMetricsAfterModal).toMatchObject({
        clientHeight: scrollMetricsBeforeModal.clientHeight,
        scrollHeight: scrollMetricsBeforeModal.scrollHeight,
        scrollTop: scrollMetricsBeforeModal.scrollTop,
      });

      const folderTrigger = page.getByRole("button", { name: "Folder", exact: true });
      await folderTrigger.click();
      const folderDialog = page.getByRole("dialog", { name: "Refine data" });
      const folderGroup = folderDialog.getByRole("group", { name: "Folder" });
      await expect(folderGroup.getByRole("searchbox", { name: "Find folder", exact: true })).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(folderTrigger).toBeFocused();

      const compareResponse = page.waitForResponse(isWorkspaceGraphResponse);
      await page.getByRole("combobox", { name: "Lens" }).selectOption("compare");
      const compareScene = await graphScene(compareResponse);
      expectKnowledgeRelations(compareScene, "combined");
      expect(compareScene.completeness).toBe("partial");
      expect(compareScene.omission_reasons).toEqual(["projection_partial"]);
      expect(compareScene.warnings).toEqual(["compare_projection_not_available"]);
      expect(compareScene.capabilities.compare).toBe(false);
      await expect(page.getByText("This scene is partial.", { exact: true })).toBeVisible();
      await expect(page.locator(".workspace-graph-notice").filter({
        hasText: "Full comparison projection is not available yet.",
      })).toBeVisible();
      await expect(page.getByTestId("workspace-graph-edge-list").locator(":scope > li"))
        .toHaveCount(48);
      await captureViewport(page, testInfo, "knowledge-40-desktop-compare-partial.png");

      expect(diagnostics.pageErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
      expect(diagnostics.wheelSensitivityWarnings).toEqual([]);
    });
  });

  test.describe("workspace switch off", () => {
    test.use({
      viewport: { width: 1440, height: 900 },
    });

    test("explains the off map and hides its menu without querying a scene", async ({
      page,
    }, testInfo) => {
      // A new sign-up owns a personal workspace whose Knowledge Map is off
      // until the owner switches it on in Settings.
      await signUp(
        page.request,
        `graph-off-${testInfo.project.name}-${testInfo.retry}-${Date.now()}@example.com`,
      );
      const workspace = await currentWorkspace(page.request);
      const sceneQueries: string[] = [];
      page.on("request", (request) => {
        if (request.url().includes(GRAPH_QUERY_PATH)) sceneQueries.push(request.url());
      });

      await page.goto("/graph");

      await expect(page.getByRole("heading", { name: "Knowledge Map", exact: true }))
        .toBeVisible();
      await expect(page.getByText(
        "The Knowledge Map is off for this workspace. You can turn it on in Settings.",
      )).toBeVisible();
      await expect(page.getByRole("link", { name: "Open Settings" }))
        .toHaveAttribute("href", `/workspaces/${workspace.id}/settings?section=workspace`);
      await expect(page.getByRole("link", { name: /Knowledge Map/ })).toHaveCount(0);
      expect(sceneQueries).toEqual([]);
    });
  });

  test.describe("390x844 mobile", () => {
    test.use({
      viewport: { width: 390, height: 844 },
    });

    test("opens on the map and contains the fullscreen canvas and Time sheet", async ({
      page,
    }, testInfo) => {
      test.setTimeout(60_000);
      const diagnostics = watchBrowserDiagnostics(page);
      await page.emulateMedia({ reducedMotion: "reduce" });
      await login(page.request, GRAPH_OWNER_EMAIL);

      const initialScene = await openKnowledgeMap(page);
      expectKnowledgeRelations(initialScene, "combined");
      await expect(page.getByRole("combobox", { name: "Lens" })).toHaveValue("combined");
      await expect(page.getByTestId("workspace-graph-node-list").locator(":scope > li"))
        .toHaveCount(KNOWLEDGE_DOCUMENT_COUNT);
      await expect(page.getByRole("button", { name: /^Unlinked \d+$/ })).toHaveCount(0);
      await selectKnowledgeLinks(page);
      await expect(page.getByTestId("workspace-graph-node-list").locator(":scope > li"))
        .toHaveCount(32);
      await expect(page.getByRole("button", { name: /^Unlinked \d+$/ }))
        .toHaveAttribute("title", "8 unlinked documents hidden from this map.");
      await page.getByRole("button", { name: /^Unlinked \d+$/ }).click();
      await expect(page.getByTestId("workspace-graph-node-list").locator(":scope > li"))
        .toHaveCount(40);
      // Phones open on the map; the list waits in the Map details sheet.
      await expectRendererReady(page);
      await captureViewport(page, testInfo, "knowledge-40-mobile-map-first.png");

      await page.getByRole("button", { name: "Open Map details sheet", exact: true }).click();
      await selectKnowledgeDocument00(page);
      // Closing the sheet hands focus to the selection card, which replaces its toggle.
      await page.keyboard.press("Escape");
      await expect(page.getByRole("region", { name: "Selection summary" })
        .getByRole("button", { name: "Details", exact: true })).toBeFocused();
      const openCanvas = page.getByRole("button", { name: "Open canvas fullscreen" });
      await openCanvas.click();
      const canvasDialog = page.getByRole("dialog", { name: "Knowledge Map canvas" });
      await expect(canvasDialog).toBeVisible();
      await expectRendererReady(canvasDialog);
      await afterAnimationFrame(page);
      await expectModalIsolation(page, canvasDialog);
      await captureViewport(page, testInfo, "knowledge-40-mobile-canvas.png");
      await captureMapCanvas(canvasDialog, testInfo, "knowledge-40-mobile-webgl-canvas.png");

      await canvasDialog.getByRole("button", { name: "Zoom in" }).click();
      await canvasDialog.getByRole("button", { name: "Zoom in" }).click();
      await afterAnimationFrame(page);
      await captureViewport(page, testInfo, "knowledge-40-mobile-label-detail.png");

      await page.keyboard.press("Escape");
      await expect(canvasDialog).toBeVisible();
      await expect(canvasDialog.getByRole("button", { name: "Clear selection" })).toHaveCount(0);
      await page.keyboard.press("Escape");
      await expect(canvasDialog).toBeHidden();
      await expect(openCanvas).toBeFocused();
      // The one canvas returns to the page; closing never leaves a second one.
      await expect(page.getByTestId("graph-canvas")).toHaveCount(1);
      await expectModalRestored(page);

      await openCanvas.click();
      const reopenedCanvas = page.getByRole("dialog", { name: "Knowledge Map canvas" });
      await expectRendererReady(reopenedCanvas);
      await reopenedCanvas.getByRole("button", { name: "Close canvas" }).click();
      await expect(reopenedCanvas).toBeHidden();
      await expect(openCanvas).toBeFocused();

      await openAdvancedDataFilters(page);
      const timeTrigger = page.getByRole("button", { name: "Time", exact: true });
      await timeTrigger.click();
      const refineDialog = page.getByRole("dialog", { name: "Refine data" });
      const timeGroup = refineDialog.getByRole("group", { name: "Time" });
      await expect(timeGroup.getByRole("combobox")).toBeFocused();
      await expectModalIsolation(page, refineDialog);
      await captureViewport(page, testInfo, "knowledge-40-mobile-time-sheet.png");
      await page.keyboard.press("Escape");
      await expect(timeTrigger).toBeFocused();
      await expectModalRestored(page);

      expect(diagnostics.pageErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
      expect(diagnostics.wheelSensitivityWarnings).toEqual([]);
    });
  });
});

test.describe("Knowledge Map unified Refine workflow", () => {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    test.describe(`${viewport.width}x${viewport.height}`, () => {
      test.use({ viewport });
      test("uses one validated Apply and discards cancelled data filters", async ({ page }, testInfo) => {
        test.setTimeout(60_000);
        const diagnostics = watchBrowserDiagnostics(page);
        const requests: Array<Record<string, unknown>> = [];
        page.on("request", (request) => {
          if (request.url().includes(GRAPH_QUERY_PATH) && request.method() === "POST") {
            requests.push(request.postDataJSON() as Record<string, unknown>);
          }
        });
        await page.emulateMedia({ reducedMotion: "reduce" });
        await login(page.request, GRAPH_OWNER_EMAIL);
        await openKnowledgeMap(page);
        const initialRequests = requests.length;
        const refine = page.getByRole("button", { name: "Refine", exact: true });
        await refine.click();
        const dialog = page.getByRole("dialog", { name: "Refine data" });
        const apply = dialog.getByRole("button", { name: "Apply data filters" });
        await expect(apply).toHaveCount(1);
        await expect(apply).toBeDisabled();
        await dialog.getByRole("button", { name: "Orphans", exact: true }).click();
        await dialog.getByRole("searchbox", { name: "Search published documents" }).fill("Discard this");
        await expect(apply).toBeEnabled();
        await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
        await expect(refine).toBeFocused();
        await refine.click();
        await expect(dialog.getByRole("searchbox", { name: "Search published documents" })).toHaveValue("");
        await expect(dialog.getByRole("button", { name: "Orphans", exact: true })).toHaveAttribute("aria-pressed", "false");
        await expect(apply).toBeDisabled();
        expect(requests).toHaveLength(initialRequests);
        await dialog.getByLabel("From", { exact: true }).fill("2026-08-05");
        await dialog.getByLabel("To", { exact: true }).fill("2026-08-01");
        await expect(dialog.getByRole("alert")).toHaveText("The start date must be before the end date.");
        await expect(apply).toBeDisabled();
        expect(requests).toHaveLength(initialRequests);
        await page.keyboard.press("Escape");
        await expect(refine).toBeFocused();
        await refine.click();
        await expect(dialog.getByLabel("From", { exact: true })).toHaveValue("");
        await dialog.getByRole("checkbox", { name: "Library", exact: true }).uncheck();
        await dialog.getByRole("button", { name: "Cross-folder", exact: true }).click();
        await dialog.getByRole("searchbox", { name: "Search published documents" }).fill("Knowledge");
        await captureViewport(page, testInfo, `unified-refine-${viewport.width}-staged.png`);
        const response = page.waitForResponse(isWorkspaceGraphResponse);
        await apply.click();
        await graphScene(response);
        await expect(dialog).toBeHidden();
        await expect(refine).toBeFocused();
        expect(requests).toHaveLength(initialRequests + 1);
        expect(requests.at(-1)).toMatchObject({
          search: "Knowledge", document_kinds: ["context"], quick_filters: ["cross_folder"],
        });
        await openAdvancedDataFilters(page);
        const summary = page.locator("details.workspace-graph-advanced-controls");
        await expect(summary.getByRole("checkbox")).toHaveCount(0);
        await expect(summary.getByRole("button", { name: "Apply data filters" })).toHaveCount(0);
        await summary.getByRole("button", { name: "Edit filters", exact: true }).click();
        await expect(dialog.getByRole("button", { name: "Cross-folder", exact: true })).toHaveAttribute("aria-pressed", "true");
        await expect(dialog.getByRole("checkbox", { name: "Library", exact: true })).not.toBeChecked();
        await expect(apply).toBeDisabled();
        expect(diagnostics.pageErrors).toEqual([]);
        expect(diagnostics.consoleErrors).toEqual([]);
      });
    });
  }
});

function expectKnowledgeRelations(scene: WorkspaceGraphScene, lens: "links" | "combined") {
  const includesCitations = lens === "combined";
  const relationCount = includesCitations ? KNOWLEDGE_COMBINED_RELATION_COUNT : KNOWLEDGE_WIKILINK_COUNT;
  expect(scene.counts).toMatchObject({ candidate_documents: KNOWLEDGE_DOCUMENT_COUNT,
    rendered_documents: KNOWLEDGE_DOCUMENT_COUNT, candidate_relations: relationCount, rendered_relations: relationCount });
  const contextLabel = (index: number) => `Knowledge Document ${String(index).padStart(2, "0")}`;
  const libraryLabel = (index: number) => `Knowledge Source ${String(index).padStart(2, "0")}`;
  expect(scene.nodes.map((node) => node.label).sort()).toEqual([
    ...Array.from({ length: KNOWLEDGE_CONTEXT_COUNT }, (_, index) => contextLabel(index)),
    ...Array.from({ length: KNOWLEDGE_LIBRARY_COUNT }, (_, index) => libraryLabel(index)),
  ].sort());
  for (const node of scene.nodes) {
    expect(node.grain).toBe("document");
    expect(node.entity_kind).toBe(node.label.startsWith("Knowledge Document") ? "context" : "library");
    expect(node.id).toBe(`document:${node.entity_kind}:${node.entity_id}`);
  }
  const byId = new Map(scene.nodes.map((node) => [node.id, node]));
  expect(byId.size).toBe(KNOWLEDGE_DOCUMENT_COUNT);
  expect(new Set(scene.edges.map((edge) => edge.id)).size).toBe(relationCount);
  expect(scene.nodes.reduce((sum, node) => sum + node.reference_count, 0)).toBe(KNOWLEDGE_WIKILINK_COUNT);
  expect(scene.nodes.reduce((sum, node) => sum + node.evidence_count, 0)).toBe(includesCitations ? KNOWLEDGE_LIBRARY_COUNT : 0);
  const actual = scene.edges.map((edge) => {
    expect(edge.source).not.toBe(edge.target); // the fixture's weight-99 self-link is excluded
    expect(edge.aggregate).toBe(false);
    expect(edge.relation_count).toBe(1);
    expect(edge.direction).toBe("directed");
    expect(edge.layer).toBe(edge.relation_kind === "wikilink" ? "knowledge" : "evidence");
    expect(byId.has(edge.source) && byId.has(edge.target)).toBe(true);
    return `${edge.relation_kind}:${byId.get(edge.source)!.label}->${byId.get(edge.target)!.label}:${edge.occurrence_weight}`;
  });
  const expected = [
    ...Array.from({ length: KNOWLEDGE_CONTEXT_COUNT }, (_, index) =>
      `wikilink:${contextLabel(index)}->${contextLabel((index + 1) % KNOWLEDGE_CONTEXT_COUNT)}:${index === 0 ? 9 : 1}`),
    ...Array.from({ length: KNOWLEDGE_SHORTCUT_COUNT }, (_, index) =>
      `wikilink:${contextLabel(index)}->${contextLabel(index + KNOWLEDGE_SHORTCUT_COUNT)}:1`),
    ...(includesCitations ? Array.from({ length: KNOWLEDGE_LIBRARY_COUNT }, (_, index) =>
      `citation_source:${contextLabel(index)}->${libraryLabel(index)}:1`) : []),
  ];
  expect(actual.sort()).toEqual(expected.sort());
}

async function selectKnowledgeLinks(page: Page) {
  const response = page.waitForResponse(isWorkspaceGraphResponse);
  await page.getByRole("combobox", { name: "Lens" }).selectOption("links");
  const links = await graphScene(response);
  expectKnowledgeRelations(links, "links");
  expect(links.completeness).toBe("complete");
}

async function openAdvancedDataFilters(page: Page) {
  // Phones keep the filters in the Map details sheet, closed until asked for.
  const detailsSheet = page.getByRole("button", { name: "Open Map details sheet", exact: true });
  if (await detailsSheet.isVisible()) await detailsSheet.click();
  const filters = page.locator("details.workspace-graph-advanced-controls");
  await expect(filters).not.toHaveAttribute("open", "");
  await filters.locator("summary").click();
  await expect(filters).toHaveAttribute("open", "");
}

async function openKnowledgeMap(page: Page) {
  const response = page.waitForResponse(isWorkspaceGraphResponse);
  await page.goto("/graph");
  await expect(page.getByRole("heading", { name: "Knowledge Map", exact: true }))
    .toBeVisible();
  await expect(page.getByText(
    "Verify your email to enable self-service account recovery.",
    { exact: true },
  )).toHaveCount(0);
  return graphScene(response);
}

function isWorkspaceGraphResponse(response: { url(): string; request(): { method(): string } }) {
  return response.url().includes(GRAPH_QUERY_PATH)
    && response.request().method() === "POST";
}

async function graphScene(responsePromise: ReturnType<Page["waitForResponse"]>) {
  const response = await responsePromise;
  expect(response.ok(), await response.text()).toBeTruthy();
  const body = await response.json() as { data: WorkspaceGraphScene };
  return body.data;
}

async function expectRendererReady(scope: Page | ReturnType<Page["getByRole"]>) {
  await expect(scope.locator('.graph-canvas-frame[data-renderer-status="ready"]'))
    .toBeVisible({ timeout: 20_000 });
  await expect(scope.getByTestId("graph-canvas"))
    .toHaveAttribute("data-map-node-count", "40");
  await expect(scope.getByTestId("graph-canvas"))
    .toHaveAttribute("data-map-edge-count", "40");
  await expect(scope.getByTestId("graph-canvas"))
    .toHaveAttribute("data-map-community-count", /^[1-9]\d*$/);
  await expect(scope.getByTestId("graph-canvas").locator("canvas").first())
    .toBeVisible();
  const gpu = await scope.getByTestId("graph-canvas").evaluate((element) => {
    const canvases = element.querySelectorAll("canvas");
    const canvas = canvases[0];
    const gl = canvas?.getContext("webgl2");
    return {
      canvasCount: canvases.length,
      webgl2: Boolean(gl),
      contextLost: gl?.isContextLost() ?? true,
      glError: gl?.getError() ?? -1,
      width: canvas?.width ?? 0,
      height: canvas?.height ?? 0,
      labelCount: element.querySelectorAll("[data-workspace-map-labels] span").length,
    };
  });
  // The retired Cytoscape canvas cannot satisfy this WebGL2 contract.
  expect(gpu.canvasCount).toBe(1);
  expect(gpu.webgl2).toBe(true);
  expect(gpu.contextLost).toBe(false);
  expect(gpu.glError).toBe(0);
  expect(gpu.width).toBeGreaterThan(0);
  expect(gpu.height).toBeGreaterThan(0);
  expect(gpu.labelCount).toBeLessThanOrEqual(40);
}

async function selectKnowledgeDocument00(page: Page) {
  const nodeButton = page.getByTestId("workspace-graph-node-list")
    .getByRole("button")
    .filter({ hasText: "Knowledge Document 00" })
    .first();
  await nodeButton.click();
  await expect(page.locator(".workspace-graph-details").getByRole("heading", {
    name: "Knowledge Document 00",
    exact: true,
  })).toBeVisible();
}

async function expectModalIsolation(
  page: Page,
  dialog: ReturnType<Page["getByRole"]>,
) {
  const appShell = page.locator(".app-shell");
  await expect.poll(() => appShell.evaluate((element) => ({
    ariaHidden: element.getAttribute("aria-hidden"),
    inertAttribute: element.hasAttribute("inert"),
    inertProperty: "inert" in element && (element as HTMLElement & { inert: boolean }).inert,
  }))).toEqual({ ariaHidden: "true", inertAttribute: true, inertProperty: true });

  const graphScroller = page.locator(".feature-main.workspace-graph");
  await expect(graphScroller).toHaveAttribute("data-modal-scroll-lock", "true");
  await expect.poll(() => graphScroller.evaluate((element) =>
    getComputedStyle(element).overflowY,
  )).toBe("hidden");

  await page.evaluate(() => {
    const backgroundButton = document.querySelector<HTMLElement>(
      ".app-shell button:not([disabled])",
    );
    backgroundButton?.focus();
  });
  await expect.poll(() => dialog.evaluate((element) =>
    element.contains(document.activeElement),
  )).toBe(true);

  for (let index = 0; index < 12; index += 1) {
    await page.keyboard.press("Tab");
    await expect.poll(() => dialog.evaluate((element) =>
      element.contains(document.activeElement),
    )).toBe(true);
  }
}

async function expectModalRestored(page: Page) {
  const appShell = page.locator(".app-shell");
  await expect.poll(() => appShell.evaluate((element) => ({
    ariaHidden: element.getAttribute("aria-hidden"),
    inertAttribute: element.hasAttribute("inert"),
    inertProperty: "inert" in element && (element as HTMLElement & { inert: boolean }).inert,
  }))).toEqual({ ariaHidden: null, inertAttribute: false, inertProperty: false });
  // The map region never scrolls; the lock must hand overflow back to its stylesheet.
  await expect.poll(() => page.locator(".feature-main.workspace-graph").evaluate((element) =>
    (element as HTMLElement).style.overflow,
  )).toBe("");
  await expect(page.locator(".feature-main.workspace-graph"))
    .not.toHaveAttribute("data-modal-scroll-lock", "true");
}

function watchBrowserDiagnostics(page: Page) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  const wheelSensitivityWarnings: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
    if (
      message.type() === "warning"
      && /wheel\s*sensitivity|wheelSensitivity/i.test(message.text())
    ) {
      wheelSensitivityWarnings.push(message.text());
    }
  });
  return { pageErrors, consoleErrors, wheelSensitivityWarnings };
}

async function captureMapCanvas(
  scope: Page | ReturnType<Page["getByRole"]>,
  testInfo: TestInfo,
  name: string,
) {
  const path = testInfo.outputPath(name);
  await scope.getByTestId("graph-canvas").screenshot({ path, animations: "disabled" });
  await testInfo.attach(name, { contentType: "image/png", path });
}

async function captureViewport(page: Page, testInfo: TestInfo, name: string) {
  const path = testInfo.outputPath(name);
  await page.screenshot({
    animations: "disabled",
    caret: "hide",
    path,
  });
  await testInfo.attach(name, { contentType: "image/png", path });
}

async function afterAnimationFrame(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

async function readScrollMetrics(locator: ReturnType<Page["locator"]>) {
  return locator.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      clientHeight: element.clientHeight,
      clientWidth: element.clientWidth,
      overflowY: style.overflowY,
      scrollHeight: element.scrollHeight,
      scrollTop: element.scrollTop,
      scrollbarGutter: style.scrollbarGutter,
    };
  });
}

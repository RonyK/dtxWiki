import { expect, test, type Locator, type Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import type { WorkspaceGraphDocumentNode, WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";
import { login } from "./auth";

const OVERVIEW_PATH = "/graph/overview/query";
const CONNECTED_DOCUMENTS = 1_928;
const RELATIONS = 5_929;

// The existing run-e2e.mjs fresh-SQLite wrapper seeds this account/corpus.
// No mocked response, solver hook, forced coordinate, pin or permission change.
test.describe("real API 2K Knowledge Map reading", () => {
  test.skip(process.env.DTXWIKI_E2E_GRAPH_2K !== "1", "Set DTXWIKI_E2E_GRAPH_2K=1 through the existing E2E wrapper");
  test.use({ viewport: { width: 1440, height: 900 } });

  test("reads an original Context document and direct neighbors, then returns to overview and zooms", async ({ page, browserName }, testInfo) => {
    test.skip(browserName !== "chromium", "Native WebGL reading acceptance targets Chromium");
    test.setTimeout(90_000);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await login(page.request, "e2e-graph-2k-owner@example.com");
    const pageErrors: string[] = [], consoleErrors: string[] = [], failedReads: string[] = [], graphQueries: string[] = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
    page.on("response", response => {
      if (response.url().includes("/api/v1/workspaces/") && response.status() >= 400) failedReads.push(`${response.status()} ${new URL(response.url()).pathname}`);
    });
    page.on("request", request => {
      if (request.method() === "POST" && /\/graph\/(overview\/)?query$/.test(new URL(request.url()).pathname)) graphQueries.push(new URL(request.url()).pathname);
    });
    const responsePromise = page.waitForResponse(response => response.url().includes(OVERVIEW_PATH) && response.request().method() === "POST");
    await page.goto("/graph");
    const response = await responsePromise;
    expect(response.ok()).toBe(true);
    expect(response.request().postDataJSON()).toMatchObject({ lens: "combined", budget: { documents: 2_000, relations: 6_000 } });
    const scene = (await response.json()).data as WorkspaceGraphScene;
    expect(scene).toMatchObject({ representation: "exact", completeness: "complete", truncated: false,
      counts: { candidate_documents: 2_000, rendered_documents: 2_000, candidate_relations: RELATIONS, rendered_relations: RELATIONS } });
    expect(scene.nodes).toHaveLength(2_000);
    expect(scene.edges).toHaveLength(RELATIONS);
    const connected = new Set(scene.edges.flatMap(edge => [edge.source, edge.target]));
    expect(connected.size).toBe(CONNECTED_DOCUMENTS);
    const canvas = page.getByTestId("graph-canvas");
    await expect(canvas.locator("..")).toHaveAttribute("data-renderer-status", "ready", { timeout: 60_000 });
    await expect(canvas).toHaveAttribute("data-map-physics", "force-atlas2-worker");
    // Initial worker seed is scene preparation, not a user action under test.
    await expect(canvas).toHaveAttribute("data-map-community-layout-status", "applied");
    await expect(canvas.locator("canvas")).toHaveCount(1);
    await canvas.locator("canvas").scrollIntoViewIfNeeded();
    await page.mouse.move(0, 0);
    const initial = await readFrame(page, canvas);
    expectCounts(initial);
    expect(initial.candidates).toHaveLength(40);
    const document = initial.candidates.filter(label => label.painted).map(label =>
      scene.nodes.find(node => node.id === label.id)).find((node): node is WorkspaceGraphDocumentNode =>
      node?.grain === "document" && node.entity_kind === "context" && connected.has(node.id)
      && neighborsOf(scene, node.id).length > 0 && neighborsOf(scene, node.id).length < 40);
    expect(document, "Preparation: a painted linked API Context document must be readable without forced geometry").toBeTruthy();
    if (!document) throw new Error("No painted linked Context document in the native overview");
    const native = canvas.locator("canvas");
    const identity = await native.elementHandle();
    if (!identity) throw new Error("Native canvas identity missing");
    const initialState = await readState(page);
    expect(initialState.selectedId).toBeNull();
    const search = page.getByRole("searchbox", { name: "Find in visible map" });
    const details = page.locator(".workspace-graph-details");
    await search.fill(document.label);
    const searched = await readFrame(page, canvas);
    expectCameraUnchanged(initial, searched);
    expectPointUnchanged(initial, searched, document.id);
    expect(searched.candidates.map(label => label.id)).toEqual([document.id]);
    const row = page.getByTestId("workspace-graph-node-list").locator(`li[data-graph-node-id="${document.id}"]`);
    await row.locator("button.workspace-graph-list-select").click();
    await expect(details.getByRole("heading", { name: document.label, exact: true })).toBeVisible();
    await expect.poll(async () => (await readState(page)).selectedId).toBe(document.id);
    const selectedSearch = await readFrame(page, canvas);
    // Existing list selection explicitly Centers; camera may change, data coordinates may not.
    expectPointUnchanged(searched, selectedSearch, document.id);
    await search.fill("");
    await expect(search).toHaveValue("");
    const selected = await readFrame(page, canvas);
    expectCameraUnchanged(selectedSearch, selected);
    expectPointUnchanged(selectedSearch, selected, document.id);
    expectCounts(selected);
    const neighbors = neighborsOf(scene, document.id);
    const selectedCandidates = selected.candidates.map(label => label.id);
    expect(selectedCandidates).toHaveLength(40);
    expect(selectedCandidates[0]).toBe(document.id);
    expect(new Set(selectedCandidates.slice(0, neighbors.length + 1))).toEqual(new Set([document.id, ...neighbors]));
    const paintedNeighbors = selected.candidates.filter(label => neighbors.includes(label.id) && label.painted);
    expect(paintedNeighbors.length, "A selected document must have at least one painted direct-neighbor title").toBeGreaterThan(0);
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", String(CONNECTED_DOCUMENTS));
    const selectedState = await readState(page);
    expect(selectedState.pinnedIds).toEqual(initialState.pinnedIds);
    expect(selectedState.pinnedPositions).toEqual(initialState.pinnedPositions);
    await testInfo.attach("selected-direct-neighbors.png", { body: await canvas.screenshot(), contentType: "image/png" });

    await page.getByRole("button", { name: "Fit graph", exact: true }).click();
    const fitted = await readFrame(page, canvas);
    expectPointUnchanged(selected, fitted, document.id);
    expect((await readState(page)).selectedId).toBe(document.id);
    // Native Fit has 8% padding: use its actual empty corner, not a clear-selection control.
    await native.click({ position: { x: 2, y: 2 } });
    await page.mouse.move(0, 0);
    await expect.poll(async () => (await readState(page)).selectedId).toBeNull();
    // The shipped screen unmounts Details when no document/edge is selected.
    await expect(details).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Clear selection", exact: true })).toHaveCount(0);
    const overview = await readFrame(page, canvas);
    expectCameraUnchanged(fitted, overview);
    expectPointUnchanged(fitted, overview, document.id);
    expect(overview.candidates.map(label => label.id)).toEqual(initial.candidates.map(label => label.id));
    expectCounts(overview);
    const beforeWheelState = await readState(page);
    expect(beforeWheelState.pinnedIds).toEqual(initialState.pinnedIds);
    expect(beforeWheelState.pinnedPositions).toEqual(initialState.pinnedPositions);
    const bounds = await native.boundingBox();
    if (!bounds) throw new Error("Native canvas has no pointer bounds");
    await page.mouse.move(bounds.x + 2, bounds.y + 2);
    await page.mouse.wheel(0, -120);
    await expect.poll(async () => (await readFrame(page, canvas)).camera.k).toBeGreaterThan(overview.camera.k);
    const zoomed = await readFrame(page, canvas);
    expectPointUnchanged(overview, zoomed, document.id);
    expectCounts(zoomed);
    await expect(native).toHaveCount(1);
    expect(await native.evaluate((element, original) => element === original, identity)).toBe(true);
    await identity.dispose();
    const afterWheelState = await readState(page);
    expect(afterWheelState.selectedId).toBeNull();
    expect(afterWheelState.pinnedIds).toEqual(beforeWheelState.pinnedIds);
    expect(afterWheelState.pinnedPositions).toEqual(beforeWheelState.pinnedPositions);
    expect(graphQueries).toEqual([new URL(response.url()).pathname]);
    expect(failedReads).toEqual([]);
    expect(pageErrors).toEqual([]);
    expect(consoleErrors).toEqual([]);
    await testInfo.attach("reading-oracles.json", { contentType: "application/json", body: JSON.stringify({
      scope: "Real API native reading only; not density, force correctness or UI2K completion acceptance",
      document: { id: document.id, label: document.label }, neighbors, paintedNeighborIds: paintedNeighbors.map(label => label.id),
      initial, searched, selectedSearch, selected, fitted, overview, zoomed, graphQueries,
    }, null, 2) });
  });

  test("runs the official CPU Worker on the original API corpus and preserves Pause/Resume", async ({ page, browserName }, testInfo) => {
    test.skip(browserName !== "chromium", "Native WebGL reading acceptance targets Chromium");
    test.setTimeout(90_000);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await login(page.request, "e2e-graph-2k-owner@example.com");
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    const canvas = page.getByTestId("graph-canvas");
    const contractPath = testInfo.outputPath("cpu-worker-real-api-contract.json");
    const diagnostics: Record<string, unknown> = {
      scope: "Original API 2K structural Pause/Resume movement and corrected contact/status preservation; not convergence, density or real-device performance acceptance",
      errors,
    };
    const saveCheckpoint = async (name: string, details: unknown) => {
      diagnostics[name] = details;
      await writeFile(contractPath, JSON.stringify(diagnostics, null, 2));
    };
    try {
      const responsePromise = page.waitForResponse(response => response.url().includes(OVERVIEW_PATH) && response.request().method() === "POST");
      await page.goto("/graph");
      const response = await responsePromise;
      await saveCheckpoint("response", { status: response.status() });
      expect(response.ok()).toBe(true);
      const scene = (await response.json()).data as WorkspaceGraphScene;
      const connected = new Set(scene.edges.flatMap(edge => [edge.source, edge.target]));
      await saveCheckpoint("api", { status: response.status(), nodes: scene.nodes.length,
        edges: scene.edges.length, connected: connected.size });
      expect(scene.nodes).toHaveLength(2_000);
      expect(scene.edges).toHaveLength(RELATIONS);
      expect(connected.size).toBe(CONNECTED_DOCUMENTS);
      await expect(canvas.locator("..")).toHaveAttribute("data-renderer-status", "ready", { timeout: 60_000 });
      await expect(canvas).toHaveAttribute("data-map-physics", "force-atlas2-worker");
      const initialState = await readState(page);
      // Preserve the original moving-structure oracle. A settled contact frame
      // is not required to move 0.25px merely because its Worker was resumed.
      await expect.poll(async () => {
        const status = await readLayoutDiagnostic(canvas);
        diagnostics.waitingForStructure = status;
        return status.ticks ?? 0;
      }, { timeout: 20_000 }).toBeGreaterThanOrEqual(10);
      await expect(canvas).toHaveAttribute("data-map-layout-running", "true");
      await page.getByRole("button", { name: "Pause layout", exact: true }).click();
      await expect(canvas).toHaveAttribute("data-map-layout-running", "false");
      await expect(page.getByTestId("workspace-map-layout-status")).toHaveAttribute("data-layout-state", "paused");
      const structuralPausedStatus = await readLayoutDiagnostic(canvas);
      await saveCheckpoint("structuralPausedStatus", structuralPausedStatus);
      expect(structuralPausedStatus.phase).toBe("structure");
      const ticks = Number(await canvas.getAttribute("data-map-layout-ticks"));
      const paused = await readFrame(page, canvas);
      const held = await readFrame(page, canvas);
      const pausedState = await readState(page);
      await saveCheckpoint("structuralPause", { ticks, paused, held, initialState, pausedState });
      expectCounts(paused);
      expect(paused.candidates).toHaveLength(40);
      expectCameraUnchanged(paused, held);
      for (const candidate of paused.candidates) expectPointUnchanged(paused, held, candidate.id);
      expect(Number(await canvas.getAttribute("data-map-layout-ticks"))).toBe(ticks);
      expect(pausedState.pinnedIds).toEqual(initialState.pinnedIds);
      expect(pausedState.pinnedPositions).toEqual(initialState.pinnedPositions);
      await page.getByRole("button", { name: "Resume layout", exact: true }).click();
      await expect(canvas).toHaveAttribute("data-map-layout-running", "true");
      await expect.poll(async () => Number(await canvas.getAttribute("data-map-layout-ticks"))).toBeGreaterThan(ticks + 2);
      await page.getByRole("button", { name: "Pause layout", exact: true }).click();
      const resumedStatus = await readLayoutDiagnostic(canvas);
      const resumed = await readFrame(page, canvas);
      const resumedState = await readState(page);
      await saveCheckpoint("structuralResume", { resumedStatus, resumed, resumedState });
      expect(resumedStatus.phase).toBe("structure");
      expectCounts(resumed);
      expectCameraUnchanged(paused, resumed);
      expect(resumed.candidates.some(candidate => {
        const previous = paused.candidates.find(item => item.id === candidate.id);
        return previous && Math.hypot(candidate.x - previous.x, candidate.y - previous.y) > 0.25;
      }), "The real Worker must publish moved positions after structural Resume").toBe(true);
      expect(resumedState.pinnedIds).toEqual(pausedState.pinnedIds);
      expect(resumedState.pinnedPositions).toEqual(pausedState.pinnedPositions);

      await page.getByRole("button", { name: "Resume layout", exact: true }).click();
      // Handoff still contains structural rows. Require the first corrected
      // contact publication, without imposing another movement/settling SLA.
      await expect.poll(async () => {
        const status = await readLayoutDiagnostic(canvas);
        diagnostics.waitingForContact = status;
        return status.phase === "contact" && status.structureIterations !== null
          && status.structureIterations > 0 && status.iterations !== null
          && status.iterations > status.structureIterations && status.contactReferenceZoom !== null
          && status.contactReferenceZoom > 0;
      }, { timeout: 45_000 }).toBe(true);
      await expect(page.getByTestId("workspace-map-layout-status")).toHaveCount(0);
      await expect(canvas).toHaveAttribute("data-map-layout-running", "true");
      const contactStatus = await readLayoutDiagnostic(canvas);
      await saveCheckpoint("correctedContactStatus", contactStatus);
      expect(contactStatus.hintState).toBeNull();
      await page.getByRole("button", { name: "Pause layout", exact: true }).click();
      await expect(canvas).toHaveAttribute("data-map-layout-running", "false");
      await expect(page.getByTestId("workspace-map-layout-status")).toHaveAttribute("data-layout-state", "paused");
      const contactPausedStatus = await readLayoutDiagnostic(canvas);
      const contactPaused = await readFrame(page, canvas);
      const contactHeld = await readFrame(page, canvas);
      const contactHeldStatus = await readLayoutDiagnostic(canvas);
      const contactState = await readState(page);
      await saveCheckpoint("contactPause", { contactPausedStatus, contactHeldStatus,
        contactPaused, contactHeld, contactState });
      expectCounts(contactPaused);
      expectCounts(contactHeld);
      expectCameraUnchanged(contactPaused, contactHeld);
      for (const candidate of contactPaused.candidates) expectPointUnchanged(contactPaused, contactHeld, candidate.id);
      expect(contactHeldStatus).toEqual(contactPausedStatus);
      expect(contactPausedStatus.structureIterations).toBe(contactStatus.structureIterations);
      expect(contactPausedStatus.contactReferenceZoom).toBe(contactStatus.contactReferenceZoom);
      expect(contactState.pinnedIds).toEqual(resumedState.pinnedIds);
      expect(contactState.pinnedPositions).toEqual(resumedState.pinnedPositions);
      const screenshotPath = testInfo.outputPath("original-api-cpu-layout-paused.png");
      await canvas.screenshot({ path: screenshotPath });
      await testInfo.attach("original-api-cpu-layout-paused.png", { path: screenshotPath, contentType: "image/png" });
      expect(errors).toEqual([]);
    } finally {
      diagnostics.finalStatus = await readLayoutDiagnostic(canvas).catch(error => ({ unavailable: String(error) }));
      await writeFile(contractPath, JSON.stringify(diagnostics, null, 2));
      await testInfo.attach("cpu-worker-real-api-contract.json", { contentType: "application/json", path: contractPath });
    }
  });
});

function neighborsOf(scene: WorkspaceGraphScene, id: string) {
  return [...new Set(scene.edges.flatMap(edge => edge.source === id ? [edge.target] : edge.target === id ? [edge.source] : []))].filter(neighbor => neighbor !== id);
}

async function readLayoutDiagnostic(canvas: Locator) {
  return canvas.evaluate(element => {
    const dataset = (element as HTMLElement).dataset;
    const counter = (value: string | undefined) => {
      if (value === undefined) return null;
      const result = Number(value);
      return Number.isSafeInteger(result) && result >= 0 ? result : null;
    };
    const contactReferenceZoom = Number(dataset.mapContactReferenceZoom);
    const hint = element.parentElement?.querySelector<HTMLElement>("[data-testid='workspace-map-layout-status']");
    return { phase: dataset.mapLayoutPhase ?? null, iterations: counter(dataset.mapLayoutIterations),
      structureIterations: counter(dataset.mapLayoutStructureIterations), ticks: counter(dataset.mapLayoutTicks),
      running: dataset.mapLayoutRunning ?? null, transition: dataset.mapLayoutTransition ?? null,
      contactReferenceZoom: Number.isFinite(contactReferenceZoom) && contactReferenceZoom > 0 ? contactReferenceZoom : null,
      hintState: hint?.dataset.layoutState ?? null, hintText: hint?.textContent?.trim() ?? null };
  });
}

async function readFrame(page: Page, canvas: Locator) {
  // Pair the actual label paint with its camera; a fixed sleep can combine an
  // earlier label projection with a newer zero-duration D3 Center transition.
  return canvas.evaluate(async element => {
    const layer = element.querySelector("[data-workspace-map-labels]");
    if (!layer) throw new Error("Native label layer is unavailable");
    await new Promise<void>((resolve, reject) => {
      const observer = new MutationObserver(() => {
        observer.disconnect();
        clearTimeout(timeout);
        resolve();
      });
      const timeout = setTimeout(() => {
        observer.disconnect();
        reject(new Error("No fresh native GPU-backed label paint"));
      }, 2_000);
      observer.observe(layer, { subtree: true, attributes: true, attributeFilter: ["data-point-x", "data-point-y"] });
    });
    const native = element.querySelector<HTMLCanvasElement>("canvas");
    const camera = (native as (HTMLCanvasElement & { __zoom?: { k: number; x: number; y: number } }) | null)?.__zoom;
    if (!camera || !Number.isFinite(camera.k) || camera.k <= 0) throw new Error("Native D3 camera is unavailable");
    const bounds = element.getBoundingClientRect();
    const candidates = Array.from(element.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span[data-node-id]"))
      .filter(label => Boolean(label.dataset.nodeId)).map(label => {
        const x = Number(label.dataset.pointX), y = Number(label.dataset.pointY), rect = label.getBoundingClientRect();
        return { id: label.dataset.nodeId!, x, y, painted: getComputedStyle(label).display !== "none"
          && rect.width > 0 && rect.height > 0 && rect.left >= bounds.left && rect.right <= bounds.right
          && rect.top >= bounds.top && rect.bottom <= bounds.bottom && rect.left >= 0 && rect.top >= 0
          && rect.right <= innerWidth && rect.bottom <= innerHeight };
      });
    return { camera: { k: camera.k, x: camera.x, y: camera.y }, candidates, width: bounds.width, height: bounds.height,
      nodes: Number((element as HTMLElement).dataset.mapNodeCount), edges: Number((element as HTMLElement).dataset.mapEdgeCount) };
  });
}

type Frame = Awaited<ReturnType<typeof readFrame>>;

function expectCounts(frame: Frame) {
  expect(frame.nodes).toBe(CONNECTED_DOCUMENTS);
  expect(frame.edges).toBe(RELATIONS);
}

function expectCameraUnchanged(before: Frame, after: Frame) {
  expect(after.camera.k).toBeCloseTo(before.camera.k, 5);
  expect(after.camera.x).toBeCloseTo(before.camera.x, 2);
  expect(after.camera.y).toBeCloseTo(before.camera.y, 2);
}

function expectPointUnchanged(before: Frame, after: Frame, id: string) {
  const oldPoint = before.candidates.find(label => label.id === id), newPoint = after.candidates.find(label => label.id === id);
  expect(oldPoint, `Tracked point before action: ${id}`).toBeTruthy();
  expect(newPoint, `Tracked point after action: ${id}`).toBeTruthy();
  if (!oldPoint || !newPoint) throw new Error(`Tracked point disappeared: ${id}`);
  expect([oldPoint.x, oldPoint.y, newPoint.x, newPoint.y].every(Number.isFinite)).toBe(true);
  expect(after.width).toBe(before.width);
  expect(after.height).toBe(before.height);
  // Remove the actual native camera transform. Cosmos's base screen/world offset
  // is constant at this unchanged canvas size; this compares the same data point.
  const dx = (newPoint.x - after.camera.x) / after.camera.k - (oldPoint.x - before.camera.x) / before.camera.k;
  const dy = (newPoint.y - after.camera.y) / after.camera.k - (oldPoint.y - before.camera.y) / before.camera.k;
  expect(Math.hypot(dx, dy) * Math.min(before.camera.k, after.camera.k), "Tracked data point moved beyond the existing 0.25px closeout budget").toBeLessThan(0.25);
}

async function readState(page: Page) {
  return page.evaluate(() => {
    // Only existing graph-owned state; never inspect or mutate auth storage.
    const keys = Object.keys(sessionStorage).filter(key => key.startsWith("dtxwiki:workspace-graph:v2:") && key.endsWith(":latest"));
    if (keys.length !== 1) throw new Error("Expected one isolated actor/workspace graph state");
    const state = JSON.parse(sessionStorage.getItem(keys[0])!).state;
    return { selectedId: state.selectedId as string | null, pinnedIds: [...state.pinnedIds].sort() as string[],
      pinnedPositions: state.pinnedPositions as Record<string, { x: number; y: number }> };
  });
}

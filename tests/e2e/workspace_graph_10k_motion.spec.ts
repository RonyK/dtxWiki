import { expect, test, type Page } from "@playwright/test";

import type { WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";
import { currentWorkspace, login } from "./auth";
import { createOverviewResponseReader, type OverviewResponseReader } from "./workspace_graph_response_reader";
import { movedPointCount, readWorkspacePointFrameOnPage, visibleOrdinaryTitleCountOnPage } from "./workspace_graph_point_frame";
import { captureNativeLongCalls, expectNativeCounts, installNativeDrawMeter, readNativeDraw, readNativeDrawDiagnostics } from "./workspace_graph_native_draw";
import { sameWorkspaceRemountCamera } from "./workspace_graph_camera";
import { createDenseCpuProfile } from "./workspace_graph_cpu_profile";

const PC_BUDGET = { documents: 10_000, relations: 100_000 };
const MOBILE_BUDGET = { documents: 2_000, relations: 6_000 };
const OVERVIEW = "/graph/overview/query";
const responseReaders = new WeakMap<Page, OverviewResponseReader>();
const nativeCostCollectors = new WeakMap<Page, ReturnType<typeof captureNativeLongCalls>>();
const cpuProfiles = new WeakMap<Page, ReturnType<typeof createDenseCpuProfile>>();

test.use({ viewport: { width: 1440, height: 900 }, trace: "off" });
test.afterEach(async ({ page }, info) => {
  const collector = nativeCostCollectors.get(page);
  try {
    const cpuProfile = cpuProfiles.get(page);
    if (cpuProfile) {
      const evidence = await cpuProfile.stop();
      try {
        await info.attach("pc10k-dense-cpu-profile-metadata.json", { body: JSON.stringify(evidence.metadata, null, 2),
          contentType: "application/json" });
        if (evidence.profileBody) await info.attach("pc10k-dense.cpuprofile", { body: evidence.profileBody,
          contentType: "application/json" });
      } catch (error) {
        if (info.status === info.expectedStatus) throw error;
        console.warn("Could not attach CPU profile diagnostic; preserving original test failure");
      }
    }
    if (collector) {
      try {
        await info.attach("pc10k-native-long-calls.json", { body: JSON.stringify({
          scope: "Opt-in existing native GL invocation JS wall time and browser long tasks over 100ms; phase0 marks entry into CPU-array reads larger than 64 pixels, phase1 is a slow completion. Not GPU execution time. No extra GL reads. Up to 64 numeric-only records retained in Node before application navigation.",
          records: collector.records,
        }, null, 2), contentType: "application/json" });
      } catch (error) {
        if (info.status === info.expectedStatus) throw error;
        console.warn("Could not attach native long-call diagnostic; preserving original test failure");
      }
    }
  } finally {
    cpuProfiles.delete(page);
    collector?.dispose();
    nativeCostCollectors.delete(page);
    await responseReaders.get(page)?.dispose();
    responseReaders.delete(page);
  }
});

// This lane proves real API/native motion and maximum-capacity stability.
// Hardware frame/latency acceptance belongs to the separately recorded PC bench.
test.describe("PC10K normal-motion and dense real-API stability", () => {
  test("native 10K motion pauses and resumes without reclaiming the user's camera", async ({ page, browserName }, info) => {
    test.skip(process.env.DTXWIKI_E2E_GRAPH_10K !== "1" || browserName !== "chromium",
      "The isolated wrapper enables the PC10K corpus for Chromium");
    test.setTimeout(180_000);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const errors = watchErrors(page);
    await installNativeDrawMeter(page);
    const scene = await openPc10k(page, "e2e-graph-10k-owner@example.com", 30_895);
    const canvas = page.getByTestId("graph-canvas");
    await expectNativeCounts(page, 10_000, 30_895);

    // A real user wheel consumes automatic-camera ownership. Native label
    // point coordinates then prove motion rather than counting RAF callbacks.
    const bounds = await canvas.locator("canvas").boundingBox();
    if (!bounds) throw new Error("No native PC10K canvas");
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.wheel(0, -80);
    await page.mouse.move(0, 0);
    await expect.poll(() => visibleOrdinaryTitleCountOnPage(page),
      { message: "The native map must retain at least one actually visible ordinary document title" }).toBeGreaterThanOrEqual(1);
    await expect.poll(async () => (await readPointFrame(page)).points.length).toBeGreaterThanOrEqual(3);
    await expect(page.getByRole("button", { name: "Pause layout", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "Pause layout", exact: true }).click();
    const paused = await stablePointFrame(page);
    const resume = page.getByRole("button", { name: "Resume layout", exact: true });
    await expect(resume).toBeEnabled();
    await resume.click();
    let moving = paused;
    await expect.poll(async () => {
      moving = await readPointFrame(page);
      return movedPointCount(paused, moving);
    }, { message: "Resume must change actual GPU-backed document point coordinates" }).toBeGreaterThanOrEqual(3);
    expect(moving.camera).toEqual(paused.camera);
    await page.getByRole("button", { name: "Pause layout", exact: true }).click();
    const stopped = await stablePointFrame(page);
    expect(stopped.camera).toEqual(paused.camera);

    const linkedIds = new Set(scene.edges.flatMap((edge) => [edge.source, edge.target]));
    const document = scene.nodes.find((node) => node.grain === "document" && linkedIds.has(node.id));
    if (!document) throw new Error("No original linked document in real PC10K response");
    await page.getByRole("searchbox", { name: "Find in visible map", exact: true }).fill(document.label);
    await page.getByTestId("workspace-graph-node-list")
      .locator(`li[data-graph-node-id="${document.id}"] button.workspace-graph-list-select`).click();
    const details = page.locator(".workspace-graph-details");
    await expect(details.getByRole("heading", { name: document.label, exact: true })).toBeVisible();
    await page.getByRole("searchbox", { name: "Find in visible map", exact: true }).fill("");
    await page.getByRole("button", { name: "Fit graph", exact: true }).click();
    await stablePointFrame(page);
    await info.attach("pc10k-normal-motion.png", { body: await page.screenshot(), contentType: "image/png" });

    // Exercise the real renderer palette-remount entrypoint, not a mocked
    // adapter or the Settings UI. Explicit user Pause must survive both ways.
    const themeCamera = await stablePointFrame(page);
    const originalTheme = await page.evaluate(() => window.document.documentElement.getAttribute("data-theme"));
    const otherTheme = originalTheme === "dark" ? "light" : "dark";
    for (const theme of [otherTheme, originalTheme]) {
      const previousCanvas = await canvas.locator("canvas").elementHandle();
      if (!previousCanvas) throw new Error("No paused native 10K canvas before palette remount");
      await page.evaluate(next => {
        if (next === null) window.document.documentElement.removeAttribute("data-theme");
        else window.document.documentElement.setAttribute("data-theme", next);
      }, theme);
      await expect.poll(() => previousCanvas.evaluate(element => element.isConnected)).toBe(false);
      await previousCanvas.dispose();
      await expect(canvas.locator("..")).toHaveAttribute("data-renderer-status", "ready");
      await expect(canvas).toHaveAttribute("data-map-node-count", "10000");
      await expectNativeCounts(page, 10_000, 30_895);
      await expect(page.getByRole("button", { name: "Resume layout", exact: true })).toBeEnabled();
      const remounted = await stablePointFrame(page);
      expect(sameWorkspaceRemountCamera(themeCamera.camera, remounted.camera)).toBe(true);
      expect(remounted.points.map(point => point.id).sort()).toEqual(themeCamera.points.map(point => point.id).sort());
      expect(movedPointCount(themeCamera, remounted, .05)).toBe(0);
      await expect(details.getByRole("heading", { name: document.label, exact: true })).toBeVisible();
    }
    await info.attach("pc10k-paused-theme-restored.png", { body: await page.screenshot(), contentType: "image/png" });

    // Lose the actual 10K native context, not a synthetic DOM event. Recovery
    // must keep the original scene/list/selection and the user's paused state.
    const originalCanvas = await canvas.locator("canvas").elementHandle();
    if (!originalCanvas) throw new Error("No native canvas to exercise context loss");
    await originalCanvas.evaluate((element) => {
      if (!(element instanceof HTMLCanvasElement)) throw new Error("Expected native HTML canvas");
      const extension = element.getContext("webgl2")?.getExtension("WEBGL_lose_context");
      if (!extension) throw new Error("Native context-loss extension unavailable");
      extension.loseContext();
    });
    await expect(canvas.locator("..")).toHaveAttribute("data-renderer-status", "error");
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "10000");
    await expect(details.getByRole("heading", { name: document.label, exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Retry interactive map", exact: true }).click();
    await expect(canvas.locator("..")).toHaveAttribute("data-renderer-status", "ready");
    expect(await originalCanvas.evaluate((element) => element.isConnected)).toBe(false);
    await originalCanvas.dispose();
    await expectNativeCounts(page, 10_000, 30_895);
    await expect(page.getByRole("button", { name: "Resume layout", exact: true })).toBeEnabled();
    await stablePointFrame(page);
    await expect(details.getByRole("heading", { name: document.label, exact: true })).toBeVisible();
    await info.attach("pc10k-context-recovered.png", { body: await page.screenshot(), contentType: "image/png" });
    await info.attach("pc10k-native-motion.json", { body: JSON.stringify({
      scope: "real API 10K/30895, native GL motion and context-loss/retry; no hardware latency claim",
      paused, moving, stopped, nativeDraw: await readNativeDraw(page), errors,
      sameOriginalUiResponses: responseReaders.get(page)!.evidence(),
    }, null, 2), contentType: "application/json" });
    expect(errors).toEqual([]);
  });

  test("exact 10K/100K remains selectable, exportable and returns to bounded 2K", async ({ page, browserName }, info) => {
    test.skip(process.env.DTXWIKI_E2E_GRAPH_10K_DENSE !== "1" || browserName !== "chromium",
      "The isolated wrapper enables the exact dense PC fixture for Chromium");
    test.setTimeout(240_000);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const errors = watchErrors(page);
    const nativeCost = process.env.DTXWIKI_E2E_GRAPH_NATIVE_COST === "1";
    if (nativeCost) nativeCostCollectors.set(page, captureNativeLongCalls(page));
    await installNativeDrawMeter(page, { longCallLog: nativeCost });
    const prepareProfile = process.env.DTXWIKI_E2E_GRAPH_DENSE_CPU_PROFILE === "1" ? async () => {
      const profile = createDenseCpuProfile(await page.context().newCDPSession(page));
      cpuProfiles.set(page, profile);
      await profile.prepare();
    } : undefined;
    const scene = await openPc10k(page, "e2e-graph-10k-dense-owner@example.com", 100_000, prepareProfile);
    await expectNativeCounts(page, 10_000, 100_000);
    const canvas = page.getByTestId("graph-canvas");
    const cpuProfile = cpuProfiles.get(page);
    if (cpuProfile) await cpuProfile.start();
    await expect.poll(() => visibleOrdinaryTitleCountOnPage(page),
      { message: "The dense native map must retain at least one actually visible ordinary document title" }).toBeGreaterThanOrEqual(1);
    const publicationReads: Array<{ startedNodeMs: number; endedNodeMs: number | null;
      status: "pending" | "returned" | "error"; finiteCoordinateCount: number | null; ids: string[]; error?: string }> = [];
    const nativeBeforePublication = await boundedNativeDiagnostics(page);
    let publicationPassed = false;
    let publicationError: string | null = null;
    try {
      await expect.poll(async () => {
        const observation: typeof publicationReads[number] = { startedNodeMs: performance.now(), endedNodeMs: null,
          status: "pending", finiteCoordinateCount: null, ids: [] };
        if (publicationReads.length < 40) publicationReads.push(observation);
        try {
          const frame = await readPointFrame(page);
          observation.status = "returned";
          observation.endedNodeMs = performance.now();
          observation.finiteCoordinateCount = frame.points.length;
          observation.ids = frame.points.map(point => point.id).slice(0, 40);
          return frame.points.length;
        } catch (error) {
          observation.status = "error";
          observation.endedNodeMs = performance.now();
          observation.error = String(error);
          throw error;
        }
      }).toBeGreaterThanOrEqual(3);
      publicationPassed = true;
    } catch (error) {
      publicationError = String(error);
      throw error;
    } finally {
      const nativeAfterPublication = await boundedNativeDiagnostics(page);
      const evidence = {
        scope: "Existing unchanged >=3/5s DOM-coordinate publication predicate; no additional GPU position reads",
        capturedNodeMs: performance.now(), publicationPassed, publicationError, publicationReads,
        nativeBeforePublication, nativeAfterPublication,
      };
      try {
        await info.attach("pc10k-100k-publication-diagnostic.json", { body: JSON.stringify(evidence, null, 2), contentType: "application/json" });
      } catch (failure) {
        if (publicationPassed) throw failure;
        // Preserve the original assertion failure if artifact storage also
        // fails; the bounded diagnostic is still retained in the case log.
        console.warn("Could not attach publication diagnostic", String(failure), JSON.stringify(evidence));
      }
    }
    const denseMoving = await readPointFrame(page);
    await expect.poll(async () => movedPointCount(denseMoving, await readPointFrame(page)),
      { message: "Maximum-capacity normal motion must move actual native document points" }).toBeGreaterThanOrEqual(3);
    await page.getByRole("button", { name: "Pause layout", exact: true }).click();
    await stablePointFrame(page);
    const nodes = page.getByTestId("workspace-graph-node-list");
    const edges = page.getByTestId("workspace-graph-edge-list");
    expect(await nodes.locator("li").count()).toBeLessThan(30);
    expect(await edges.locator("li").count()).toBeLessThan(30);
    await nodes.locator("button.workspace-graph-list-select").first().focus();
    await page.keyboard.press("End");
    const lastNode = scene.nodes.at(-1)!;
    await expect(nodes.locator(`li[data-graph-node-id="${lastNode.id}"] button`)).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator(".workspace-graph-details").getByRole("heading", { name: lastNode.label, exact: true })).toBeVisible();
    await edges.locator("button.workspace-graph-list-select").first().focus();
    await page.keyboard.press("End");
    await expect(edges.locator(`li[data-graph-edge-id="${scene.edges.at(-1)!.id}"] button`)).toBeFocused();

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Export visible map", exact: true }).click();
    const stream = await (await downloadPromise).createReadStream();
    if (!stream) throw new Error("No visible-map export stream");
    const chunks: Buffer[] = [];
    let exportBytes = 0;
    for await (const chunk of stream) {
      const buffer = Buffer.from(chunk);
      exportBytes += buffer.length;
      if (exportBytes > 192 * 1024 * 1024) throw new Error("Maximum-capacity export exceeded the bounded 192MiB reader");
      chunks.push(buffer);
    }
    const exported = JSON.parse(Buffer.concat(chunks).toString("utf8")) as {
      nodes: { id: string }[]; edges: { id: string; source: string; target: string }[];
    };
    expect(exported.nodes).toHaveLength(10_000);
    expect(new Set(exported.nodes.map((node) => node.id))).toEqual(new Set(scene.nodes.map((node) => node.id)));
    const original = new Map(scene.edges.map((edge) => [edge.id, `${edge.source}\0${edge.target}`]));
    expect(exported.edges).toHaveLength(100_000);
    expect(exported.edges.every((edge) => original.get(edge.id) === `${edge.source}\0${edge.target}`)).toBe(true);
    expect(new Set(exported.edges.map((edge) => edge.id)).size).toBe(100_000);
    await info.attach("pc10k-100k.png", { body: await page.screenshot(), contentType: "image/png" });

    const view = page.locator("details.workspace-graph-view-options");
    await view.locator("summary").click();
    const boundedRead = waitOverview(page, MOBILE_BUDGET.documents);
    await page.getByRole("combobox", { name: "Map node limit", exact: true }).selectOption("2k");
    const bounded = await boundedRead;
    expect(bounded.nodes.length).toBeLessThanOrEqual(2_000);
    expect(bounded.edges.length).toBeLessThanOrEqual(6_000);
    await expect(page.getByTestId("graph-canvas")).toHaveAttribute("data-map-node-count", String(bounded.nodes.length));
    await expectNativeCounts(page, bounded.nodes.length,
      bounded.edges.filter((edge) => edge.source !== edge.target).length);
    await info.attach("pc10k-100k-stability.json", { body: JSON.stringify({
      scope: "exact real API/native10K100K motion/pause and original export; maximum-capacity stability, not representative latency",
      counts: scene.counts, bounded: bounded.counts, errors, publicationReads, nativeBeforePublication,
      sameOriginalUiResponses: responseReaders.get(page)!.evidence(),
    }, null, 2), contentType: "application/json" });
    expect(errors).toEqual([]);
  });
});

async function waitOverview(page: Page, documents: number): Promise<WorkspaceGraphScene> {
  const reader = responseReaders.get(page);
  if (!reader) throw new Error("Original overview response reader must be installed before the UI read");
  const budget = documents === PC_BUDGET.documents ? PC_BUDGET : MOBILE_BUDGET;
  const response = await page.waitForResponse((entry) => entry.url().includes(OVERVIEW)
    && entry.request().method() === "POST" && entry.request().postDataJSON()?.budget?.documents === budget.documents
    && entry.request().postDataJSON()?.budget?.relations === budget.relations);
  expect(response.ok()).toBe(true);
  expect(response.request().postDataJSON()).toMatchObject({ budget, lens: "combined" });
  return reader.read(response);
}

async function openPc10k(page: Page, email: string, relations: number, before10k?: () => Promise<void>) {
  await login(page.request, email);
  const workspace = await currentWorkspace(page.request);
  // Keep the existing 64MiB representative bound. Only this known maximum
  // 100K-relations fixture opts into a bounded 192MiB original-body buffer.
  const reader = await createOverviewResponseReader(page, workspace.id,
    relations === 100_000 ? { resourceLimitMiB: 192 } : {});
  responseReaders.set(page, reader);
  const initial = waitOverview(page, 2_000);
  await page.goto("/graph");
  await initial;
  if (before10k) {
    await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]')).toBeVisible();
    await before10k();
  }
  const view = page.locator("details.workspace-graph-view-options");
  await view.locator("summary").click();
  const full = waitOverview(page, PC_BUDGET.documents);
  await page.getByRole("combobox", { name: "Map node limit", exact: true }).selectOption("10k");
  const scene = await full;
  expect(scene.representation).toBe("exact");
  expect(scene.completeness).toBe("complete");
  expect(scene.truncated).toBe(false);
  expect(scene.nodes).toHaveLength(10_000);
  expect(scene.edges).toHaveLength(relations);
  await view.locator("summary").click();
  const unlinked = page.getByRole("button", { name: /^Unlinked \d+$/ });
  if (await unlinked.count()) await unlinked.click();
  const canvas = page.getByTestId("graph-canvas");
  await expect(canvas).toHaveAttribute("data-map-node-count", "10000");
  await expect(canvas).toHaveAttribute("data-map-edge-count", String(relations));
  await expect(canvas).toHaveAttribute("data-map-community-layout-status", "applied", { timeout: 90_000 });
  await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]')).toBeVisible();
  return scene;
}

async function readPointFrame(page: Page) {
  return readWorkspacePointFrameOnPage(page);
}

async function boundedNativeDiagnostics(page: Page) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const metadata = readNativeDrawDiagnostics(page).then(value => ({ status: "returned" as const, value }),
    error => ({ status: "error" as const, error: String(error) }));
  const deadline = new Promise<{ status: "deadline"; diagnosticDeadlineMs: number }>(resolve => {
    timer = setTimeout(() => resolve({ status: "deadline", diagnosticDeadlineMs: 1_000 }), 1_000);
  });
  try { return await Promise.race([metadata, deadline]); }
  finally { if (timer !== undefined) clearTimeout(timer); }
}

type PointFrame = Awaited<ReturnType<typeof readPointFrame>>;

async function stablePointFrame(page: Page) {
  let previous = await readPointFrame(page);
  let stable = 0;
  await expect.poll(async () => {
    const next = await readPointFrame(page);
    const previousIds = new Set(previous.points.map((point) => point.id));
    stable = next.camera !== null && next.points.length >= 3 && next.points.length === previous.points.length
      && next.points.every((point) => previousIds.has(point.id))
      && JSON.stringify(next.camera) === JSON.stringify(previous.camera)
      && movedPointCount(previous, next, .05) === 0 ? stable + 1 : 0;
    previous = next;
    return stable;
  }, { intervals: [250], message: "Paused native document coordinates and camera must remain stable" }).toBeGreaterThanOrEqual(3);
  return previous;
}

function watchErrors(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (entry) => { if (entry.type() === "error") errors.push(entry.text()); });
  return errors;
}

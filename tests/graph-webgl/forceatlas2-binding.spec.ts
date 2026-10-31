import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";
import { fixtureServerConfig } from "./vite-fixture";
import type { BindingSnapshot } from "./forceatlas2-binding-entry";

let server: ViteDevServer, origin: string;
test.beforeAll(async () => {
  server = await createServer({ ...fixtureServerConfig,
    server: { host: "127.0.0.1", port: 0, fs: { allow: [path.resolve(".")] } } });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("Native FA2 binding fixture did not bind TCP");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });
test.afterEach(async ({ page }) => {
  await page.evaluate(() => window.__forceAtlas2Binding?.destroy());
});

async function fixture(page: Page): Promise<string[]> {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.setViewportSize({ width: 1174, height: 717 });
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/forceatlas2-binding.html")}`);
  await page.waitForFunction(() => ["ready", "error"].includes(window.__forceAtlas2Binding?.status));
  expect(await page.evaluate(() => ({ status: window.__forceAtlas2Binding.status,
    error: window.__forceAtlas2Binding.error ?? null }))).toEqual({ status: "ready", error: null });
  return errors;
}
async function evidence(info: TestInfo, name: string, payload: unknown): Promise<void> {
  const file = info.outputPath(`${name}.json`);
  await writeFile(file, JSON.stringify({ payload,
    scope: "Real native Cosmos rendering and direct FA2 binding. Forced manual native steps while paused; diagnostic readback outside counted steps. Not production adapter, physical pointer drag, full2K UX or target-device performance." }, null, 2));
  await info.attach(name, { path: file, contentType: "application/json" });
}
function valid(snapshot: BindingSnapshot): void {
  expect(snapshot.glError).toBe(0);
  expect(snapshot.contextLost).toBe(false);
  expect(snapshot.running).toBe(false);
  expect(snapshot.readPixelsDuringSteps).toBe(0);
  expect(snapshot.positions.every(Number.isFinite)).toBe(true);
  expect(snapshot.nativeRgba).toHaveLength(8);
  for (let node = 0; node < 2; node++) {
    expect(snapshot.nativeRgba.slice(node * 4, node * 4 + 2)).toEqual(snapshot.positions.slice(node * 2, node * 2 + 2));
    expect(snapshot.nativeRgba[node * 4 + 2]).toBe(node);
    expect(snapshot.nativeRgba[node * 4 + 3]).toBe(0);
  }
}

test("native FA2 GPU copy preserves signed coordinates, exact pins and renderer index channels without physics readback", async ({ page }, info) => {
  const errors = await fixture(page);
  const result = await page.evaluate(() => window.__forceAtlas2Binding.manual(3));
  const fit = await page.evaluate(() => window.__forceAtlas2Binding.fitAndDraw());
  await evidence(info, "native-signed-pair", { result, fit, errors });
  valid(result.before); valid(result.after);
  valid(fit.before); valid(fit.after);
  expect(result.before.positions).toEqual([-5000, -6000, 7000, 8000]);
  expect(result.after.positions.slice(0, 2)).toEqual(result.before.positions.slice(0, 2));
  expect(result.after.positions.slice(2)).not.toEqual(result.before.positions.slice(2));
  expect(result.after.positions[2]).toBeGreaterThan(4096);
  expect(result.after.generations).toBe(1);
  expect(result.after.simulationSteps - result.before.simulationSteps).toBe(3);
  expect(result.after.inputReads[0]).toEqual({ positions: [-7048, -8048, 4952, 5952],
    radii: [1, 1], sources: [0], targets: [1], weights: [1], origin: [2048, 2048] });
  expect(fit.after.positions).toEqual(fit.before.positions);
  expect(fit.after.generations).toBe(fit.before.generations);
  await info.attach("native-rendered-pair", { body: await page.screenshot(), contentType: "image/png" });
  expect(errors).toEqual([]);
});

test("color-only native render retains the model but same-count position/topology replacement creates a new generation", async ({ page }, info) => {
  const errors = await fixture(page);
  await page.evaluate(() => window.__forceAtlas2Binding.manual(1));
  const style = await page.evaluate(() => window.__forceAtlas2Binding.styleOnly());
  const replacement = await page.evaluate(() => window.__forceAtlas2Binding.replaceSameCount());
  await evidence(info, "native-style-and-replacement", { style, replacement, errors });
  for (const snapshot of [style.before, style.after, replacement.before, replacement.committed, replacement.after]) valid(snapshot);
  expect(style.after.positions).toEqual(style.before.positions);
  expect(style.after.generations).toBe(style.before.generations);
  expect(style.after.invalidations).toBe(style.before.invalidations);
  expect(replacement.committed.positions).toEqual(replacement.replacement);
  expect(replacement.committed.invalidations).toBeGreaterThan(replacement.before.invalidations);
  expect(replacement.after.generations).toBe(replacement.before.generations + 1);
  expect(replacement.after.inputReads.at(-1)).toEqual({ positions: [-6548, -7548, 6452, 7952],
    radii: [1, 1], sources: [1], targets: [0], weights: [7], origin: [2048, 2048] });
  expect(replacement.after.positions.slice(0, 2)).toEqual(replacement.replacement.slice(0, 2));
  expect(replacement.after.rendererLinkUploads).toEqual([[0, 1], [1, 0]]);
  expect(errors).toEqual([]);
});

test("explicit native drag-row upload preserves pins and moves a free model row without recreation", async ({ page }, info) => {
  const errors = await fixture(page);
  await page.evaluate(() => window.__forceAtlas2Binding.manual(1));
  const drag = await page.evaluate(() => window.__forceAtlas2Binding.dragRow(0, [-4321.25, -5432.5]));
  const free = await page.evaluate(() => window.__forceAtlas2Binding.dragRow(1, [9000.25, 11000.5]));
  await evidence(info, "native-drag-row-synchronization", { drag, free, errors });
  valid(drag.before); valid(drag.after);
  valid(free.before); valid(free.after);
  expect(drag.after.positions.slice(0, 2)).toEqual([-4321.25, -5432.5]);
  expect(drag.after.generations).toBe(drag.before.generations);
  expect(drag.after.invalidations).toBe(drag.before.invalidations);
  expect(drag.after.afterDragCalls - drag.before.afterDragCalls).toBe(1);
  expect(drag.after.simulationSteps - drag.before.simulationSteps).toBe(2);
  // Pinned native-copy preservation alone could hide a broken model import.
  // This unpinned row must continue near the uploaded point, not return to its
  // pre-drag model location. Far-separated circles receive no contact shift;
  // force clamp=10 bounds each size-aware step by log(11) model units.
  const freeDistance = Math.hypot(free.after.positions[2] - 9000.25, free.after.positions[3] - 11000.5);
  expect(freeDistance).toBeGreaterThan(0);
  expect(freeDistance).toBeLessThan(2 * Math.log(11) + .01);
  expect(free.after.positions.slice(0, 2)).toEqual([-4321.25, -5432.5]);
  expect(free.after.generations).toBe(free.before.generations);
  expect(free.after.invalidations).toBe(free.before.invalidations);
  expect(free.after.afterDragCalls - free.before.afterDragCalls).toBe(1);
  expect(free.after.simulationSteps - free.before.simulationSteps).toBe(2);
  expect(errors).toEqual([]);
});

test("public pause/resume retains the attached model and detaching destroys only simulation resources", async ({ page }, info) => {
  const errors = await fixture(page);
  await page.evaluate(() => window.__forceAtlas2Binding.manual(1));
  const pause = await page.evaluate(() => window.__forceAtlas2Binding.pauseResume());
  const continuation = await page.evaluate(() => window.__forceAtlas2Binding.manual(1));
  const detached = await page.evaluate(() => window.__forceAtlas2Binding.detach());
  await evidence(info, "native-pause-resume-detach", { pause, continuation, detached, errors });
  valid(pause.before); valid(pause.after); valid(continuation.before); valid(continuation.after);
  expect(pause.after.positions).toEqual(pause.before.positions);
  expect(pause.after.simulationSteps).toBe(pause.before.simulationSteps);
  expect(pause.resumedRunning).toBe(true);
  expect(continuation.after.generations).toBe(continuation.before.generations);
  expect(continuation.after.positions.slice(2)).not.toEqual(continuation.before.positions.slice(2));
  expect(detached).toEqual({ simulationDestroys: 1, frameStillAvailable: true, glError: 0 });
  await page.evaluate(() => window.__forceAtlas2Binding.destroy());
  expect(errors).toEqual([]);
});

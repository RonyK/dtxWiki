import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { createServer, type ViteDevServer } from "vite";
import type { ForceAtlas2Fixture } from "./forceatlas2-driver-entry";

type ReferenceSnapshot = { iteration: number; positions: number[]; force: number[]; previousForce: number[]; masses: number[] };
type Reference = { snapshot(): ReferenceSnapshot; step(iterations: number): ReferenceSnapshot };
// Dynamic file URL keeps the Node-only official oracle out of the browser.
let referenceModule: {
  createForceAtlas2Reference(input: ForceAtlas2Fixture & { initialForce?: number[] }): Reference;
};
let server: ViteDevServer, origin: string;
let evidenceSequence = 0;
test.beforeAll(async () => {
  referenceModule = await import(pathToFileURL(path.resolve("tests/graph-webgl/forceatlas2-reference.mjs")).href);
  server = await createServer({ root: path.resolve("frontend"), configFile: false, logLevel: "error",
    server: { host: "127.0.0.1", port: 0, fs: { allow: [path.resolve(".")] } } });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("GPU numerical fixture did not bind TCP");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

function pair(weight = 4): ForceAtlas2Fixture {
  return { nodeIds: ["left", "right"], edgeIds: ["original:forward"],
    positions: [-5, 0, 5, 0], radii: [1, 1], sources: [0], targets: [1], weights: [weight] };
}

async function fixture(page: Page) {
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/forceatlas2-driver.html")}`);
  await page.waitForFunction(() => !!window.__forceAtlas2Core);
}

async function verify(page: Page, info: TestInfo, input: ForceAtlas2Fixture, checkpoints: number[], pause = false) {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await fixture(page);
  const gpu = await page.evaluate(({ input, checkpoints, pause }) =>
    window.__forceAtlas2Core.run(input, checkpoints, pause), { input, checkpoints, pause });
  const reference = referenceModule.createForceAtlas2Reference(input);
  const cpu = [reference.snapshot()];
  for (const target of checkpoints) cpu.push(reference.step(target - cpu.at(-1)!.iteration));
  const artifact = info.outputPath(`full-model-parity-${++evidenceSequence}.json`);
  await writeFile(artifact, JSON.stringify({ input, gpu, cpu,
    tolerance: "absolute1e-4 + relative1e-4 per force/position component, GPU float vs upstream JS-double arithmetic",
    scope: "Dedicated-context complete-model GPU core only. Not product rendering, hard collision, live2K UX or target-device acceptance." }, null, 2));
  await info.attach("full-model-parity", { path: artifact, contentType: "application/json" });
  expect(errors).toEqual([]); expect(gpu.errors.every(error => error === 0)).toBe(true);
  expect(gpu.pausedUnchanged).toBe(true); expect(gpu.destroyedRejectsStep).toBe(true);
  expect(gpu.samples).toHaveLength(cpu.length);
  for (let sample = 0; sample < cpu.length; sample++) {
    expect(gpu.samples[sample].iteration).toBe(cpu[sample].iteration);
    expect(gpu.samples[sample].masses).toEqual(cpu[sample].masses);
    for (const column of ["positions", "force", "previousForce"] as const) {
      const actual = gpu.samples[sample][column], expected = cpu[sample][column];
      expect(actual).toHaveLength(expected.length);
      for (let row = 0; row < actual.length; row++) {
        expect(Number.isFinite(actual[row])).toBe(true);
        expect(Math.abs(actual[row] - expected[row]), `sample${sample} ${column}[${row}]`)
          .toBeLessThanOrEqual(1e-4 + 1e-4 * Math.abs(expected[row]));
      }
    }
  }
  return { gpu, cpu };
}

test("complete GPU pair forces and adaptive integration match official size-aware FA2, unclamped and clamped", async ({ page }, info) => {
  await verify(page, info, pair(1), [1, 3, 8]);
  await verify(page, info, pair(4), [1, 3, 8]);
});

test("parallel/reciprocal/self-loop masses and pinned force history match the original directed edge rows", async ({ page }, info) => {
  await verify(page, info, { nodeIds: ["left", "right", "third"], edgeIds: ["parallel:a", "parallel:b", "reverse", "loop"],
    positions: [-5, 0, 5, 0, 0, 12], radii: [1, 2, .5], sources: [0, 0, 1, 2], targets: [1, 1, 0, 2],
    weights: [4, 2, 1, 3], pins: [0] }, [1, 3, 8], true);
});

test("coincident, touching and overlapping glyphs retain the official soft-collision limitations", async ({ page }, info) => {
  for (const positions of [[0, 0, 0, 0], [-1, 0, 1, 0], [-.5, 0, .5, 0]]) {
    await verify(page, info, { ...pair(), positions }, [1, 8]);
  }
});

test("signed coordinates beyond the former SDK box are not clipped and pins survive pause/resume", async ({ page }, info) => {
  const input = { ...pair(), positions: [-5000, -6000, 7000, 8000], pins: [0] };
  const { gpu } = await verify(page, info, input, [1, 8], true);
  for (const sample of gpu.samples) {
    expect(sample.positions.slice(0, 2)).toEqual(input.positions.slice(0, 2));
    expect(sample.positions[2]).toBeGreaterThan(4096);
  }
});

function weightedBridge(): ForceAtlas2Fixture {
  const count = 32, sources: number[] = [], targets: number[] = [], weights: number[] = [];
  for (let group = 0; group < 4; group++) for (let a = 0; a < 8; a++) for (let b = a + 1; b < 8; b++) {
    sources.push(group * 8 + a); targets.push(group * 8 + b); weights.push(32);
  }
  sources.push(0, 16); targets.push(8, 24); weights.push(64, 1);
  const positions = Array.from({ length: count }, (_, index) => {
    const group = Math.floor(index / 8), angle = index % 8 * Math.PI / 4;
    return [(group % 2 === 0 ? -400 : 400) + (group % 2 === 0 ? 1 : -1) * 48 * Math.cos(angle),
      (group < 2 ? -400 : 400) + (group < 2 ? 1 : -1) * 48 * Math.sin(angle)];
  }).flat().map(Math.fround);
  return { nodeIds: Array.from({ length: count }, (_, index) => `context:${index}`),
    edgeIds: sources.map((_, index) => `original:${index}`), positions, radii: Array(count).fill(1), sources, targets, weights };
}

test("actual weighted cross-community bridge fixture follows the complete model without folder inputs", async ({ page }, info) => {
  const input = weightedBridge();
  const { gpu } = await verify(page, info, input, [1, 2, 3, 4, 5, 6, 7, 8]);
  const final = gpu.samples.at(-1)!.positions;
  const distance = (a: number, b: number) => Math.hypot(final[a * 2] - final[b * 2], final[a * 2 + 1] - final[b * 2 + 1]);
  expect(distance(0, 8)).toBeLessThan(distance(16, 24));
});

test("near-equilibrium weighted forces retain the official adaptive trajectory over 32 steps", async ({ page }, info) => {
  // Repeated Float32 intermediate rounding first perturbs force history, then
  // amplifies through swinging. Keep the full upstream model and every component.
  await verify(page, info, weightedBridge(), [1, 2, 8, 16, 32]);
});

test("identical-input GPU steps preserve the official weighted model and force history independently of accumulated drift", async ({ page }, info) => {
  await fixture(page);
  const input = weightedBridge(), checkpoints = [1, 2, 3, 4, 5, 6, 7, 8];
  const gpu = await page.evaluate(({ input, checkpoints }) => window.__forceAtlas2Core.run(input, checkpoints), { input, checkpoints });
  const predictions: ReferenceSnapshot[] = [];
  for (let step = 1; step < gpu.samples.length; step++) {
    const previous = gpu.samples[step - 1];
    predictions.push(referenceModule.createForceAtlas2Reference({ ...input,
      positions: previous.positions, initialForce: previous.force }).step(1));
  }
  const artifact = info.outputPath("identical-input-steps.json");
  await writeFile(artifact, JSON.stringify({ input, gpu, predictions,
    scope: "Official whole model from identical GPU Float32 positions AND force history. Same component tolerance; DOES NOT replace or erase the separate accumulated-trajectory gate." }, null, 2));
  await info.attach("identical-input-steps", { path: artifact, contentType: "application/json" });
  expect(gpu.errors.every(error => error === 0)).toBe(true);
  for (let step = 1; step < gpu.samples.length; step++) for (const key of ["positions", "force", "previousForce"] as const) {
    const actual = gpu.samples[step][key], expected = predictions[step - 1][key];
    expect(actual).toHaveLength(expected.length);
    for (let row = 0; row < expected.length; row++) {
      expect(Number.isFinite(actual[row])).toBe(true);
      expect(Math.abs(actual[row] - expected[row]), `identical-state step${step} ${key}[${row}]`)
        .toBeLessThanOrEqual(1e-4 + 1e-4 * Math.abs(expected[row]));
    }
  }
});

test("malformed model rows fail explicitly instead of dropping or normalizing authored data", async ({ page }) => {
  await fixture(page);
  for (const input of [{ ...pair(), radii: [0, 1] }, { ...pair(), weights: [0] },
    { ...pair(), positions: [0, 0] }, { ...pair(), targets: [2] }, { ...pair(), pins: [2] }]) {
    expect(await page.evaluate(input => window.__forceAtlas2Core.invalid(input), input)).toBeTruthy();
  }
});

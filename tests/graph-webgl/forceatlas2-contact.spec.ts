import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";
import type { ContactFixture } from "./forceatlas2-contact-entry";

let server: ViteDevServer, origin: string;
test.beforeAll(async () => {
  server = await createServer({ root: path.resolve("frontend"), configFile: false, logLevel: "error",
    server: { host: "127.0.0.1", port: 0, fs: { allow: [path.resolve(".")] } } });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("Contact numerical fixture did not bind TCP");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

const pair = (positions = [-.5, 0, .5, 0]): ContactFixture => ({
  state: [positions[0], positions[1], 3, -4, positions[2], positions[3], 5, -6], radii: [1, 1],
});

async function fixture(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/forceatlas2-contact.html")}`);
  await page.waitForFunction(() => !!window.__forceAtlas2Contact);
  return errors;
}

async function evidence(info: TestInfo, name: string, result: unknown) {
  const artifact = info.outputPath(`${name}.json`);
  await writeFile(artifact, JSON.stringify({ scope: "Separate bounded GPU contact augmentation; not exact upstream FA2, arbitrary-jam convergence or live 2K UX acceptance", result }, null, 2));
  await info.attach(name, { path: artifact, contentType: "application/json" });
}

async function run(page: Page, info: TestInfo, input: ContactFixture, name: string) {
  const errors = await fixture(page);
  const result = await page.evaluate(input => window.__forceAtlas2Contact.run(input), input);
  await evidence(info, name, result);
  expect(errors).toEqual([]); expect(result.errors.every(value => value === 0)).toBe(true);
  expect(result.before.state).toEqual(input.state.map(Math.fround)); expect(result.after).toEqual(result.before);
  expect(result.result.force).toEqual(result.before.force); expect(result.reprojected.force).toEqual(result.before.force);
  expect(result.result.state.every(Number.isFinite)).toBe(true); expect(result.reprojected.state.every(Number.isFinite)).toBe(true);
  expect(result.destroyedProject).toBe(true); expect(result.destroyedPins).toBe(true);
  return result;
}

test("free pair overlap splits penetration once and bounded projection preserves force history", async ({ page }, info) => {
  const result = await run(page, info, { ...pair(), iterations: 1 }, "free-pair");
  expect(result.result.positions).toEqual([-1, 0, 1, 0]);
  expect(result.reprojected).toEqual(result.result);
});

test("a pinned partner takes no correction and the free node takes full penetration", async ({ page }, info) => {
  const result = await run(page, info, { ...pair(), pins: [0] }, "one-pinned");
  expect(result.result.positions).toEqual([-.5, 0, 1.5, 0]);
  expect(result.reprojected).toEqual(result.result);
});

test("both-pinned overlap is explicitly unresolvable without changing coordinates or force history", async ({ page }, info) => {
  const result = await run(page, info, { ...pair(), pins: [0, 1] }, "both-pinned");
  expect(result.result).toEqual(result.before); expect(result.reprojected).toEqual(result.before);
});

test("coincident centers use deterministic opposite index directions without random kicks", async ({ page }, info) => {
  const result = await run(page, info, pair([0, 0, 0, 0]), "coincident-pair");
  expect(result.result.positions).toEqual([-1, 0, 1, 0]); expect(result.reprojected).toEqual(result.result);
});

test("exact-touch and separated circles receive no correction", async ({ page }, info) => {
  for (const [name, positions] of [["touching", [-1, 0, 1, 0]], ["separated", [-3, 0, 3, 0]]] as const) {
    const result = await run(page, info, pair([...positions]), name);
    expect(result.result).toEqual(result.before); expect(result.reprojected).toEqual(result.before);
  }
});

test("independent source packing and signed coordinates beyond the old box preserve original indexed rows", async ({ page }, info) => {
  const result = await run(page, info, { state: [5000, -6000, 2, -3, -7000, 8000, -4, .5, 0, 0, 7, -8],
    radii: [1, 2, 3], pins: [2], sourceDimensions: { width: 3, height: 1 } }, "source-packing");
  expect(result.dimensions).toEqual({ width: 2, height: 2 });
  expect(result.result).toEqual(result.before); expect(result.reprojected).toEqual(result.before);
});

test("pin mutations update the same geometry and invalid indices preserve the previous complete pin set", async ({ page }, info) => {
  const errors = await fixture(page), result = await page.evaluate(() => window.__forceAtlas2Contact.pinMutation());
  await evidence(info, "pin-mutations", result);
  expect(errors).toEqual([]); expect(result.errors.every(value => value === 0)).toBe(true);
  expect(result.samples.map(sample => sample.pins)).toEqual([[0], [1], [], [0, 1], [0, 2], []]);
  expect(result.samples.map(sample => sample.sample.positions)).toEqual([
    [-.5, 0, 1.5, 0], [-1.5, 0, .5, 0], [-1, 0, 1, 0], [-.5, 0, .5, 0], [-.5, 0, .5, 0], [-1, 0, 1, 0],
  ]);
  result.samples.forEach((sample, index) => {
    expect(sample.sample.force).toEqual([3, -4, 5, -6]);
    if (index === 4) expect(sample.error).toContain("outside the original node rows");
    else expect(sample.error).toBeNull();
  });
  expect(result.sourceAfter.state).toEqual(result.input.state);
});

test("malformed typed radii and pin rows are rejected rather than silently defaulted", async ({ page }, info) => {
  const errors = await fixture(page), results = [];
  for (const kind of ["wrong-type", "empty", "zero", "nonfinite", "underflow", "invalid-pin"]) {
    results.push({ kind, result: await page.evaluate(kind => window.__forceAtlas2Contact.invalid(kind), kind) });
  }
  await evidence(info, "invalid-inputs", results);
  expect(errors).toEqual([]);
  for (const sample of results) { expect(sample.result.error).toBeTruthy(); expect(sample.result.gl).toBe(0); }
});

test("caller GL guard restores actual state after contact factory/project and synchronous throws", async ({ page }, info) => {
  const errors = await fixture(page), result = await page.evaluate(() => window.__forceAtlas2Contact.stateGuard());
  await evidence(info, "caller-state-guard", result);
  expect(errors).toEqual([]); expect(result.errors.every(value => value === 0)).toBe(true);
  expect(result.caller).toMatchObject({ viewport: [3, 4, 17, 19], activeUnit: 2, flags: Array(6).fill(true),
    pixel: [8, 8, 1, 1, 1, 1, 1, 1, 1, 1], colorMask: [false, true, false, true],
    programPresent: true, vaoPresent: true, distinctFramebuffers: true, packPresent: true, unpackPresent: true,
    allTexturesAndSamplersPresent: true });
  expect(result.observations.map(sample => sample.name)).toEqual([
    "factory", "project", "project-then-throw", "factory-then-throw", "invalid-factory", "invalid-project",
  ]);
  result.observations.forEach((sample, index) => {
    expect(sample.threw).toBe(index >= 2);
    expect(Object.entries(sample.restored).filter(([key]) => key !== "units").every(([, value]) => value === true)).toBe(true);
    expect(sample.restored.units).toEqual(Array.from({ length: 4 }, () => ({ texture: true, sampler: true })));
  });
  expect(result.result.positions).toEqual([-1, 0, 1, 0]); expect(result.result.force).toEqual([3, -4, 5, -6]);
});

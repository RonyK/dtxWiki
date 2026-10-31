import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";

import { fixtureServerConfig } from "./vite-fixture";

import type {} from "./workspace-relation-forces-entry";

let server: ViteDevServer;
let origin: string;
test.beforeAll(async () => {
  server = await createServer({ ...fixtureServerConfig,
    server: { host: "127.0.0.1", port: 0, fs: { allow: [path.resolve(".")] } } });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("The relation force fixture did not bind TCP");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

type ForceEvidence = Awaited<ReturnType<Window["__workspaceRelationForces"]["manual"]>>;
async function attachEvidence(testInfo: TestInfo, name: string, payload: unknown): Promise<void> {
  const artifact = testInfo.outputPath(`${name}.json`);
  await writeFile(artifact, JSON.stringify(payload, null, 2));
  await testInfo.attach(name, { contentType: "application/json", path: artifact });
}
const bridgeEndpoints = [[0, 8], [16, 24]] as const;
const distance = (positions: number[], [source, target]: readonly [number, number]) =>
  Math.hypot(positions[source * 2] - positions[target * 2], positions[source * 2 + 1] - positions[target * 2 + 1]);

async function sample(page: Page, suffix = ""): Promise<ForceEvidence> {
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/workspace-relation-forces.html")}${suffix}`);
  await page.waitForFunction(() => ["ready", "error"].includes(window.__workspaceRelationForces?.status));
  expect(await page.evaluate(() => ({ status: window.__workspaceRelationForces.status,
    error: window.__workspaceRelationForces.error ?? null }))).toEqual({ status: "ready", error: null });
  return page.evaluate(() => window.__workspaceRelationForces.manual([1, 8]));
}

// Independent dimensional oracle: derive graph-space clearance from the
// observed native uploads and the pinned SDK's .08 canonical Fit padding.
// Do not import the production calibration/profile helpers or infer force
// units from the final worker-derived collision config.
function fitClearance(positions: number[], calibration: ForceEvidence["calibration"]) {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (let index = 0; index < positions.length; index += 2) {
    minX = Math.min(minX, positions[index]); maxX = Math.max(maxX, positions[index]);
    minY = Math.min(minY, positions[index + 1]); maxY = Math.max(maxY, positions[index + 1]);
  }
  const dx = maxX - minX, dy = maxY - minY;
  const zoom = Math.max(.001, Math.min(calibration.viewport.width * .84 / (dx === 0 ? 1 : dx),
    calibration.viewport.height * .84 / (dy === 0 ? 1 : dy)));
  return { radius: Math.max(...calibration.pointSizes) / (2 * zoom), padding: 1 / zoom };
}

function assertOriginalContract(evidence: ForceEvidence): void {
  expect(evidence.nodeIds).toHaveLength(32);
  expect(new Set(evidence.nodeIds).size).toBe(32);
  expect(evidence.edgeIds).toHaveLength(114); // Four complete8-node cliques and two ORIGINAL directed bridges.
  expect(new Set(evidence.edgeIds).size).toBe(114);
  expect(evidence.weights.slice(0, 112)).toEqual(new Array(112).fill(32));
  expect(evidence.weights.slice(112)).toEqual(evidence.swapped ? [1, 64] : [64, 1]);
  expect(evidence.workerInputs).toHaveLength(1);
  expect(evidence.workerInputs[0]).toEqual({ identityKey: evidence.identityKey,
    workspaceId: "workspace-relation-force-fixture", nodeKeys: evidence.nodeKeys,
    sources: evidence.sources, targets: evidence.targets, weights: evidence.weights });
  expect(evidence.worker.communityCount).toBe(4);
  expect(evidence.worker.communityIds).toHaveLength(32);
  const groupIds = [];
  for (let group = 0; group < 4; group++) {
    const ids = evidence.worker.communityIds.slice(group * 8, (group + 1) * 8);
    expect(new Set(ids).size).toBe(1);
    groupIds.push(ids[0]);
  }
  expect(new Set(groupIds).size).toBe(4);
  for (const [source, target] of bridgeEndpoints) {
    expect(evidence.worker.communityIds[source]).not.toBe(evidence.worker.communityIds[target]);
    expect(evidence.degrees[source]).toBe(8); expect(evidence.degrees[target]).toBe(8);
  }
  expect(evidence.worker.initialPositions).toHaveLength(64);
  expect(evidence.worker.initialPositions.every(Number.isFinite)).toBe(true);
  expect(evidence.calibration.fallbackPositions).toHaveLength(64);
  expect(evidence.calibration.fallbackPositions.every(Number.isFinite)).toBe(true);
  expect(evidence.calibration.fallbackPositions).not.toEqual(evidence.fixturePositions);
  expect(evidence.calibration.pointSizes).toHaveLength(32);
  expect(evidence.calibration.pointSizes.every(value => Number.isFinite(value) && value > 0)).toBe(true);
  expect(Number.isInteger(evidence.calibration.viewport.width)).toBe(true);
  expect(Number.isInteger(evidence.calibration.viewport.height)).toBe(true);
  expect(evidence.calibration.viewport.width).toBeGreaterThan(0);
  expect(evidence.calibration.viewport.height).toBeGreaterThan(0);
  const forceBasis = fitClearance(evidence.calibration.fallbackPositions, evidence.calibration);
  const collisionBasis = evidence.session === "matched" ? forceBasis
    : fitClearance(evidence.worker.initialPositions, evidence.calibration);
  expect(evidence.firstPositionUpload).toEqual(evidence.session === "matched"
    ? evidence.fixturePositions : evidence.calibration.fallbackPositions);
  expect(evidence.acceptedPositionUpload).toEqual(evidence.session === "matched"
    ? evidence.fixturePositions : evidence.worker.initialPositions);
  expect(evidence.accepted.tick).toBe(0);
  expect(evidence.accepted.alphaFromTick).toBeNull();
  if (evidence.session === "matched") expect(evidence.accepted.positions).toEqual(evidence.fixturePositions);
  const spacing = 4 * (forceBasis.radius + forceBasis.padding);
  expect(evidence.before.positions).toEqual(evidence.fixturePositions);
  expect(distance(evidence.before.positions, bridgeEndpoints[0])).toBe(704);
  expect(distance(evidence.before.positions, bridgeEndpoints[1])).toBe(704);
  expect(evidence.before.tick).toBe(0);
  expect(evidence.before.alphaFromTick).toBeNull();
  expect(evidence.initialAlpha).toBe(.35);
  for (const snapshot of [evidence.accepted, evidence.before, ...evidence.samples]) {
    expect(snapshot.positions).toHaveLength(64);
    expect(snapshot.positions.every(value => Number.isFinite(value) && value > 0 && value < 4096)).toBe(true);
    expect(snapshot.running).toBe(false);
    expect(snapshot.glError).toBe(0);
    expect(snapshot.clusterIndexUploads).toEqual([]);
    expect(snapshot.clusterTargetUploads).toEqual([]);
    expect(snapshot.nativeLinks).toEqual(evidence.sources.flatMap((source, index) => [source, evidence.targets[index]]));
    expect(snapshot.config).toMatchObject({ gravity: .25, center: 0, spring: 1,
      cluster: 0, friction: .85, collision: 1, spaceSize: 4096, simulationEnabled: true });
    expect(snapshot.config.collisionRadius).toBeGreaterThan(0);
    expect(snapshot.config.collisionPadding).toBeGreaterThan(0);
    expect(snapshot.config.collisionRadius).toBe(collisionBasis.radius);
    expect(snapshot.config.collisionPadding).toBe(collisionBasis.padding);
    expect(snapshot.config.distance).toBe(spacing);
    expect(snapshot.config.repulsion).toBe((spacing / 10) ** 2);
    expect(snapshot.config).toEqual(evidence.before.config);
    // Independent oracle for the production column: no community, folder,
    // camera or worker timing is part of native authored spring strength.
    expect(snapshot.linkStrengthUploads).toHaveLength(1);
    for (let edge = 0; edge < evidence.edgeIds.length; edge++) {
      const degree = Math.min(evidence.degrees[evidence.sources[edge]], evidence.degrees[evidence.targets[edge]]);
      const expected = Math.min(16, 1 + Math.log2(evidence.weights[edge])) / Math.max(1, degree);
      expect(snapshot.linkStrengthUploads[0][edge]).toBe(Math.fround(expected));
    }
  }
  expect(evidence.samples.map(snapshot => snapshot.tick)).toEqual([1, 8]);
  for (const snapshot of evidence.samples) {
    expect(snapshot.alphaFromTick).toBeGreaterThan(0);
    expect(snapshot.alphaFromTick).toBeLessThan(.35);
  }
}

test("matched-session actual authored bridge contraction follows occurrence weight across the real worker partition, not folder/community forces", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  const original = await sample(page, "?session=matched");
  const swapped = await sample(page, "?session=matched&swap=1");
  // Retain actual inputs/calls/positions even if the current native force
  // candidate fails the no-anchor or authored-contraction contract.
  await attachEvidence(testInfo, "actual-authored-bridge-raw", {
    qualification: "Matched-session manual32-document actual-worker/native inputs and readback; not fresh layout acceptance",
    original, swapped, errors,
  });
  assertOriginalContract(original); assertOriginalContract(swapped);
  expect(original.session).toBe("matched"); expect(swapped.session).toBe("matched");
  expect(swapped.nodeIds).toEqual(original.nodeIds);
  expect(swapped.nodeKeys).toEqual(original.nodeKeys);
  expect(swapped.edgeIds).toEqual(original.edgeIds);
  expect(swapped.sources).toEqual(original.sources); expect(swapped.targets).toEqual(original.targets);
  expect(swapped.degrees).toEqual(original.degrees);
  expect(swapped.fixturePositions).toEqual(original.fixturePositions);
  expect(swapped.calibration).toEqual(original.calibration);
  expect(swapped.worker.communityIds).toEqual(original.worker.communityIds);
  expect(swapped.before.config).toEqual(original.before.config);
  const comparisons = original.samples.map((snapshot, index) => {
    const control = swapped.samples[index];
    expect(control.tick).toBe(snapshot.tick);
    expect(control.alphaFromTick).toBe(snapshot.alphaFromTick);
    const contraction = (run: ForceEvidence, positions: number[]) => bridgeEndpoints.map(pair =>
      distance(run.before.positions, pair) - distance(positions, pair));
    const actual = contraction(original, snapshot.positions), swap = contraction(swapped, control.positions);
    // Counterbalanced contrast, not the invalid claim that the strongest edge
    // is always the closest edge in a conflicting multi-force graph.
    expect(actual[0], `tick${snapshot.tick}: strong top bridge versus weak matched bottom`).toBeGreaterThan(actual[1]);
    expect(swap[1], `tick${snapshot.tick}: strong bottom bridge versus weak matched top`).toBeGreaterThan(swap[0]);
    expect(actual[0], `tick${snapshot.tick}: same top bridge after authored weight swap`).toBeGreaterThan(swap[0]);
    expect(swap[1], `tick${snapshot.tick}: same bottom bridge after authored weight swap`).toBeGreaterThan(actual[1]);
    return { tick: snapshot.tick, alpha: snapshot.alphaFromTick,
      originalContraction: actual, swappedContraction: swap,
      counterbalancedStrongMinusWeak: (actual[0] - actual[1] + swap[1] - swap[0]) / 2 };
  });
  expect(errors).toEqual([]);
  await attachEvidence(testInfo, "actual-authored-bridge-swap", {
    qualification: "Matched-session manual32-document native force contribution only; not fresh layout/live2K readability or hardware performance",
    original, swapped, comparisons, errors,
  });
});

test("matched-session directory reassignment changes metadata but not actual worker or manual native authored forces", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  const original = await sample(page, "?session=matched");
  const reassigned = await sample(page, "?session=matched&folders=1");
  await attachEvidence(testInfo, "actual-force-folder-raw", {
    qualification: "Matched-session same local backend/indexed metadata inputs and readback before contract assertions",
    original, reassigned, errors,
  });
  assertOriginalContract(original); assertOriginalContract(reassigned);
  expect(original.session).toBe("matched"); expect(reassigned.session).toBe("matched");
  expect(reassigned.folderKeys).not.toEqual(original.folderKeys);
  expect(reassigned.crossFolder).not.toEqual(original.crossFolder);
  expect(reassigned.identityKey).toBe(original.identityKey);
  expect(reassigned.workerInputs).toEqual(original.workerInputs);
  expect(reassigned.worker).toEqual(original.worker);
  expect(reassigned.fixturePositions).toEqual(original.fixturePositions);
  expect(reassigned.calibration).toEqual(original.calibration);
  expect(reassigned.before.config).toEqual(original.before.config);
  for (let sampleIndex = 0; sampleIndex < original.samples.length; sampleIndex++) {
    const baseline = original.samples[sampleIndex], changed = reassigned.samples[sampleIndex];
    expect(changed.tick).toBe(baseline.tick);
    expect(changed.alphaFromTick).toBe(baseline.alphaFromTick);
    expect(changed.linkStrengthUploads).toEqual(baseline.linkStrengthUploads);
    // Exact same indexed native inputs on the same backend: folder metadata
    // does not enter any force shader. This is not cross-GPU bit determinism.
    expect(changed.positions).toEqual(baseline.positions);
  }
  expect(errors).toEqual([]);
  await attachEvidence(testInfo, "actual-force-folder-independence", {
    qualification: "Matched-session same local backend/indexed input metadata contrast; not fresh layout, cross-GPU determinism or hidden-data authorization",
    original, reassigned, errors,
  });
});

test("fresh native acceptance keeps fallback forces and derives each collision clearance from its actual accepted worker seed", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  const original = await sample(page);
  const swapped = await sample(page, "?swap=1");
  await attachEvidence(testInfo, "actual-fresh-worker-calibration", {
    qualification: "Fresh32-document native acceptance and independent units; worker-specific clearance may differ, so no pure cross-run shader-attribution or live2K quality claim",
    original, swapped, errors,
  });
  assertOriginalContract(original); assertOriginalContract(swapped);
  expect(original.session).toBe("fresh"); expect(swapped.session).toBe("fresh");
  expect(swapped.calibration).toEqual(original.calibration);
  expect(swapped.nodeIds).toEqual(original.nodeIds);
  expect(swapped.nodeKeys).toEqual(original.nodeKeys);
  expect(swapped.edgeIds).toEqual(original.edgeIds);
  expect(swapped.sources).toEqual(original.sources); expect(swapped.targets).toEqual(original.targets);
  expect(swapped.degrees).toEqual(original.degrees);
  expect(swapped.accepted.config.distance).toBe(original.accepted.config.distance);
  expect(swapped.accepted.config.repulsion).toBe(original.accepted.config.repulsion);
  expect(errors).toEqual([]);
});

test("collision own-cell centroid excludes self for separated points while a real overlap separates", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/workspace-relation-forces.html")}`);
  await page.waitForFunction(() => ["ready", "error"].includes(window.__workspaceRelationForces?.status));
  expect(await page.evaluate(() => ({ status: window.__workspaceRelationForces.status,
    error: window.__workspaceRelationForces.error ?? null }))).toEqual({ status: "ready", error: null });
  const evidence = await page.evaluate(() => window.__workspaceRelationForces.collisionOnly());
  await attachEvidence(testInfo, "actual-native-collision-self-centroid", { evidence, errors });
  expect(errors).toEqual([]);
  expect(evidence.pointSizes).toEqual([3, 3]);
  expect(evidence.samples.map(sample => sample.name)).toEqual([
    "separated-same-cell", "separated-translated", "overlap",
  ]);
  const [sameCell, translated, overlap] = evidence.samples;
  const config = sameCell.config;
  expect(config).toMatchObject({ gravity: 0, center: 0, repulsion: 0, spring: 0,
    cluster: 0, collision: 1, radius: 1.5, padding: 0, spaceSize: 4096 });
  const gridSize = Math.min(512, Math.max(32, Math.ceil(config.spaceSize / Math.max(config.radius + config.padding, 8))));
  const cellSize = config.spaceSize / gridSize;
  expect(cellSize).toBe(8);
  const offsets = [[0, 0], [.5, 0], [0, .5], [.5, .5]] as const;
  const sharedCells = (positions: number[]) => offsets.map(([x, y]) =>
    Math.floor((positions[0] + x * cellSize) / cellSize) === Math.floor((positions[2] + x * cellSize) / cellSize)
    && Math.floor((positions[1] + y * cellSize) / cellSize) === Math.floor((positions[3] + y * cellSize) / cellSize));
  expect(sharedCells(sameCell.input)).toEqual([true, false, true, false]);
  expect(sharedCells(translated.input)).toEqual([false, false, false, false]);
  expect(translated.input).toEqual(sameCell.input.map((value, index) => value + (index % 2 === 0 ? 3 : 0)));
  const diameter = 2 * (config.radius + config.padding);
  expect(distance(sameCell.input, [0, 1])).toBe(5);
  expect(distance(translated.input, [0, 1])).toBe(5);
  expect(5).toBeGreaterThan(diameter);
  expect(5 / 2).toBeLessThan(diameter); // The old own-cell centroid's false overlap.
  for (const sample of evidence.samples) {
    expect(sample.config).toEqual(config);
    expect(sample.before).toEqual(sample.input);
    expect(sample.after).toHaveLength(4);
    expect(sample.after.every(value => Number.isFinite(value) && value > 0 && value < config.spaceSize)).toBe(true);
    expect(sample.ticksBefore).toBe(0);
    expect(sample.ticksAfter).toBe(1);
    expect(sample.alphaAfter).toBeGreaterThan(0);
    expect(sample.running).toBe(false);
    expect(sample.glErrors).toEqual([0, 0]);
  }
  // Positive control comes before the exact stationary oracles: collision
  // must be live, not disabled to make the phantom-response case pass.
  expect(distance(overlap.before, [0, 1])).toBe(2);
  expect(distance(overlap.before, [0, 1])).toBeLessThan(diameter);
  expect(distance(overlap.after, [0, 1])).toBeGreaterThan(2);
  expect(translated.after).toEqual(translated.before);
  expect(sameCell.after).toEqual(sameCell.before);
});

test("shared RestLog GPU artifact obeys the independent seven-case relation law", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/workspace-relation-forces.html")}`);
  await page.waitForFunction(() => ["ready", "error"].includes(window.__workspaceRelationForces?.status));
  expect(await page.evaluate(() => ({ status: window.__workspaceRelationForces.status,
    error: window.__workspaceRelationForces.error ?? null }))).toEqual({ status: "ready", error: null });
  const evidence = await page.evaluate(() => window.__workspaceRelationForces.relationLaw());
  await attachEvidence(testInfo, "shared-rest-log-law", { evidence, errors });
  expect(evidence.cases.map(sample => sample.name)).toEqual([
    "compression", "rest", "extension20", "extension40", "sqrt-strength", "scale-covariance", "pinned-source",
  ]);
  expect(evidence.failures).toEqual([]);
  expect(evidence.passed).toBe(true);
  expect(errors).toEqual([]);
});

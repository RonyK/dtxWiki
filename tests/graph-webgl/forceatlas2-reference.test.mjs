import assert from "node:assert/strict";
import { test } from "node:test";
import { MultiDirectedGraph } from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import defaults from "graphology-layout-forceatlas2/defaults.js";
import { createForceAtlas2Reference, FORCEATLAS2_SETTINGS, FORCEATLAS2_SIZE_AWARE_SETTINGS,
  verifyForceAtlas2Source } from "./forceatlas2-reference.mjs";

const pair = () => ({ nodeIds: ["original:left", "original:right"], edgeIds: ["authored:left-right"],
  positions: [-5, 0, 5, 0], sources: [0], targets: [1], weights: [4] });

test("the unmodified upstream numerical oracle is pinned and defaults change only LinLog", () => {
  assert.equal(verifyForceAtlas2Source().version, "0.10.1");
  assert.deepEqual(FORCEATLAS2_SETTINGS, { ...defaults, linLogMode: true });
  assert(Object.isFrozen(FORCEATLAS2_SETTINGS));
});
test("reference steps exactly equal the official public API, including complete adaptive integration", () => {
  const input = pair(), graph = new MultiDirectedGraph();
  input.nodeIds.forEach((id, index) => graph.addNode(id, { x: input.positions[index * 2], y: input.positions[index * 2 + 1] }));
  graph.addDirectedEdgeWithKey(input.edgeIds[0], input.nodeIds[0], input.nodeIds[1], { weight: 4 });
  const expected = forceAtlas2(graph, { iterations: 3, settings: FORCEATLAS2_SETTINGS, getEdgeWeight: "weight" });
  const actual = createForceAtlas2Reference(input).step(3);
  assert.deepEqual(actual.positions, input.nodeIds.flatMap(id => [expected[id].x, expected[id].y]));
  assert.equal(actual.iteration, 3);
});
test("independent pair oracle checks weighted repulsion, gravity and logarithmic attraction before integration", () => {
  const actual = createForceAtlas2Reference(pair()).step();
  const leftForce = Math.fround(Math.fround(Math.fround(-25 / 10) + 5) + 4 * Math.log1p(10));
  assert.deepEqual(actual.masses, [5, 5]);
  assert.equal(actual.force[0], leftForce); assert.equal(actual.force[2], -leftForce);
  const speed = Math.log1p(leftForce / 2) / (1 + Math.sqrt(5 * leftForce));
  assert.equal(actual.positions[0], Math.fround(-5 + leftForce * speed));
  assert.equal(actual.positions[2], Math.fround(5 - leftForce * speed));
});
test("parallel, reciprocal and self-loop rows preserve IDs/direction and raw weighted masses", () => {
  const input = { ...pair(), nodeIds: ["__proto__", "constructor"], edgeIds: ["parallel:a", "parallel:b", "reverse", "loop"],
    sources: [0, 0, 1, 0], targets: [1, 1, 0, 0], weights: [2, 3, 4, 5] };
  const actual = createForceAtlas2Reference(input).snapshot();
  assert.deepEqual(actual.nodeIds, input.nodeIds); assert.deepEqual(actual.edgeIds, input.edgeIds);
  assert.deepEqual(actual.sources, input.sources); assert.deepEqual(actual.targets, input.targets);
  assert.deepEqual(actual.weights, input.weights); assert.deepEqual(actual.masses, [20, 10]);
  assert.deepEqual(actual.edgeMatrix, [0, 10, 2, 0, 10, 3, 10, 0, 4, 0, 0, 5]);
});
test("pinned source retains its coordinate but remains a force source", () => {
  const input = { ...pair(), pins: [0] }, actual = createForceAtlas2Reference(input).step(8);
  assert.deepEqual(actual.positions.slice(0, 2), input.positions.slice(0, 2));
  assert.notEqual(actual.positions[2], input.positions[2]);
  assert(actual.force[0] !== 0); assert.deepEqual(actual.fixed, [1, 0]);
});
test("explicit origin translation is covariant and does not rescale input geometry", () => {
  const direct = createForceAtlas2Reference(pair()).step(3);
  const translated = createForceAtlas2Reference({ ...pair(), positions: [2043, 2048, 2053, 2048], origin: [2048, 2048] }).step(3);
  assert.deepEqual(translated.positions, direct.positions.map((value, index) => value + translated.origin[index % 2]));
  assert.deepEqual(translated.force, direct.force);
});
test("caller input and old snapshots cannot mutate a running reference", () => {
  const input = pair(), saved = structuredClone(input), reference = createForceAtlas2Reference(input);
  const before = reference.snapshot(); before.nodeMatrix.fill(99); before.weights.fill(99);
  input.positions.fill(99); input.weights.fill(99);
  assert.deepEqual(reference.step(3), createForceAtlas2Reference(saved).step(3));
});
test("invalid inputs and fractional iterations fail without silently dropping original data", () => {
  for (const weight of [NaN, Infinity, -1, 0, 1e39, 1e-50]) assert.throws(() => createForceAtlas2Reference({ ...pair(), weights: [weight] }));
  assert.throws(() => createForceAtlas2Reference({ ...pair(), nodeIds: ["same", "same"] }));
  assert.throws(() => createForceAtlas2Reference({ ...pair(), sources: [2] }));
  assert.throws(() => createForceAtlas2Reference({ ...pair(), positions: [NaN, 0, 1, 0] }));
  assert.throws(() => createForceAtlas2Reference({ ...pair(), pins: [2] }));
  const reference = createForceAtlas2Reference(pair()), before = reference.snapshot();
  for (const iterations of [0, -1, .5, Infinity]) assert.throws(() => reference.step(iterations));
  assert.deepEqual(reference.snapshot(), before);
});
test("nonfinite upstream state is retained as failed evidence and cannot be resumed", () => {
  const reference = createForceAtlas2Reference({ ...pair(), weights: [1e30] });
  assert.throws(() => reference.step(), /Nonfinite FA2 state/);
  const failed = reference.snapshot(); assert.equal(failed.failed, true); assert.equal(failed.iteration, 1);
  assert.throws(() => reference.step(), /not resumable/);
  assert.deepEqual(reference.snapshot(), failed);
});

test("explicit physical radii select only the immutable official size-aware profile", () => {
  assert.deepEqual(FORCEATLAS2_SIZE_AWARE_SETTINGS, { ...defaults, linLogMode: true, adjustSizes: true });
  assert(Object.isFrozen(FORCEATLAS2_SIZE_AWARE_SETTINGS));
  const plain = createForceAtlas2Reference(pair()).snapshot();
  const sized = createForceAtlas2Reference({ ...pair(), radii: [1.25, .5] }).snapshot();
  assert.deepEqual(plain.settings, FORCEATLAS2_SETTINGS); assert.equal(plain.radii, null);
  assert.deepEqual(sized.settings, FORCEATLAS2_SIZE_AWARE_SETTINGS);
  assert.deepEqual(sized.radii, [1.25, .5]);
  assert.deepEqual([sized.nodeMatrix[8], sized.nodeMatrix[18]], [1.25, .5]);
  assert.deepEqual(sized.positions, plain.positions); assert.deepEqual(sized.masses, plain.masses);
});

test("size-aware reference matches the official public API with radii, pins and original edge rows", () => {
  const input = { nodeIds: ["left", "right", "third"], edgeIds: ["parallel:a", "parallel:b", "reverse", "loop"],
    positions: [-5, 0, 5, 0, 0, 12], sources: [0, 0, 1, 2], targets: [1, 1, 0, 2],
    weights: [4, 2, 1, 3], radii: [1, 2, .5], pins: [0] };
  const graph = new MultiDirectedGraph();
  input.nodeIds.forEach((id, index) => graph.addNode(id, {
    x: input.positions[index * 2], y: input.positions[index * 2 + 1],
    size: input.radii[index], fixed: input.pins.includes(index),
  }));
  input.edgeIds.forEach((id, index) => graph.addDirectedEdgeWithKey(id,
    input.nodeIds[input.sources[index]], input.nodeIds[input.targets[index]], { weight: input.weights[index] }));
  const expected = forceAtlas2(graph, { iterations: 6, settings: FORCEATLAS2_SIZE_AWARE_SETTINGS, getEdgeWeight: "weight" });
  const reference = createForceAtlas2Reference(input);
  reference.step(2);
  const actual = reference.step(4);
  assert.deepEqual(actual.positions, input.nodeIds.flatMap(id => [expected[id].x, expected[id].y]));
  assert.equal(actual.iteration, 6); assert.deepEqual(actual.radii, input.radii);
  assert.deepEqual(actual.edgeIds, input.edgeIds); assert.deepEqual(actual.sources, input.sources);
  assert.deepEqual(actual.targets, input.targets); assert.deepEqual(actual.weights, input.weights);
  assert.deepEqual(actual.masses, [8, 8, 7]); assert.deepEqual(actual.fixed, [1, 0, 0]);
});

test("independent size-aware pair checks clearance forces, aggregate clamp and its own adaptive integration", () => {
  for (const weight of [1, 4]) {
    const mass = 1 + weight, distance = 10, clearance = distance - 1 - 1;
    const actual = createForceAtlas2Reference({ ...pair(), weights: [weight], radii: [1, 1] }).step();
    // Float32 force accumulation is ordered repulsion, gravity, then attraction.
    const repulsion = -mass * mass * distance / (clearance * clearance);
    const attraction = weight * distance * Math.log1p(clearance) / clearance;
    const rawForce = Math.fround(Math.fround(Math.fround(repulsion) + mass) + attraction);
    assert(weight === 1 ? rawForce < 10 : rawForce > 10, "Exercise both unclamped and clamped branches");
    const force = Math.min(10, rawForce);
    assert.deepEqual(actual.masses, [mass, mass]);
    assert.deepEqual(actual.force, [force, 0, -force, 0]);
    const speed = .1 * Math.log1p(force / 2) / (1 + Math.sqrt(mass * force));
    assert.deepEqual(actual.positions, [Math.fround(-5 + force * speed), 0, Math.fround(5 - force * speed), 0]);
    assert.deepEqual(actual.previousForce, [0, 0, 0, 0]);
    assert.deepEqual(actual.convergence, [1, 1], "Size-aware upstream branch does not update convergence");
  }
});

test("official size-aware model cannot separate exact coincident centers at the gravity origin", () => {
  const input = { ...pair(), positions: [0, 0, 0, 0], radii: [1, 1] };
  const actual = createForceAtlas2Reference(input).step(64);
  assert.deepEqual(actual.positions, input.positions);
  assert.deepEqual(actual.force, [0, 0, 0, 0]); assert.equal(actual.failed, false);
  assert.deepEqual(actual.radii, [1, 1]); assert.deepEqual(actual.masses, [5, 5]);
});

test("exact touching suppresses both pair forces and ordinary gravity can move nodes into overlap", () => {
  const actual = createForceAtlas2Reference({ ...pair(), positions: [-1, 0, 1, 0], radii: [1, 1] }).step();
  assert.deepEqual(actual.force, [5, 0, -5, 0], "Only ordinary gravity remains when clearance is exactly zero");
  const speed = .1 * Math.log1p(5 / 2) / (1 + Math.sqrt(5 * 5));
  assert.deepEqual(actual.positions, [Math.fround(-1 + 5 * speed), 0, Math.fround(1 - 5 * speed), 0]);
  assert(actual.positions[2] - actual.positions[0] < 2, "The official model is not a hard contact constraint");
});

test("overlapping centers repel outward but a single size-aware step does not guarantee clearance", () => {
  const input = { ...pair(), positions: [-.5, 0, .5, 0], radii: [1, 1] };
  const actual = createForceAtlas2Reference(input).step();
  assert.deepEqual(actual.force, [-10, 0, 10, 0]);
  const speed = .1 * Math.log1p(10 / 2) / (1 + Math.sqrt(5 * 10));
  assert.deepEqual(actual.positions, [Math.fround(-.5 - 10 * speed), 0, Math.fround(.5 + 10 * speed), 0]);
  const separation = actual.positions[2] - actual.positions[0];
  assert(separation > 1); assert(separation < 2, "Do not promote soft repulsion to a nonoverlap guarantee");
});

test("a size-aware pin remains fixed, influences its neighbor and skips the free-node force clamp", () => {
  const input = { ...pair(), positions: [-.5, 0, .5, 0], radii: [1, 1], pins: [0] };
  const reference = createForceAtlas2Reference(input), actual = reference.step();
  assert.deepEqual(actual.fixed, [1, 0]);
  assert.deepEqual(actual.positions.slice(0, 2), input.positions.slice(0, 2));
  assert.equal(actual.force[0], -100 * 5 * 5 + 5);
  assert.equal(actual.force[2], 10);
  assert(actual.positions[2] > input.positions[2]);
  assert.deepEqual(reference.step(8).positions.slice(0, 2), input.positions.slice(0, 2));
});

test("invalid physical radius rows fail instead of falling through the upstream size default", () => {
  for (const radii of [[], [1], [1, 1, 1]]) {
    assert.throws(() => createForceAtlas2Reference({ ...pair(), radii }), /Indexed radius rows disagree/);
  }
  for (const invalid of [NaN, Infinity, -Infinity, -1, 0, -0, 1e39, 1e-50]) {
    for (const radii of [[invalid, 1], [1, invalid]]) {
      assert.throws(() => createForceAtlas2Reference({ ...pair(), radii }), /Finite positive Float32 model radius required/);
    }
  }
});

test("mutating caller radius rows or old snapshots cannot change the size-aware model or its state", () => {
  const input = { ...pair(), radii: Float32Array.from([1.25, .5]) }, saved = structuredClone(input);
  const reference = createForceAtlas2Reference(input), before = reference.snapshot();
  before.radii.fill(99); before.nodeMatrix[8] = 99; before.settings.adjustSizes = false;
  input.radii.fill(99);
  assert.deepEqual(reference.step(3), createForceAtlas2Reference(saved).step(3));
  assert.deepEqual(FORCEATLAS2_SIZE_AWARE_SETTINGS, { ...defaults, linLogMode: true, adjustSizes: true });
});

test("size-aware force-history restoration predicts the same complete next states without restarting integration", () => {
  const input = { ...pair(), radii: [1, 1] }, running = createForceAtlas2Reference(input);
  const saved = running.step(2);
  const resumed = createForceAtlas2Reference({ ...input, positions: saved.positions, initialForce: saved.force });
  const continued = running.step(4), restored = resumed.step(4);
  assert.deepEqual(restored.nodeMatrix, continued.nodeMatrix);
  assert.deepEqual(restored.edgeMatrix, continued.edgeMatrix);
  assert.throws(() => createForceAtlas2Reference({ ...pair(), initialForce: [0, 0, 0, 0] }), /size-aware/);
  for (const initialForce of [[], [0, 0], [NaN, 0, 0, 0], [1e100, 0, 0, 0]]) {
    assert.throws(() => createForceAtlas2Reference({ ...input, initialForce }));
  }
});

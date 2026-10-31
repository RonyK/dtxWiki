import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

import { MultiDirectedGraph } from "graphology";
import iterate from "graphology-layout-forceatlas2/iterate.js";
import helpers from "graphology-layout-forceatlas2/helpers.js";

// An independent CPU oracle for a future complete GPU model, never a runtime
// replacement for the product simulation. Keep the upstream algorithm intact.
export const FORCEATLAS2_SOURCE = Object.freeze({
  version: "0.10.1", commit: "ee71891109dfa22394eedb7cbeb4e143625b9bb6",
  files: Object.freeze({
    "iterate.js": "a20b1bbc2e5dfb94fc09f8ce75acf600974893a658a5e188bb2e429598cd2276",
    "helpers.js": "5973958b9d2aafcd7a3274ef9e3c06c8367a7fb3f9b76f3e39571c727c3d3807",
    "defaults.js": "fb2e0de65a11a8c1a3488ceabdbf4d24f16fddd90db8a5cfcc3ec33c705a765c",
  }),
});
export const FORCEATLAS2_SETTINGS = Object.freeze({
  linLogMode: true, outboundAttractionDistribution: false, adjustSizes: false,
  edgeWeightInfluence: 1, scalingRatio: 1, strongGravityMode: false,
  gravity: 1, slowDown: 1, barnesHutOptimize: false, barnesHutTheta: .5,
});
// The upstream size-aware profile changes the whole integration branch, not
// only collision forces. Never graft these equations into the previous model.
export const FORCEATLAS2_SIZE_AWARE_SETTINGS = Object.freeze({ ...FORCEATLAS2_SETTINGS, adjustSizes: true });
const NODE_STRIDE = 10, EDGE_STRIDE = 3;

export function verifyForceAtlas2Source() {
  const require = createRequire(import.meta.url);
  const metadata = require("graphology-layout-forceatlas2/package.json");
  assert.equal(metadata.version, FORCEATLAS2_SOURCE.version, "Review the numerical oracle before upgrading");
  for (const [file, expected] of Object.entries(FORCEATLAS2_SOURCE.files)) {
    const source = readFileSync(require.resolve(`graphology-layout-forceatlas2/${file}`));
    assert.equal(createHash("sha256").update(source).digest("hex"), expected, `Unreviewed FA2 ${file}`);
  }
  return FORCEATLAS2_SOURCE;
}

/**
 * Preserve caller indexing and every directed edge row. Origin translation is
 * explicit because upstream ordinary gravity acts toward (0,0), unlike Cosmos.
 * @param {{nodeIds:string[],edgeIds:string[],positions:ArrayLike<number>,sources:ArrayLike<number>,
 * targets:ArrayLike<number>,weights:ArrayLike<number>,origin?:number[],pins?:number[],radii?:ArrayLike<number>,initialForce?:ArrayLike<number>}} input
 */
export function createForceAtlas2Reference(input) {
  verifyForceAtlas2Source();
  const nodeIds = [...input.nodeIds], edgeIds = [...input.edgeIds];
  const count = nodeIds.length, size = edgeIds.length;
  const radii = input.radii === undefined ? null : Array.from(input.radii);
  const settings = radii ? FORCEATLAS2_SIZE_AWARE_SETTINGS : FORCEATLAS2_SETTINGS;
  if (radii) {
    assert.equal(radii.length, count, "Indexed radius rows disagree");
    assert(radii.every(radius => Number.isFinite(Math.fround(radius)) && Math.fround(radius) > 0),
      "Finite positive Float32 model radius required");
  }
  assert(nodeIds.every(id => typeof id === "string") && new Set(nodeIds).size === count, "Unique node IDs required");
  assert(edgeIds.every(id => typeof id === "string") && new Set(edgeIds).size === size, "Unique edge IDs required");
  assert.equal(input.positions.length, count * 2, "Indexed position rows disagree");
  for (const column of [input.sources, input.targets, input.weights]) assert.equal(column.length, size, "Indexed edge rows disagree");
  const origin = [...(input.origin ?? [0, 0])];
  assert(origin.length === 2 && origin.every(Number.isFinite), "Finite origin required");
  const pins = new Set(input.pins ?? []);
  assert([...pins].every(index => Number.isInteger(index) && index >= 0 && index < count), "Invalid pin index");
  const sources = Array.from(input.sources), targets = Array.from(input.targets), weights = Array.from(input.weights);
  const graph = new MultiDirectedGraph();
  for (let index = 0; index < count; index++) {
    const x = input.positions[index * 2] - origin[0], y = input.positions[index * 2 + 1] - origin[1];
    assert(Number.isFinite(Math.fround(x)) && Number.isFinite(Math.fround(y)), "Finite Float32 position required");
    // Numeric internal keys avoid upstream's plain-object key hazards. Original
    // caller IDs are retained separately and never replaced in output evidence.
    graph.addNode(String(index), { x, y, fixed: pins.has(index), ...(radii ? { size: radii[index] } : {}) });
  }
  for (let index = 0; index < size; index++) {
    const source = sources[index], target = targets[index], weight = weights[index];
    assert([source, target].every(value => Number.isInteger(value) && value >= 0 && value < count), "Invalid edge endpoint");
    assert(Number.isFinite(Math.fround(weight)) && Math.fround(weight) > 0, "Finite positive Float32 weight required");
    graph.addDirectedEdgeWithKey(String(index), String(source), String(target), { weight });
  }
  const matrices = helpers.graphToByteArrays(graph, (_key, attr) => attr.weight);
  assert(Array.from(matrices.nodes).every(Number.isFinite), "Float32 weighted mass overflow");
  // Identical-input one-step GPU diagnostics must restore the complete force
  // history, not just positions. Only the size-aware profile has no evolving
  // convergence field; do not pretend this restores the other profile's state.
  if (input.initialForce !== undefined) {
    assert(radii, "Force-history restoration requires the size-aware profile");
    assert.equal(input.initialForce.length, count * 2, "Indexed force-history rows disagree");
    for (let node = 0; node < count; node++) for (let axis = 0; axis < 2; axis++) {
      const force = Math.fround(input.initialForce[node * 2 + axis]);
      assert(Number.isFinite(force), "Finite Float32 force history required");
      matrices.nodes[node * NODE_STRIDE + 2 + axis] = force;
    }
  }
  assert.equal(helpers.validateSettings(settings), null);
  let iteration = 0, failed = false;
  function snapshot() {
    const positions = [], masses = [], force = [], previousForce = [], convergence = [], fixed = [];
    for (let index = 0; index < count; index++) {
      const offset = index * NODE_STRIDE;
      positions.push(matrices.nodes[offset] + origin[0], matrices.nodes[offset + 1] + origin[1]);
      force.push(matrices.nodes[offset + 2], matrices.nodes[offset + 3]);
      previousForce.push(matrices.nodes[offset + 4], matrices.nodes[offset + 5]);
      masses.push(matrices.nodes[offset + 6]); convergence.push(matrices.nodes[offset + 7]);
      fixed.push(matrices.nodes[offset + 9]);
    }
    return { iteration, failed, nodeIds: [...nodeIds], edgeIds: [...edgeIds], sources: [...sources], targets: [...targets],
      weights: [...weights], origin: [...origin], settings: { ...settings },
      radii: radii ? [...radii] : null,
      positions, masses, force, previousForce, convergence, fixed,
      nodeMatrix: Array.from(matrices.nodes), edgeMatrix: Array.from(matrices.edges),
      nodeStride: NODE_STRIDE, edgeStride: EDGE_STRIDE };
  }
  return {
    snapshot,
    step(iterations = 1) {
      assert(!failed, "Discard failed numerical reference; its state is not resumable");
      assert(Number.isSafeInteger(iterations) && iterations > 0, "Positive integer iteration count required");
      try {
        for (let index = 0; index < iterations; index++) {
          iterate(settings, matrices.nodes, matrices.edges);
          iteration++;
          assert(Array.from(matrices.nodes).every(Number.isFinite), `Nonfinite FA2 state at iteration ${iteration}`);
        }
      } catch (error) { failed = true; throw error; }
      return snapshot();
    },
  };
}

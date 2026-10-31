import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { COSMOS_COLLISION_RADII_PATCH_HUNKS, patchCosmosCollisionRadii } from "./cosmos-collision-radii-patch.mjs";
import { patchCosmosGpuSimulation } from "./cosmos-gpu-simulation-patch.mjs";
import { COSMOS_VERSION } from "./cosmos-rest-log-patch.mjs";

const require = createRequire(import.meta.url);
const original = await readFile(require.resolve("@cosmos.gl/graph"), "utf8");
const hostSource = patchCosmosGpuSimulation(original, COSMOS_VERSION);
const modified = patchCosmosCollisionRadii(hostSource, COSMOS_VERSION);
const apiHunk = COSMOS_COLLISION_RADII_PATCH_HUNKS.find(hunk => hunk.id === "collision-radii-api");
const methods = apiHunk.after.slice(0, -apiHunk.before.length);
const Host = new Function(`return class Host { ${methods} }`)();

test("all glyph collision changes reverse exactly and preserve original force shaders and clamp", () => {
  let reversed = modified;
  for (const { before, after } of [...COSMOS_COLLISION_RADII_PATCH_HUNKS].reverse()) {
    assert.equal(reversed.split(after).length, 2);
    reversed = reversed.replace(after, before);
  }
  assert.equal(reversed, hostSource);
  assert.ok(modified.includes("pointPosition.r = clamp(pointPosition.r, 0.0, spaceSize);"));
  assert.throws(() => patchCosmosCollisionRadii(modified, COSMOS_VERSION));
  assert.throws(() => patchCosmosCollisionRadii(`${hostSource}\n`, COSMOS_VERSION));
  assert.throws(() => patchCosmosCollisionRadii(hostSource, "next"));
});

function host() {
  const value = new Host();
  value.graph = { pointsNumber: 3 };
  value.isForceCollisionReady = true;
  value.requestRender = () => { value.requested = (value.requested ?? 0) + 1; };
  return value;
}

test("radius ownership is copied, optional and invalidates only collision resources", () => {
  const value = host(), radii = Float32Array.from([1, 3, 2]);
  value.setPointCollisionRadii(null);
  assert.equal(value.requested, undefined);
  value.setPointCollisionRadii(radii);
  radii.fill(99);
  assert.deepEqual([...value.graph._dtxCollisionRadii], [1, 3, 2]);
  assert.equal(value.isForceCollisionReady, false);
  value.setPointCollisionRadii(null);
  assert.equal(value.graph._dtxCollisionRadii, null);
  assert.equal(value.requested, 2);
});

test("invalid columns fail before replacing the current owner's geometry", () => {
  const value = host();
  value.setPointCollisionRadii(Float32Array.from([1, 3, 2]));
  for (const radii of [new Float32Array(2), [1, 2, 3], Float32Array.from([1, 0, 2]),
    Float32Array.from([1, NaN, 2]), Float32Array.from([1, Infinity, 2]), Float32Array.from([1, 3e38, 2])]) {
    assert.throws(() => value.setPointCollisionRadii(radii));
    assert.deepEqual([...value.graph._dtxCollisionRadii], [1, 3, 2]);
  }
});

function collision(source, radii) {
  const begin = source.indexOf("class ze extends V {");
  const createStart = source.indexOf("  create() {", begin);
  const end = source.indexOf("  initPrograms() {", createStart);
  const create = source.slice(createStart, end);
  const Collision = new Function("E", "Y", "m", "P", "Z", "x", `return class Collision { ${create} }`)(
    { pointDefaultSize: 4 }, [[0, 0], [.5, 0], [0, .5], [.5, .5]],
    { SAMPLE: 1, RENDER: 2, COPY_DST: 4 }, () => 32, () => new Float32Array(8), { VERTEX: 1, COPY_DST: 4 },
  );
  const value = new Collision();
  value.device = {
    createTexture(options) { return { ...options, copyImageData({ data }) { this.uploaded = data; } }; },
    createFramebuffer(options) { return options; }, createBuffer(options) { return options; },
  };
  value.store = { pointsTextureSize: 2, adjustedSpaceSize: 4096 };
  value.data = { pointsNumber: 3, pointSizes: true, _dtxCollisionRadii: radii,
    getResolvedPointSize(index) { return [2, 6, 4][index]; } };
  value.config = { simulationCollisionRadius: 0, simulationCollisionPadding: 1 };
  value.gridTargets = [];
  value.destroyGridTargets = () => { value.gridTargets = []; };
  value.create();
  return value;
}

test("null executes identical upstream collision grid and size upload", () => {
  const stock = collision(original, null), current = collision(modified, null);
  assert.equal(current.cellSize, stock.cellSize);
  assert.equal(current.gridTextureSize, stock.gridTextureSize);
  assert.deepEqual(current.sizeTexture.uploaded, stock.sizeTexture.uploaded);
});

test("actual modified native create uploads each world diameter and grids by the largest", () => {
  const current = collision(modified, Float32Array.from([4, 12, 8]));
  assert.deepEqual([0, 4, 8].map(offset => current.sizeTexture.uploaded[offset]), [8, 24, 16]);
  assert.ok(current.cellSize >= 2 * (12 + 1));
  assert.throws(() => collision(modified, new Float32Array(2)), /Stale collision radius rows/);
});

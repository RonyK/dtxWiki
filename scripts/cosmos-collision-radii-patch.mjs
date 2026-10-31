import assert from "node:assert/strict";
import { COSMOS_SOURCE_SHA256, COSMOS_VERSION, sha256 } from "./cosmos-rest-log-patch.mjs";
import { COSMOS_GPU_SIMULATION_PATCH_HUNKS } from "./cosmos-gpu-simulation-patch.mjs";

const pinnedPointsComment = "  /**\n   * Sets which points are pinned (fixed) in position.";
const collisionMethods = `  /** Optional native-world glyph radii; null preserves the upstream size column. */
  setPointCollisionRadii(radii) {
    if (this._isDestroyed) return;
    if (radii !== null && (!(radii instanceof Float32Array) || radii.length !== this.graph.pointsNumber ||
        !radii.every(value => Number.isFinite(value) && value > 0 && Number.isFinite(Math.fround(value * 2))))) {
      throw new TypeError("Collision radii must match every point with a positive finite native radius");
    }
    if (radii === null && this.graph._dtxCollisionRadii == null) return;
    this.graph._dtxCollisionRadii = radii?.slice() ?? null;
    this.isForceCollisionReady = false;
    this.requestRender();
  }
`;
const collisionSizes = `    let s = n.pointDefaultSize ?? E.pointDefaultSize;
    if (i.pointSizes && i.pointsNumber !== void 0)
      for (let p = 0; p < i.pointsNumber; p++) s = Math.max(s, i.getResolvedPointSize(p));
    const r = n.simulationCollisionRadius ?? 0, d = n.simulationCollisionPadding ?? 0, h = (r > 0 ? r : s * 0.5) + d;`;
const perPointCollisionSizes = `    const collisionRadii = i._dtxCollisionRadii;
    if (collisionRadii && collisionRadii.length !== i.pointsNumber) throw new Error("Stale collision radius rows");
    let s = collisionRadii ? 0 : n.pointDefaultSize ?? E.pointDefaultSize;
    if (collisionRadii) {
      for (let p = 0; p < i.pointsNumber; p++) s = Math.max(s, collisionRadii[p] * 2);
    } else if (i.pointSizes && i.pointsNumber !== void 0) {
      for (let p = 0; p < i.pointsNumber; p++) s = Math.max(s, i.getResolvedPointSize(p));
    }
    const r = n.simulationCollisionRadius ?? 0, d = n.simulationCollisionPadding ?? 0, h = (r > 0 ? r : s * 0.5) + d;`;

/** Every change is reversible; the original force shaders/integration stay intact. */
export const COSMOS_COLLISION_RADII_PATCH_HUNKS = Object.freeze([
  { id: "collision-radii-api", before: pinnedPointsComment, after: collisionMethods + pinnedPointsComment },
  { id: "collision-grid-size", before: collisionSizes, after: perPointCollisionSizes },
  { id: "collision-size-texture", before: "      l[p * 4] = i.getResolvedPointSize(p);",
    after: "      l[p * 4] = collisionRadii ? collisionRadii[p] * 2 : i.getResolvedPointSize(p);" },
  { id: "collision-owned-cleanup", before: "    this._isDestroyed || (this._isDestroyed = !0,",
    after: "    this._isDestroyed || (this.graph._dtxCollisionRadii = null, this._isDestroyed = !0," },
].map(hunk => Object.freeze(hunk)));

export function patchCosmosCollisionRadii(source, version) {
  assert.equal(version, COSMOS_VERSION, "Review glyph collision units before upgrading Cosmos");
  let original = source;
  for (const { id, before, after } of [...COSMOS_GPU_SIMULATION_PATCH_HUNKS].reverse()) {
    assert.equal(original.split(after).length, 2, `Expected exactly one reviewed GPU ${id} seam`);
    original = original.replace(after, before);
  }
  assert.equal(sha256(original), COSMOS_SOURCE_SHA256, "Cosmos source differs from the reviewed package");
  let modified = source;
  for (const { id, before, after } of COSMOS_COLLISION_RADII_PATCH_HUNKS) {
    assert.equal(modified.split(before).length, 2, `Expected exactly one ${id} seam`);
    modified = modified.replace(before, after);
  }
  let reversed = modified;
  for (const { id, before, after } of [...COSMOS_COLLISION_RADII_PATCH_HUNKS].reverse()) {
    assert.equal(reversed.split(after).length, 2, `Expected exactly one patched ${id} seam`);
    reversed = reversed.replace(after, before);
  }
  assert.equal(reversed, source, "Unexpected extra glyph collision SDK change");
  return modified;
}

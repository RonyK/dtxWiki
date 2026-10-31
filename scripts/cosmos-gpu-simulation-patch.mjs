import assert from "node:assert/strict";

import {
  COSMOS_VERSION,
  COSMOS_SOURCE_SHA256,
  restLogTension,
  sha256,
} from "./cosmos-rest-log-patch.mjs";

const pinnedPointsComment = "  /**\n   * Sets which points are pinned (fixed) in position.";

const hostMethods = `  /** Optional shared-context GPU simulation. Native resources stay renderer-owned. */
  attachGpuSimulation(simulation) {
    if (simulation !== null && (!simulation ||
        ["step", "afterDrag", "invalidate", "destroy"].some(name => typeof simulation[name] !== "function"))) {
      throw new TypeError("GPU simulation must implement step, afterDrag, invalidate and destroy");
    }
    if (this._isDestroyed) {
      simulation?.destroy();
      return;
    }
    if ((this._gpuSimulation ?? null) === simulation) return;
    const previous = this._gpuSimulation;
    this._gpuSimulation = null;
    previous?.destroy();
    this._gpuSimulation = simulation;
    this._gpuSimulationFailed = false;
    simulation?.invalidate();
    this.requestRender();
  }
  getGpuSimulationFrame() {
    if (this._isDestroyed || !this.isReady) return null;
    const gl = this.device?.gl;
    const points = this.points;
    const textureSize = this.store.pointsTextureSize;
    const nodeCount = this.graph.pointsNumber;
    if (!gl || gl.isContextLost() || !points || !Number.isInteger(textureSize) || textureSize < 1 ||
        !Number.isInteger(nodeCount) || nodeCount < 1 || nodeCount > textureSize * textureSize) return null;
    const position = points.currentPositionTexture;
    const previous = points.previousPositionTexture;
    const framebuffer = points.currentPositionFbo;
    const pins = points.pinnedStatusTexture;
    if ([position, previous, framebuffer, pins].some(resource => !resource || resource.destroyed || !resource.handle) ||
        [position, previous, pins].some(texture => texture.width !== textureSize || texture.height !== textureSize)) return null;
    const draggingIndex = this.dragInstance?.isActive ? this.store.draggingPointIndex : null;
    return {
      gl,
      positionTexture: position.handle,
      previousPositionTexture: previous.handle,
      positionFramebuffer: framebuffer.handle,
      pinsTexture: pins.handle,
      textureSize: { width: textureSize, height: textureSize },
      nodeCount,
      pinnedIndices: (this.graph.inputPinnedPoints ?? []).slice(),
      draggingIndex: Number.isInteger(draggingIndex) && draggingIndex >= 0 && draggingIndex < nodeCount ? draggingIndex : null,
    };
  }
  _failGpuSimulation(error) {
    if (this._gpuSimulationFailed) return;
    this._gpuSimulationFailed = true;
    const simulation = this._gpuSimulation;
    this.pause();
    this.setConfigPartial({ enableSimulation: false });
    if (typeof simulation?.onError === "function") simulation.onError(error);
    else throw error;
  }
  _afterGpuSimulationDrag() {
    if (!this._gpuSimulation || this._gpuSimulationFailed) return;
    const frame = this.getGpuSimulationFrame();
    const index = this.store.hoveredPoint?.index ?? this.store.draggingPointIndex;
    if (frame && Number.isInteger(index) && index >= 0 && index < frame.nodeCount) {
      try { this._gpuSimulation.afterDrag(frame, index); }
      catch (error) { this._failGpuSimulation(error); }
    }
  }
`;

const simulationGate = "    if (!o) return;\n    this.isRightClickMouse && this.config.enableRightClickRepulsion &&";
const optionalSimulationGate = `    if (!o) return;
    if (this._gpuSimulation) {
      if (this._gpuSimulationFailed) {
        this.pause();
        this.setConfigPartial({ enableSimulation: false });
        if (!this._isDestroyed) this.points?.trackPoints();
        return;
      }
      const duringZoom = this.zoomInstance.shouldEnableSimulationDuringZoomOverride ?? this.config.enableSimulationDuringZoom;
      if (e || s && !(this.zoomInstance.isRunning && !duringZoom)) {
        this.points?.swapFbo();
        const frame = this.getGpuSimulationFrame();
        if (frame) {
          try { this._gpuSimulation.step(frame); }
          catch (error) {
            // Only the new current frame may be partially written. The
            // previous frame remains renderer-owned and restores exactly.
            this.points?.swapFbo();
            this._failGpuSimulation(error);
            if (!this._isDestroyed) this.points?.trackPoints();
            return;
          }
          this.points.isPositionsUpToDate = false;
          this.points.areClusterCentroidsUpToDate = false;
          this.markPickingBuffersStale();
          this.store.alpha += this.store.addAlpha(this.config.simulationDecay);
          if (this.isRightClickMouse && this.config.enableRightClickRepulsion) this.store.alpha = Math.max(this.store.alpha, 0.1);
          this.store.simulationProgress = Math.sqrt(Math.min(1, ne / this.store.alpha));
          this.config.onSimulationTick?.(this.store.alpha, this.store.hoveredPoint?.index, this.store.hoveredPoint?.position);
        }
      }
      this.points?.trackPoints();
      return;
    }
    this.isRightClickMouse && this.config.enableRightClickRepulsion &&`;

const updateMethod = `  update(e = this.store.alpha) {
    const { graph: t } = this;
    this.store.pointsTextureSize = Math.ceil(Math.sqrt(t.pointsNumber ?? 0)), this.store.linksTextureSize = Math.ceil(Math.sqrt((t.linksNumber ?? 0) * 2)), this.create(), this.initPrograms(), this.store.alpha = e;
  }`;
const optionalUpdateMethod = `  update(e = this.store.alpha) {
    const { graph: t } = this;
    const positionsPending = this.isPointPositionsUpdateNeeded;
    this.store.pointsTextureSize = Math.ceil(Math.sqrt(t.pointsNumber ?? 0)), this.store.linksTextureSize = Math.ceil(Math.sqrt((t.linksNumber ?? 0) * 2)), this.create(), positionsPending && this._gpuSimulation?.invalidate(), this.initPrograms(), this.store.alpha = e;
  }`;

const dragCalls = "(C = this.points) == null || C.drag(), (g = this.points) == null || g.trackPoints()";
const destroyStart = "    var e, t, i, n, o, s, r, d, h, u, l, c, f, p, v;\n    this._isDestroyed || (this._isDestroyed = !0,";
const optionalDestroyStart = `    var e, t, i, n, o, s, r, d, h, u, l, c, f, p, v;
    if (!this._isDestroyed) {
      const simulation = this._gpuSimulation;
      this._gpuSimulation = null;
      try { simulation?.destroy(); }
      catch (error) { console.error("GPU simulation cleanup failed", error); }
    }
    this._isDestroyed || (this._isDestroyed = !0,`;

/** Public so reversal/provenance tests can account for every changed SDK byte. */
export const COSMOS_GPU_SIMULATION_PATCH_HUNKS = Object.freeze([
  { id: "host-api", before: pinnedPointsComment, after: hostMethods + pinnedPointsComment },
  { id: "optional-simulation", before: simulationGate, after: optionalSimulationGate },
  { id: "uploaded-positions", before: updateMethod, after: optionalUpdateMethod },
  { id: "native-drag-sync", before: dragCalls, after: "(C = this.points) == null || C.drag(), this._afterGpuSimulationDrag(), (g = this.points) == null || g.trackPoints()" },
  { id: "owned-cleanup", before: destroyStart, after: optionalDestroyStart },
].map(hunk => Object.freeze(hunk)));

/**
 * Accept only the reviewed original bytes, optionally composed after the exact
 * maintained rest-log patch. No installed SDK or generated file is modified.
 */
export function patchCosmosGpuSimulation(source, version) {
  assert.equal(version, COSMOS_VERSION, "Review the GPU simulation seam before upgrading Cosmos");
  const hasRestLog = source.includes(restLogTension);
  if (hasRestLog) assert.equal(source.split(restLogTension).length, 2, "Expected exactly one rest-log shader");
  const original = hasRestLog ? source.replace(restLogTension, "l = (l - randomMinLinkDist) / l;") : source;
  assert.equal(sha256(original), COSMOS_SOURCE_SHA256, "Cosmos source differs from the reviewed package");
  let modified = source;
  for (const { id, before, after } of COSMOS_GPU_SIMULATION_PATCH_HUNKS) {
    assert.equal(modified.split(before).length, 2, `Expected exactly one ${id} seam`);
    modified = modified.replace(before, after);
  }
  let reversed = modified;
  for (const { id, before, after } of [...COSMOS_GPU_SIMULATION_PATCH_HUNKS].reverse()) {
    assert.equal(reversed.split(after).length, 2, `Expected exactly one patched ${id} seam`);
    reversed = reversed.replace(after, before);
  }
  assert.equal(reversed, source, "Unexpected extra SDK change");
  return modified;
}

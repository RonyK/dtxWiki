import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import {
  COSMOS_GPU_SIMULATION_PATCH_HUNKS,
  patchCosmosGpuSimulation,
} from "./cosmos-gpu-simulation-patch.mjs";
import {
  COSMOS_VERSION,
  COSMOS_SOURCE_SHA256,
  patchCosmosRestLog,
  sha256,
} from "./cosmos-rest-log-patch.mjs";

const require = createRequire(import.meta.url);
const original = await readFile(require.resolve("@cosmos.gl/graph"), "utf8");
const modified = patchCosmosGpuSimulation(original, COSMOS_VERSION);

function reverse(source) {
  for (const { before, after } of [...COSMOS_GPU_SIMULATION_PATCH_HUNKS].reverse()) {
    assert.equal(source.split(after).length, 2);
    source = source.replace(after, before);
  }
  return source;
}

function between(source, start, end) {
  const first = source.indexOf(start);
  assert.notEqual(first, -1, start);
  const last = source.indexOf(end, first + start.length);
  assert.notEqual(last, -1, end);
  return source.slice(first, last);
}

function buildHost(source) {
  const hostHunk = COSMOS_GPU_SIMULATION_PATCH_HUNKS.find(hunk => hunk.id === "host-api");
  const hostMethods = source.includes("  attachGpuSimulation(simulation) {")
    ? hostHunk.after.slice(0, -hostHunk.before.length) : "";
  const simulationMethod = between(source, "  runSimulationStep(e = !1) {", "\n  initPrograms() {");
  const updateMethod = between(source, "  update(e = this.store.alpha) {", "\n  /**\n   * Runs one step of the simulation");
  const renderFrameMethod = between(source, "  renderFrame(e) {", "\n  stopFrames() {");
  const destroyBody = COSMOS_GPU_SIMULATION_PATCH_HUNKS.find(hunk => hunk.id === "owned-cleanup");
  const destroyNeedle = source.includes(destroyBody.after) ? destroyBody.after : destroyBody.before;
  const destroyOffset = source.indexOf(destroyNeedle);
  const destroyStart = source.lastIndexOf("  destroy() {", destroyOffset);
  const destroyEnd = source.indexOf("\n  /**\n   * Uploads the processed data", destroyOffset);
  assert.ok(destroyStart >= 0 && destroyEnd > destroyStart);
  const destroyMethod = source.slice(destroyStart, destroyEnd);
  // Execute only the actual patched methods, with inert DOM/luma dependencies.
  // No WebGL context is created and no real GPU operation runs in these tests.
  return new Function("ne", "w", "window", "document", "O", "console", `return class Host {
    constructor(data) { Object.assign(this, data); }
    ${hostMethods}
    ${simulationMethod}
    ${updateMethod}
    ${renderFrameMethod}
    ${destroyMethod}
  }`)(0.001, {}, { clearTimeout() {} }, {}, () => ({ on() { return this; } }), {
    error(...args) { errors.push(args); },
  });
}

const errors = [];
const Host = buildHost(modified);
const StockHost = buildHost(original);

function fixture(HostType = Host) {
  const calls = [];
  const gl = { lost: false, isContextLost() { return this.lost; } };
  const resource = handle => ({ handle, width: 2, height: 2, destroyed: false });
  const points = {
    currentPositionTexture: resource("position-a"), previousPositionTexture: resource("position-b"),
    currentPositionFbo: resource("framebuffer-a"), previousPositionFbo: resource("framebuffer-b"),
    pinnedStatusTexture: resource("pins-a"),
    isPositionsUpToDate: true, areClusterCentroidsUpToDate: true,
    swapFbo() {
      if ([this.currentPositionTexture, this.previousPositionTexture, this.currentPositionFbo,
        this.previousPositionFbo].some(value => !value || value.destroyed)) return;
      [this.currentPositionTexture, this.previousPositionTexture] = [this.previousPositionTexture, this.currentPositionTexture];
      [this.currentPositionFbo, this.previousPositionFbo] = [this.previousPositionFbo, this.currentPositionFbo];
      this.areClusterCentroidsUpToDate = false;
      calls.push("swap");
    },
    updatePosition() { this.isPositionsUpToDate = false; calls.push("native-integrate"); },
    trackPoints() { calls.push("track"); },
    setTransitionProgress() {},
    draw() { calls.push("draw"); },
    drag() { calls.push("native-drag"); this.isPositionsUpToDate = false; },
    destroy() { calls.push("native-points-destroy"); },
  };
  const force = name => ({
    run() { calls.push(name); }, create() { calls.push(`${name}-create`); },
    initPrograms() { calls.push(`${name}-init`); }, destroy() { calls.push(`${name}-destroy`); },
  });
  const host = new HostType({
    _isDestroyed: false, isReady: true,
    device: { gl, handle: "not-the-instrumented-gl", beginRenderPass() { return { end() { calls.push("draw-end"); } }; }, submit() { calls.push("submit"); } },
    shouldDestroyDevice: false,
    points, graph: { pointsNumber: 3, linksNumber: 2, inputPinnedPoints: [0, 2] },
    store: {
      pointsTextureSize: 2, linksTextureSize: 2, isSimulationRunning: true,
      alpha: 1, simulationProgress: 0, hoveredPoint: { index: 1, position: [4, 5] },
      draggingPointIndex: 1, addAlpha() { return -0.25; },
    },
    config: {
      enableSimulation: true, enableSimulationDuringZoom: false,
      simulationGravity: 0.08, simulationCenter: 0.1, simulationCollision: true,
      simulationDecay: 100, enableRightClickRepulsion: true, renderLinks: true,
      onSimulationTick(...args) { calls.push(["tick", ...args]); },
    },
    forceGravity: force("gravity"), forceCenter: force("center"), forceManyBody: force("many-body"),
    forceLinkIncoming: force("link-in"), forceLinkOutgoing: force("link-out"),
    forceCollision: force("collision"), forceMouse: force("mouse"),
    isForceCollisionReady: false, isRightClickMouse: false,
    zoomInstance: { isRunning: false }, dragInstance: { isActive: false },
    transition: { isActive: false, isActiveFor() { return false; }, abort() { calls.push("abort-transition"); } },
    resizeObserver: { disconnect() {} },
    lines: { draw() { calls.push("draw-links"); }, setTransitionProgress() {}, destroy() { calls.push("native-lines-destroy"); } },
    requestRender() { calls.push("request-render"); },
    pause() { this.store.isSimulationRunning = false; calls.push("pause"); },
    setConfigPartial(config) {
      Object.assign(this.config, config);
      if (config.enableSimulation === false) {
        this.store.isSimulationRunning = false;
        this.store.alpha = 0;
        this.store.simulationProgress = 0;
      }
      calls.push(["config", config]);
    },
    markPickingBuffersStale() { calls.push("picking-stale"); },
    create() {
      calls.push("create");
      if (this.isPointPositionsUpdateNeeded) calls.push("upload-positions");
      this.isPointPositionsUpdateNeeded = false;
    },
    initPrograms() { calls.push("init-programs"); },
    resolvePendingPick() {}, findHoveredItem() {}, cancelLongPress() {},
    stopFrames() { calls.push("stop-frames"); },
  });
  return { host, calls, gl, points };
}

function attach(host, calls) {
  const frames = [];
  const simulation = {
    step(frame) { frames.push(frame); calls.push("external-step"); },
    afterDrag(frame, index) { frames.push(frame); calls.push(["external-drag", index]); },
    invalidate() { calls.push("invalidate"); }, destroy() { calls.push("external-destroy"); },
  };
  host.attachGpuSimulation(simulation);
  calls.length = 0;
  return { simulation, frames };
}

test("all optional SDK changes reverse exactly to the pinned original", () => {
  assert.equal(sha256(original), COSMOS_SOURCE_SHA256);
  assert.equal(reverse(modified), original);
  assert.equal(COSMOS_GPU_SIMULATION_PATCH_HUNKS.length, 5);
});

test("composition after the exact rest-log extension remains independently reversible", () => {
  const restLog = patchCosmosRestLog(original, COSMOS_VERSION);
  assert.equal(reverse(patchCosmosGpuSimulation(restLog, COSMOS_VERSION)), restLog);
});

test("version drift, source drift and double application fail closed", () => {
  assert.throws(() => patchCosmosGpuSimulation(original, "3.5.0"), /Review the GPU/);
  for (const changed of [`${original}\n`, modified, original.replace("  setPinnedPoints(e) {", "  setPinnedPoints(x) {")]) {
    assert.throws(() => patchCosmosGpuSimulation(changed, COSMOS_VERSION), /source differs/);
  }
});

test("without attachment the actual stock simulation method is unchanged in behavior", () => {
  for (const scenario of ["running", "paused", "manual", "zoom", "right-click", "disabled"]) {
    const current = fixture(), stock = fixture(StockHost);
    for (const { host } of [current, stock]) {
      if (scenario === "paused" || scenario === "manual") host.store.isSimulationRunning = false;
      if (scenario === "zoom") host.zoomInstance.isRunning = true;
      if (scenario === "right-click") host.isRightClickMouse = true;
      if (scenario === "disabled") host.config.enableSimulation = false;
      host.runSimulationStep(scenario === "manual");
    }
    assert.deepEqual(current.calls, stock.calls, scenario);
    assert.equal(current.host.store.alpha, stock.host.store.alpha, scenario);
    assert.equal(current.host.store.simulationProgress, stock.host.store.simulationProgress, scenario);
  }
});

test("attachment exposes dynamic borrowed native handles, pin metadata and no GL readback", () => {
  const { host, gl, points } = fixture();
  const frame = host.getGpuSimulationFrame();
  assert.equal(frame.gl, gl);
  assert.deepEqual(frame.textureSize, { width: 2, height: 2 });
  assert.equal(frame.nodeCount, 3);
  assert.equal(frame.positionTexture, "position-a");
  assert.equal(frame.previousPositionTexture, "position-b");
  assert.equal(frame.positionFramebuffer, "framebuffer-a");
  assert.equal(frame.pinsTexture, "pins-a");
  assert.deepEqual(frame.pinnedIndices, [0, 2]);
  frame.pinnedIndices.push(1);
  assert.deepEqual(host.graph.inputPinnedPoints, [0, 2]);
  points.pinnedStatusTexture = { ...points.pinnedStatusTexture, handle: "pins-reallocated" };
  points.swapFbo();
  const next = host.getGpuSimulationFrame();
  assert.equal(next.positionTexture, "position-b");
  assert.equal(next.previousPositionTexture, "position-a");
  assert.equal(next.positionFramebuffer, "framebuffer-b");
  assert.equal(next.pinsTexture, "pins-reallocated");
});

test("frame refuses unavailable, destroyed, lost, inconsistent and empty resources", () => {
  const mutations = [
    f => { f.host.isReady = false; }, f => { f.host._isDestroyed = true; },
    f => { f.gl.lost = true; }, f => { f.points.currentPositionFbo = undefined; },
    f => { f.points.currentPositionTexture.destroyed = true; },
    f => { f.points.previousPositionTexture.handle = null; },
    f => { f.points.pinnedStatusTexture.width = 4; },
    f => { f.host.store.pointsTextureSize = 0; },
    f => { f.host.graph.pointsNumber = 0; }, f => { f.host.graph.pointsNumber = 5; },
  ];
  for (const mutate of mutations) {
    const f = fixture(); mutate(f); assert.equal(f.host.getGpuSimulationFrame(), null);
  }
});

test("active native drag contributes exactly its valid row index", () => {
  const { host } = fixture();
  assert.equal(host.getGpuSimulationFrame().draggingIndex, null);
  host.dragInstance.isActive = true;
  assert.equal(host.getGpuSimulationFrame().draggingIndex, 1);
  for (const index of [undefined, -1, 3, 0.5, NaN]) {
    host.store.draggingPointIndex = index;
    assert.equal(host.getGpuSimulationFrame().draggingIndex, null);
  }
});

test("external step swaps before the call and preserves tick metadata, tracking and invalidation", () => {
  const { host, calls, points } = fixture();
  const { frames } = attach(host, calls);
  host.runSimulationStep();
  assert.equal(frames.length, 1);
  assert.equal(frames[0].previousPositionTexture, "position-a");
  assert.equal(frames[0].positionTexture, "position-b");
  assert.equal(frames[0].positionFramebuffer, "framebuffer-b");
  assert.deepEqual(calls, ["swap", "external-step", "picking-stale", ["tick", 0.75, 1, [4, 5]], "track"]);
  assert.equal(host.store.alpha, 0.75);
  assert.equal(host.store.simulationProgress, Math.sqrt(0.001 / 0.75));
  assert.equal(points.isPositionsUpToDate, false);
  assert.equal(points.areClusterCentroidsUpToDate, false);
});

test("external path never composes the native mouse, cluster, collision or other forces", () => {
  const { host, calls } = fixture();
  attach(host, calls);
  host.isRightClickMouse = true;
  host.graph.pointClusters = new Float32Array([0, 1, 2]);
  host.clusters = { run() { throw new Error("native cluster force ran"); } };
  host.runSimulationStep();
  assert.equal(calls.includes("external-step"), true);
  for (const force of ["mouse", "gravity", "center", "many-body", "link-in", "link-out", "collision", "native-integrate"]) {
    assert.equal(calls.includes(force), false, force);
  }
});

test("SDK disabled and paused gates remain effective; manual stepping still works", () => {
  const { host, calls } = fixture();
  attach(host, calls);
  host.config.enableSimulation = false;
  host.runSimulationStep(true);
  assert.deepEqual(calls, []);
  host.config.enableSimulation = true;
  host.store.isSimulationRunning = false;
  host.runSimulationStep();
  assert.deepEqual(calls, ["track"]);
  calls.length = 0;
  host.runSimulationStep(true);
  assert.equal(calls.includes("external-step"), true);
});

test("zoom override and manual-step precedence match native gating", () => {
  for (const [configured, override, manual, expected] of [
    [false, undefined, false, false], [true, false, false, false],
    [false, true, false, true], [true, undefined, false, true],
    [false, false, true, true],
  ]) {
    const { host, calls } = fixture(); attach(host, calls);
    host.zoomInstance.isRunning = true;
    host.config.enableSimulationDuringZoom = configured;
    host.zoomInstance.shouldEnableSimulationDuringZoomOverride = override;
    host.runSimulationStep(manual);
    assert.equal(calls.includes("external-step"), expected);
  }
});

test("missing native frame skips physics without falling back or cooling", () => {
  const { host, calls } = fixture(); attach(host, calls);
  host.points.pinnedStatusTexture = undefined;
  host.runSimulationStep();
  assert.equal(calls.includes("external-step"), false);
  assert.equal(calls.includes("native-integrate"), false);
  assert.equal(host.store.alpha, 1);
  assert.equal(calls.at(-1), "track");
});

test("same-sized position upload invalidates after create, but style-only render does not", () => {
  const { host, calls } = fixture(); attach(host, calls);
  host.isPointPositionsUpdateNeeded = true;
  host.update(0.4);
  assert.deepEqual(calls, ["create", "upload-positions", "invalidate", "init-programs"]);
  assert.equal(host.store.alpha, 0.4);
  calls.length = 0;
  host.isPointColorUpdateNeeded = true;
  host.isLinkColorUpdateNeeded = true;
  host.isLinkStyleUpdateNeeded = true;
  host.update();
  assert.deepEqual(calls, ["create", "init-programs"]);
});

test("new geometry recalculates native texture metadata before notifying driver", () => {
  const { host, calls } = fixture(); attach(host, calls);
  host.graph.pointsNumber = 5;
  host.graph.linksNumber = 3;
  host.isPointPositionsUpdateNeeded = true;
  host.create = function () {
    assert.equal(this.store.pointsTextureSize, 3);
    assert.equal(this.store.linksTextureSize, 3);
    for (const field of ["currentPositionTexture", "previousPositionTexture", "pinnedStatusTexture"]) {
      this.points[field] = { handle: field, width: 3, height: 3, destroyed: false };
    }
    this.points.currentPositionFbo = { handle: "new-framebuffer", destroyed: false };
    this.isPointPositionsUpdateNeeded = false;
    calls.push("upload-geometry");
  };
  host._gpuSimulation.invalidate = function () {
    assert.equal(host.getGpuSimulationFrame().nodeCount, 5);
    calls.push("invalidate");
  };
  host.update();
  assert.deepEqual(calls, ["upload-geometry", "invalidate", "init-programs"]);
});

test("native drag sync runs after native GPU write with the exact new current handles", () => {
  const { host, calls } = fixture();
  const { frames } = attach(host, calls);
  host.config.enableSimulation = false;
  host.dragInstance.isActive = true;
  host.renderFrame(1);
  assert.deepEqual(calls, ["draw-links", "draw", "swap", "native-drag", ["external-drag", 1], "track", "picking-stale", "draw-end", "submit"]);
  assert.equal(frames.length, 1);
  assert.equal(frames[0].positionTexture, "position-b");
  assert.equal(frames[0].positionFramebuffer, "framebuffer-b");
  assert.equal(frames[0].draggingIndex, 1);
});

test("non-drag rendering does not invoke drag synchronization", () => {
  const { host, calls } = fixture(); attach(host, calls);
  host.config.enableSimulation = false;
  host.renderFrame(1);
  assert.equal(calls.some(value => Array.isArray(value) && value[0] === "external-drag"), false);
});

test("invalid drag indices or lost resources are not imported into the driver", () => {
  const { host, calls } = fixture(); attach(host, calls);
  for (const index of [-1, 3, 0.5, NaN]) {
    host.store.hoveredPoint.index = index;
    host._afterGpuSimulationDrag();
  }
  host.store.hoveredPoint = undefined;
  host.store.draggingPointIndex = 2;
  host._afterGpuSimulationDrag();
  assert.deepEqual(calls, [["external-drag", 2]]);
  calls.length = 0;
  host.device.gl.lost = true;
  host._afterGpuSimulationDrag();
  assert.deepEqual(calls, []);
});

test("same attachment is idempotent and replacement/detachment destroy once", () => {
  const { host, calls } = fixture();
  const { simulation } = attach(host, calls);
  host.attachGpuSimulation(simulation);
  assert.deepEqual(calls, []);
  const replacement = { ...simulation, destroy() { calls.push("replacement-destroy"); } };
  host.attachGpuSimulation(replacement);
  assert.deepEqual(calls, ["external-destroy", "invalidate", "request-render"]);
  calls.length = 0;
  host.attachGpuSimulation(null);
  host.attachGpuSimulation(null);
  assert.deepEqual(calls, ["replacement-destroy", "request-render"]);
  host.runSimulationStep();
  assert.equal(calls.includes("native-integrate"), true);
});

test("malformed attachment does not destroy the currently attached owner", () => {
  const { host, calls } = fixture(); attach(host, calls);
  assert.throws(() => host.attachGpuSimulation({ step() {} }), /must implement/);
  assert.deepEqual(calls, []);
  host.runSimulationStep();
  assert.equal(calls.includes("external-step"), true);
});

test("Graph destroys its attached simulation before native resources, exactly once", () => {
  const { host, calls } = fixture(); attach(host, calls);
  host.destroy();
  assert.equal(calls.filter(value => value === "external-destroy").length, 1);
  assert.ok(calls.indexOf("external-destroy") < calls.indexOf("native-points-destroy"));
  assert.equal(host.getGpuSimulationFrame(), null);
  const once = calls.slice();
  host.destroy();
  assert.deepEqual(calls, once);
});

test("driver cleanup failure is reported without preventing native cleanup", () => {
  const { host, calls } = fixture();
  const { simulation } = attach(host, calls);
  simulation.destroy = () => { throw new Error("driver cleanup failed"); };
  const before = errors.length;
  host.destroy();
  assert.equal(errors.length, before + 1);
  assert.equal(errors.at(-1)[0], "GPU simulation cleanup failed");
  assert.equal(calls.includes("native-points-destroy"), true);
  host.destroy();
  assert.equal(errors.length, before + 1);
});

test("attaching to an already destroyed host disposes only the new driver", () => {
  const { host, calls } = fixture();
  host._isDestroyed = true;
  host.attachGpuSimulation({
    step() {}, afterDrag() {}, invalidate() { throw new Error("destroyed host initialized simulation"); },
    destroy() { calls.push("new-driver-destroy"); },
  });
  assert.deepEqual(calls, ["new-driver-destroy"]);
  assert.equal(host._gpuSimulation, undefined);
  assert.equal(host.getGpuSimulationFrame(), null);
});

test("step failure rolls back to the native pre-step frame and reports once without ticking or native fallback", () => {
  const { host, calls, points } = fixture();
  const { simulation } = attach(host, calls);
  const error = new Error("GPU step failed");
  let notifications = 0, attempts = 0;
  simulation.step = frame => {
    attempts++;
    assert.equal(frame.previousPositionTexture, "position-a");
    assert.equal(frame.positionTexture, "position-b");
    calls.push("failed-step");
    throw error;
  };
  simulation.onError = caught => { assert.equal(caught, error); notifications++; calls.push("error"); };
  host.runSimulationStep();
  assert.deepEqual(calls, ["swap", "failed-step", "swap", "pause", ["config", { enableSimulation: false }], "error", "track"]);
  assert.equal(points.currentPositionTexture.handle, "position-a");
  assert.equal(points.currentPositionFbo.handle, "framebuffer-a");
  assert.equal(host.config.enableSimulation, false);
  assert.equal(host.store.isSimulationRunning, false);
  assert.equal(host._gpuSimulationFailed, true);
  assert.equal(notifications, 1);
  host.runSimulationStep(true);
  assert.equal(attempts, 1);
  assert.equal(notifications, 1);
});

test("standalone step failure still throws its diagnostic error after rollback and disable", () => {
  const { host, calls, points } = fixture();
  const { simulation } = attach(host, calls);
  const error = new Error("standalone GPU failure");
  simulation.step = () => { throw error; };
  assert.throws(() => host.runSimulationStep(), caught => caught === error);
  assert.equal(points.currentPositionTexture.handle, "position-a");
  assert.equal(host.config.enableSimulation, false);
  assert.equal(host._gpuSimulationFailed, true);
  assert.equal(calls.includes("native-integrate"), false);
  assert.equal(calls.some(value => Array.isArray(value) && value[0] === "tick"), false);
});

test("failed attachment cannot retry through manual execution or an enable/resume request", () => {
  const { host, calls } = fixture();
  const { simulation } = attach(host, calls);
  let attempts = 0, notifications = 0;
  simulation.step = () => { attempts++; throw new Error("latched failure"); };
  simulation.onError = () => { notifications++; };
  host.runSimulationStep();
  calls.length = 0;
  host.config.enableSimulation = true;
  host.store.isSimulationRunning = true;
  host.runSimulationStep(true);
  assert.equal(attempts, 1);
  assert.equal(notifications, 1);
  assert.equal(host.config.enableSimulation, false);
  assert.equal(host.store.isSimulationRunning, false);
  assert.equal(calls.includes("native-integrate"), false);
});

test("after-drag failure preserves the native drag result without a rollback and reports once", () => {
  const { host, calls, points } = fixture();
  const { simulation } = attach(host, calls);
  const error = new Error("GPU drag import failed");
  let attempts = 0, notifications = 0;
  simulation.afterDrag = frame => {
    attempts++;
    assert.equal(frame.positionTexture, "position-b");
    assert.equal(frame.positionFramebuffer, "framebuffer-b");
    throw error;
  };
  simulation.onError = caught => { assert.equal(caught, error); notifications++; calls.push("error"); };
  host.config.enableSimulation = false;
  host.dragInstance.isActive = true;
  host.renderFrame(1);
  assert.equal(calls.filter(value => value === "swap").length, 1);
  assert.equal(points.currentPositionTexture.handle, "position-b");
  assert.equal(points.currentPositionFbo.handle, "framebuffer-b");
  assert.equal(host._gpuSimulationFailed, true);
  assert.equal(notifications, 1);
  host._afterGpuSimulationDrag();
  assert.equal(attempts, 1);
  assert.equal(notifications, 1);
});

test("standalone after-drag failure throws without rolling back valid native positions", () => {
  const { host, calls, points } = fixture();
  const { simulation } = attach(host, calls);
  const error = new Error("standalone drag failure");
  simulation.afterDrag = () => { throw error; };
  points.swapFbo();
  assert.throws(() => host._afterGpuSimulationDrag(), caught => caught === error);
  assert.equal(calls.filter(value => value === "swap").length, 1);
  assert.equal(points.currentPositionTexture.handle, "position-b");
  assert.equal(host.config.enableSimulation, false);
});

test("failure latch clears only when explicitly replacing the attached simulation", () => {
  const { host, calls } = fixture();
  const { simulation } = attach(host, calls);
  simulation.step = () => { throw new Error("failure before replacement"); };
  simulation.onError = () => {};
  host.runSimulationStep();
  host.attachGpuSimulation(simulation);
  assert.equal(host._gpuSimulationFailed, true);
  host.update();
  assert.equal(host._gpuSimulationFailed, true);
  const replacement = { ...simulation, step() { calls.push("replacement-step"); } };
  host.attachGpuSimulation(replacement);
  assert.equal(host._gpuSimulationFailed, false);
  host.config.enableSimulation = true;
  host.store.isSimulationRunning = true;
  host.runSimulationStep();
  assert.equal(calls.includes("replacement-step"), true);
});

test("a synchronous failure observer that destroys the host gets no later tracking or tick", () => {
  const { host, calls } = fixture();
  const { simulation } = attach(host, calls);
  simulation.step = () => { throw new Error("owner disposed on failure"); };
  simulation.onError = () => { host.destroy(); };
  host.runSimulationStep();
  assert.equal(calls.includes("native-points-destroy"), true);
  assert.equal(calls.includes("track"), false);
  assert.equal(calls.some(value => Array.isArray(value) && value[0] === "tick"), false);
});

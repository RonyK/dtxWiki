import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { COSMOS_POINT_PICKER_PATCH_HUNKS, patchCosmosPointPicker } from "./cosmos-point-picker-patch.mjs";
import { patchCosmosGpuSimulation, COSMOS_GPU_SIMULATION_PATCH_HUNKS } from "./cosmos-gpu-simulation-patch.mjs";
import { patchCosmosCollisionRadii, COSMOS_COLLISION_RADII_PATCH_HUNKS } from "./cosmos-collision-radii-patch.mjs";
import { COSMOS_VERSION, COSMOS_SOURCE_SHA256, sha256 } from "./cosmos-rest-log-patch.mjs";

const require = createRequire(import.meta.url);
const original = await readFile(require.resolve("@cosmos.gl/graph"), "utf8");
const native = patchCosmosCollisionRadii(patchCosmosGpuSimulation(original, COSMOS_VERSION), COSMOS_VERSION);
const modified = patchCosmosPointPicker(native, COSMOS_VERSION);
const hunk = id => COSMOS_POINT_PICKER_PATCH_HUNKS.find(value => value.id === id);

function between(source, start, end) {
  const first = source.indexOf(start);
  assert.notEqual(first, -1, start);
  const last = source.indexOf(end, first + start.length);
  assert.notEqual(last, -1, end);
  return source.slice(first, last);
}

function pointsClass(source) {
  const pointsSource = source.slice(source.indexOf("class Ni extends V {"));
  const methods = [
    source === modified ? hunk("picker-borrowed-frame").after.slice(0, -hunk("picker-borrowed-frame").before.length) : "",
    between(pointsSource, "  get hasPendingPickReadback() {", "\n  updatePositions() {"),
    between(pointsSource, "  pickPointSync() {", "\n  /**"),
    between(pointsSource, "  requestPickPoint() {", "\n  /**"),
    between(pointsSource, "  discardPendingPick() {", "\n  /**"),
    between(pointsSource, "  takePickResult() {", "\n  trackPointsByIndices(e) {"),
    between(pointsSource, "  updatePositions() {", "\n  initPrograms() {"),
  ];
  return new Function("A", "B", "Re", "$e", `return class Points { ${methods.join("\n")} }`)(
    (device, fbo, ...coordinates) => { device.reads.push(coordinates); return new Float32Array([2, 4, 5, 1]); },
    8,
    data => ({ index: data[0], position: [data[1], data[2]] }),
    class Readback {
      constructor() { this.inFlight = false; }
      issue() { this.inFlight = true; return true; }
      poll() { this.inFlight = false; return new Float32Array([2, 4, 5, 1]); }
      cancel() { this.inFlight = false; }
    },
  );
}

const Points = pointsClass(modified), StockPoints = pointsClass(original);
const resource = handle => ({ handle, width: 2, height: 2, destroyed: false });

function pointsFixture(Type = Points) {
  const calls = [], value = new Type();
  Object.assign(value, {
    data: { pointsNumber: 3 },
    store: { pointsTextureSize: 2, transformationMatrix4x4: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
      screenSize: [800, 600], screenMousePosition: [250, 300], adjustedSpaceSize: 4096, maxPointSize: 40 },
    config: { pointSizeScale: 1.2, scalePointsOnZoom: true, pixelRatio: 2, pointOpacity: 1, enableSimulation: false },
    device: { gl: { lost: false, isContextLost() { return this.lost; } }, reads: [] },
    currentPositionTexture: resource("positions"),
    pointStatusTexture: resource("status"), exitTexture: resource("exit"),
    pickingFbo: resource("legacy-picking"),
    updatePointStatus() { calls.push("status"); this.pointStatusTexture = resource("lazy-status"); },
    updateExit() { calls.push("exit"); this.exitTexture = resource("lazy-exit"); },
    updatePickingBuffer() { calls.push("legacy-raster"); },
    getCursorPickingWindow() { return { x: 5, y: 6, centerX: 7, centerY: 8 }; },
  });
  return { value, calls };
}

function pickerFixture(calls = []) {
  return {
    hasPending: false, result: { index: 1, position: [23, 45] },
    pickSync(frame) { calls.push(["sync", frame]); return this.result; },
    request(frame) { calls.push(["request", frame]); if (this.hasPending) return false; this.hasPending = true; return true; },
    take(frame) { calls.push(["take", frame]); if (!this.hasPending) return undefined; this.hasPending = false; return this.result; },
    discard() { calls.push("discard"); this.hasPending = false; },
    destroy() { calls.push("destroy"); this.hasPending = false; },
  };
}

test("all optional picking hunks reverse through collision/GPU changes to the pinned SDK", () => {
  let reversed = modified;
  for (const hunks of [COSMOS_POINT_PICKER_PATCH_HUNKS, COSMOS_COLLISION_RADII_PATCH_HUNKS, COSMOS_GPU_SIMULATION_PATCH_HUNKS]) {
    for (const { before, after } of [...hunks].reverse()) {
      assert.equal(reversed.split(after).length, 2);
      reversed = reversed.replace(after, before);
    }
  }
  assert.equal(reversed, original);
  assert.equal(sha256(reversed), COSMOS_SOURCE_SHA256);
  assert.throws(() => patchCosmosPointPicker(modified, COSMOS_VERSION));
  assert.throws(() => patchCosmosPointPicker(`${native}\n`, COSMOS_VERSION), /source differs/);
  assert.throws(() => patchCosmosPointPicker(native, "next"), /Review point/);
  assert.throws(() => patchCosmosPointPicker(original, COSMOS_VERSION), /reviewed/);
});

test("unattached actual sync/async picking executes the same legacy paths and results", () => {
  for (const Type of [StockPoints, Points]) {
    const { value, calls } = pointsFixture(Type);
    assert.equal(value.hasPendingPickReadback, false);
    assert.deepEqual(value.pickPointSync(), { index: 2, position: [4, 5] });
    assert.equal(value.requestPickPoint(), true);
    assert.equal(value.hasPendingPickReadback, true);
    assert.equal(value.requestPickPoint(), false);
    assert.deepEqual(value.takePickResult(), { index: 2, position: [4, 5] });
    assert.equal(value.hasPendingPickReadback, false);
    assert.equal(value.takePickResult(), undefined);
    value.requestPickPoint(); value.discardPendingPick();
    assert.equal(value.hasPendingPickReadback, false);
    assert.equal(value.issuedPickingWindow, undefined);
    assert.deepEqual(calls, ["legacy-raster", "legacy-raster", "legacy-raster"]);
    assert.deepEqual(value.device.reads, [[5, 6, 8, 8]]);
  }
});

test("attached actual methods delegate every readback state without rasterizing legacy points", () => {
  const { value, calls } = pointsFixture();
  const providerCalls = [], picker = pickerFixture(providerCalls);
  value.data._dtxPointPicker = picker;
  assert.equal(value.hasPendingPickReadback, false);
  assert.equal(value.pickPointSync(), picker.result);
  assert.equal(value.requestPickPoint(), true);
  assert.equal(value.hasPendingPickReadback, true);
  assert.equal(value.requestPickPoint(), false);
  assert.equal(value.takePickResult(), picker.result);
  assert.equal(value.takePickResult(), undefined);
  picker.result = null;
  value.requestPickPoint();
  assert.equal(value.takePickResult(), null);
  value.pickingReadback = { inFlight: true, cancel() { this.inFlight = false; } };
  value.issuedPickingWindow = {};
  value.discardPendingPick();
  assert.equal(value.pickingReadback.inFlight, false);
  assert.equal(value.issuedPickingWindow, undefined);
  assert.equal(providerCalls.at(-1), "discard");
  assert.deepEqual(calls, []);
  assert.deepEqual(value.device.reads, []);
  assert.equal(providerCalls[0][1].positionTexture, "positions");
});

test("borrowed frame copies view/pointer and exposes exact live native handles without CPU position copies", () => {
  const { value, calls } = pointsFixture();
  value.pointStatusTexture = undefined; value.exitTexture = undefined;
  const frame = value._dtxPointPickFrame();
  assert.deepEqual(calls, ["status", "exit"]);
  assert.equal(frame.gl, value.device.gl);
  assert.deepEqual(frame.textureSize, { width: 2, height: 2 });
  assert.equal(frame.nodeCount, 3);
  assert.equal(frame.positionTexture, "positions");
  assert.equal(frame.pointStatusTexture, "lazy-status");
  assert.equal(frame.exitTexture, "lazy-exit");
  assert.deepEqual(frame.pointer, [250, 300]);
  assert.equal(frame.pointGreyoutOpacity, null);
  assert.equal(frame.pixelRatio, 2);
  frame.pointer[0] = 999; frame.screenSize[0] = 999; frame.transformationMatrix[0] = 999;
  assert.equal(value.store.screenMousePosition[0], 250);
  assert.equal(value.store.screenSize[0], 800);
  assert.equal(value.store.transformationMatrix4x4[0], 1);
  value.currentPositionTexture = resource("next-native-position");
  assert.equal(value._dtxPointPickFrame().positionTexture, "next-native-position");
  // Cosmos uses one shared all-present texel until a point becomes absent.
  value.exitTexture = { ...resource("all-present-exit"), width: 1, height: 1 };
  assert.equal(value._dtxPointPickFrame().exitTexture, "all-present-exit");
  assert.deepEqual(value.device.reads, []);
});

test("borrowed frame fails closed for context loss, invalid view/count and missing resources", () => {
  const mutations = [
    v => { v.device.gl = undefined; }, v => { v.device.gl.lost = true; },
    v => { v.data.pointsNumber = 0; }, v => { v.data.pointsNumber = 5; }, v => { v.data.pointsNumber = 1.5; },
    v => { v.store.pointsTextureSize = 0; }, v => { v.currentPositionTexture.handle = null; },
    v => { v.currentPositionTexture.destroyed = true; }, v => { v.pointStatusTexture.width = 4; },
    v => { v.exitTexture.height = 4; }, v => { v.store.transformationMatrix4x4[0] = NaN; },
    v => { v.exitTexture.width = 1; },
    v => { v.store.screenSize[0] = 0; }, v => { v.store.screenMousePosition[0] = Infinity; },
    v => { v.store.adjustedSpaceSize = 0; },
  ];
  for (const mutate of mutations) {
    const { value } = pointsFixture(); mutate(value);
    assert.equal(value._dtxPointPickFrame(), null);
  }
  const { value } = pointsFixture(), picker = pickerFixture();
  value.data._dtxPointPicker = picker; value.device.gl.lost = true;
  picker.pickSync = frame => { assert.equal(frame, null); return null; };
  assert.equal(value.pickPointSync(), null);
});

test("actual same-count data upload retires async work but physics integration source remains byte-identical", () => {
  const { value } = pointsFixture(), calls = [];
  value.data._dtxPointPicker = pickerFixture(calls);
  // Early return still retires the old data epoch, including equal node counts.
  assert.equal(value.updatePositions(), false);
  assert.deepEqual(calls, ["discard"]);
  assert.equal(between(modified, "  updatePosition() {", "\n  drag() {"), between(original, "  updatePosition() {", "\n  drag() {"));
});

const api = hunk("picker-host-api").after.slice(0, -hunk("picker-host-api").before.length);
const errors = [];
const destroyOffset = modified.indexOf(hunk("picker-owned-cleanup").after);
const destroyMethod = modified.slice(modified.lastIndexOf("  destroy() {", destroyOffset), modified.indexOf("\n  /**\n   * Uploads the processed data", destroyOffset));
const hoverMethods = [
  between(modified, "  processHoverResult(e, t) {", "\n  /** Applies a picked point"),
  between(modified, "  applyPickedPoint(e) {", "\n  /** Applies a picked link"),
  between(modified, "  applyPickedLink(e) {", "\n  updateCanvasCursor() {"),
].join("\n");
const Host = new Function("window", "document", "O", "console", `return class Host { ${api} ${destroyMethod} ${hoverMethods} }`)(
  { clearTimeout() {} }, {}, () => ({ on() { return this; } }), { error(...args) { errors.push(args); } },
);

function hostFixture(Type = Host) {
  const calls = [], value = new Type();
  Object.assign(value, {
    graph: { pointsNumber: 10 }, config: {}, store: {}, transition: { abort() {} }, _isDestroyed: false,
    requestRender() { calls.push("render"); }, cancelLongPress() {}, stopFrames() {}, updateCanvasCursor() {},
    points: { discardPendingPick() { calls.push("legacy-points-discard"); value.graph._dtxPointPicker?.discard(); }, destroy() { calls.push("points-destroy"); } },
    lines: { discardPendingPick() { calls.push("legacy-lines-discard"); }, destroy() { calls.push("lines-destroy"); } },
  });
  return { value, calls };
}

test("attachment retires legacy point/link work, replaces ownership once and null restores stock paths", () => {
  const { value, calls } = hostFixture(), a = pickerFixture(calls), b = pickerFixture(calls);
  value.attachPointPicker(a);
  assert.deepEqual(calls, ["legacy-points-discard", "legacy-lines-discard", "render"]);
  assert.equal(value._shouldForceHoverDetection, true);
  calls.length = 0;
  value.attachPointPicker(a);
  assert.deepEqual(calls, []);
  value.attachPointPicker(b);
  assert.deepEqual(calls, ["legacy-points-discard", "discard", "legacy-lines-discard", "destroy", "render"]);
  calls.length = 0;
  value.attachPointPicker(null);
  assert.equal(value.graph._dtxPointPicker, null);
  assert.deepEqual(calls, ["legacy-points-discard", "discard", "legacy-lines-discard", "destroy", "render"]);
  value.attachPointPicker(null);
  assert.equal(calls.length, 5);
});

test("new owner with the same point index emits old hover-out before its fresh hover-in", () => {
  const { value } = hostFixture(), callbacks = [], a = pickerFixture(), b = pickerFixture();
  value.config.onPointMouseOver = index => callbacks.push(["over", index]);
  value.config.onPointMouseOut = () => callbacks.push("out");
  a.result = b.result = { index: 0, position: [23, 45] };
  value.attachPointPicker(a);
  value.processHoverResult(a.pickSync(null), null);
  assert.deepEqual(callbacks, [["over", 0]]);
  value.attachPointPicker(a);
  assert.deepEqual(callbacks, [["over", 0]], "same owner must not retire hover");
  value.attachPointPicker(b);
  assert.equal(value.store.hoveredPoint, undefined);
  assert.deepEqual(callbacks, [["over", 0], "out"]);
  value.processHoverResult(b.pickSync(null), null);
  assert.deepEqual(callbacks, [["over", 0], "out", ["over", 0]]);
  value.attachPointPicker(null);
  value.attachPointPicker(null);
  assert.deepEqual(callbacks, [["over", 0], "out", ["over", 0], "out"]);
});

test("a synchronous hover-out teardown cannot attach an incoming picker to a destroyed Graph", () => {
  const { value } = hostFixture(), cleanup = [], a = pickerFixture(), b = pickerFixture(cleanup);
  value.attachPointPicker(a);
  value.processHoverResult({ index: 0, position: [23, 45] }, null);
  value.config.onPointMouseOut = () => value.destroy();
  value.attachPointPicker(b);
  assert.equal(value._isDestroyed, true);
  assert.equal(value.graph._dtxPointPicker, null);
  assert.deepEqual(cleanup, ["destroy"]);
});

test("invalid attachment is rejected before touching its owner; destroyed host destroys incoming provider", () => {
  const { value, calls } = hostFixture(), picker = pickerFixture();
  value.attachPointPicker(picker); calls.length = 0;
  for (const bad of [undefined, {}, { ...picker, request: undefined }, { ...picker, hasPending: undefined }]) {
    assert.throws(() => value.attachPointPicker(bad), /Point picker/);
    assert.equal(value.graph._dtxPointPicker, picker);
  }
  assert.deepEqual(calls, []);
  value._isDestroyed = true;
  value.attachPointPicker(pickerFixture(calls));
  assert.deepEqual(calls, ["destroy"]);
});

test("Graph destruction clears provider before native resources and destroys it exactly once", () => {
  const { value, calls } = hostFixture(), picker = pickerFixture(calls);
  value.attachPointPicker(picker); calls.length = 0;
  picker.destroy = () => { assert.equal(value.graph._dtxPointPicker, null); calls.push("destroy"); };
  value.destroy(); value.destroy();
  assert.equal(calls.filter(call => call === "destroy").length, 1);
  assert.ok(calls.indexOf("destroy") < calls.indexOf("points-destroy"));
  const broken = hostFixture();
  broken.value.attachPointPicker(pickerFixture());
  broken.value.graph._dtxPointPicker.destroy = () => { throw new Error("cleanup"); };
  assert.doesNotThrow(() => broken.value.destroy());
  assert.equal(errors.at(-1)[0], "Point picker cleanup failed");
});

test("tracked-frame getter refreshes only the GPU table and snapshots its actual baked slot order", () => {
  const { value } = hostFixture(), calls = [];
  value.device = { gl: { isContextLost() { return false; } } };
  Object.assign(value.points, {
    _dtxTrackedIndices: [9, 2, 1], trackedIndices: [999, 999, 999],
    trackedPositionsFbo: resource("tracked-fbo"),
    currentPositionTexture: resource("positions"), trackedIndicesTexture: resource("tracked-indices"),
    trackPointsCommand: {}, trackPoints() { calls.push("gpu-track"); },
    getTrackedPositionsMap() { throw new Error("synchronous CPU readback must not run"); },
  });
  const frame = value.getTrackedPointPositionsFrame();
  assert.deepEqual(frame, { gl: value.device.gl, framebuffer: "tracked-fbo", width: 2, height: 2, indices: [9, 2, 1] });
  assert.deepEqual(calls, ["gpu-track"]);
  frame.indices[0] = 100;
  assert.deepEqual(value.points._dtxTrackedIndices, [9, 2, 1]);
  for (const mutate of [
    v => { v._isDestroyed = true; }, v => { v.device.gl.isContextLost = () => true; },
    v => { v.points.trackedPositionsFbo.destroyed = true; }, v => { v.points.trackedPositionsFbo.handle = null; },
    v => { v.points._dtxTrackedIndices = []; }, v => { v.points._dtxTrackedIndices = [0, 1, 2, 3, 4]; },
    v => { v.points.trackPointsCommand = null; }, v => { v.points.currentPositionTexture.destroyed = true; },
    v => { v.points.trackedIndicesTexture.destroyed = true; },
    v => { v.points._dtxTrackedIndices = [0, 10, 1]; }, v => { v.points._dtxTrackedIndices = [0, -1, 1]; },
    v => { v.points.trackedIndicesTexture.width = 1; },
  ]) {
    const copy = Object.assign(new Host(), value, { _isDestroyed: false, device: { gl: { isContextLost() { return false; } } },
      points: { ...value.points, _dtxTrackedIndices: [9, 2, 1], trackedPositionsFbo: resource("tracked-fbo"),
        currentPositionTexture: resource("positions"), trackedIndicesTexture: resource("indices") } });
    mutate(copy); assert.equal(copy.getTrackedPointPositionsFrame(), null);
  }
  assert.equal(between(modified, "  getTrackedPointPositionsMap() {", "\n  /**"), between(original, "  getTrackedPointPositionsMap() {", "\n  /**"));
  const snapshot = hunk("tracked-index-snapshot");
  assert.equal(modified.split(snapshot.after).length, 2);
  assert.ok(snapshot.after.includes("Array.from(e ?? [])"));
  const uploadPrefix = between(modified, "  trackPointsByIndices(e) {", "\n    const i = Math.ceil(Math.sqrt(e.length))");
  const Upload = new Function(`return class Upload { ${uploadPrefix}\n  } }`)();
  const upload = new Upload();
  upload.device = {};
  const callerOwned = [9, 2, 1];
  upload.trackPointsByIndices(callerOwned);
  callerOwned.reverse();
  assert.deepEqual(upload._dtxTrackedIndices, [9, 2, 1]);
});

const InteractionHost = new Function("w", "Ct", "j", `return class Host {
  ${api}
  ${between(modified, "  findHoveredItem(e = !1) {", "\n  /** Marks both picking")}
  ${between(modified, "  onClick(e) {", "\n  updateMousePosition(e) {")}
}`)({ PointSizes: "sizes" }, 4, 2);

function interactionFixture(attached) {
  const calls = [], value = new InteractionHost();
  Object.assign(value, {
    graph: { linksNumber: 1, _dtxPointPicker: attached ? pickerFixture() : null },
    store: { isLinkHoveringEnabled: true, hoveredPoint: { index: 0, position: [0, 0] } },
    transition: { isActiveFor() { return false; } }, _isPointerOnCanvas: true,
    _findHoveredItemExecutionCount: 0, _lastMouseX: 1, _lastMouseY: 0, _lastCheckedMouseX: 0, _lastCheckedMouseY: 0,
    points: { requestPickPoint() { calls.push("request"); return true; }, discardPendingPick() { calls.push("discard-points"); }, pickPointSync() { calls.push("sync"); return null; } },
    lines: { requestPickLink() { calls.push("request-link"); return true; }, discardPendingPick() { calls.push("discard-links"); }, pickLinkSync() { calls.push("sync-link"); return 2; } },
    updatePickingBufferStaleness() {}, processHoverResult(point, link) { calls.push(["hover", point, link]); this.store.hoveredPoint = point; this.store.hoveredLinkIndex = link; },
    updateMousePosition(event) { calls.push(["pointer", event]); },
    config: { onClick(index, position, event) { calls.push(["click", index, position, event]); }, onLinkClick(index) { calls.push(["link-click", index]); } },
  });
  return { value, calls };
}

test("attached hover removes only native cadence/movement thresholds and preserves native link fallback", () => {
  const current = interactionFixture(true), stock = interactionFixture(false);
  current.value.findHoveredItem(); stock.value.findHoveredItem();
  assert.deepEqual(current.calls, ["request", "request-link"]);
  assert.deepEqual(stock.calls, []);
  assert.equal(stock.value._findHoveredItemExecutionCount, 1);
  current.calls.length = 0;
  current.value.findHoveredItem(true);
  assert.deepEqual(current.calls, ["discard-points", "discard-links", "sync", "sync-link", ["hover", null, 2]]);
});

test("attached click synchronously refreshes current coordinates after but never before suppression", () => {
  const { value, calls } = interactionFixture(true), event = { offsetX: 10, offsetY: 20 };
  value._shouldSuppressNextClick = true;
  value.onClick(event);
  assert.deepEqual(calls, []);
  assert.equal(value._shouldSuppressNextClick, false);
  value.onClick(event);
  assert.deepEqual(calls, [["pointer", event], "discard-points", "discard-links", "sync", "sync-link", ["hover", null, 2], ["click", undefined, undefined, event], ["link-click", 2]]);
  const stock = interactionFixture(false);
  stock.value.onClick(event);
  assert.deepEqual(stock.calls, [["click", 0, [0, 0], event]]);
});

test("scoped pointer enter/leave and context event hooks preserve default event ownership", () => {
  for (const id of ["picker-pointer-enter", "picker-pointer-leave"]) {
    const change = hunk(id);
    assert.equal(modified.split(change.after).length, 2);
    assert.ok(change.after.includes("this.graph._dtxPointPicker"));
  }
  assert.ok(hunk("picker-pointer-enter").after.includes("this.updateMousePosition(l)"));
  assert.ok(hunk("picker-pointer-leave").after.includes("this.graph._dtxPointPicker?.discard()"));
  const { value, calls } = hostFixture(), listeners = [];
  value.canvasD3Selection = { on(events, callback) { listeners.push([events, callback]); return this; } };
  value.attachPointPicker(null);
  assert.deepEqual(listeners, []);
  value.attachPointPicker(pickerFixture(calls));
  assert.equal(listeners[0][0], "webglcontextlost.cosmos webglcontextrestored.cosmos");
  listeners[0][1]();
  assert.equal(calls.at(-1), "discard");
  value.attachPointPicker(null);
  assert.equal(listeners.at(-1)[1], null);
});

function gestureClass(source) {
  const methods = hunk("picker-host-api").after.slice(0, -hunk("picker-host-api").before.length);
  return new Function("Host", "w", "Ct", "j", `return class GestureHost extends Host {
    ${methods}
    ${between(source, "  findHoveredItem(e = !1) {", "\n  /** Marks both picking")}
    ${between(source, "  onClick(e) {", "\n  updateMousePosition(e) {")}
    ${between(source, "  setPointPositions(e, t) {", "\n  /**")}
    ${between(source, "  fireContextMenu(e) {", "\n  /**")}
  }`)(Host, { PointSizes: "sizes", Positions: "positions" }, 4, 2);
}

const GestureHost = gestureClass(modified);
function nativePointerHandler(eventName, nextEvent) {
  const start = `.on("${eventName}", (l) => {`;
  const body = between(modified, start, `}).on("${nextEvent}"`).slice(start.length);
  return new Function("l", body);
}
const pointerHandlers = {
  down: nativePointerHandler("pointerdown.cosmos", "pointerup.cosmos"),
  move: nativePointerHandler("pointermove.cosmos", "pointerleave.cosmos pointercancel.cosmos"),
  up: nativePointerHandler("pointerup.cosmos", "click.cosmos"),
  leave: nativePointerHandler("pointerleave.cosmos pointercancel.cosmos", "pointerdown.cosmos"),
};
const pressEvent = (changes = {}) => ({ isPrimary: true, pointerId: 7, pointerType: "mouse", button: 0, buttons: 1,
  clientX: 300.75, clientY: 200.75, offsetX: 300.75, offsetY: 200.75, isTrusted: true, detail: 1, ...changes });
const clickEvent = (changes = {}) => pressEvent({ buttons: 0, clientX: 300, clientY: 200, offsetX: 301, offsetY: 201, ...changes });

function gestureFixture(Type = GestureHost) {
  const { value, calls } = hostFixture(Type), events = [], listeners = new Map();
  let point = { index: 0, position: [23.75, 45.75] }, link = null;
  Object.assign(value.graph, { linksNumber: 1, _dtxPointPickEpoch: 4 });
  Object.assign(value.store, { isLinkHoveringEnabled: true });
  Object.assign(value.transition, { isActiveFor() { return false; }, queue() {} });
  Object.assign(value, {
    _isPointerOnCanvas: true, _findHoveredItemExecutionCount: 0,
    _lastMouseX: 0, _lastMouseY: 0, _lastCheckedMouseX: 0, _lastCheckedMouseY: 0,
    ensureDevice() { return false; }, updatePickingBufferStaleness() {},
    updateMousePosition(event) { calls.push(["pointer", event]); },
    canvasD3Selection: { on(name, callback) { listeners.set(name, callback); return this; } },
  });
  Object.assign(value.points, {
    pickPointSync() { calls.push("sync-point"); return point; },
    requestPickPoint() { calls.push("request-point"); return true; },
    data: value.graph, store: value.store, config: value.config,
  });
  Object.assign(value.lines, {
    pickLinkSync() { calls.push("sync-link"); return link; },
    requestPickLink() { return true; },
  });
  Object.assign(value.config, {
    onClick(index, position) { events.push(["click", index, position]); },
    onPointClick(index, position) { events.push(["point", index, position]); },
    onLinkClick(index) { events.push(["link", index]); },
    onBackgroundClick() { events.push(["background"]); },
  });
  value.attachPointPicker(pickerFixture());
  calls.length = 0;
  return { value, calls, events, listeners,
    winner(next, edge = null) { point = next; link = edge; },
    down(event = pressEvent()) { pointerHandlers.down.call(value, event); },
    up(event = pressEvent({ buttons: 0 })) { pointerHandlers.up.call(value, event); },
  };
}

test("stationary fractional index-zero press survives rounded offsets and changed GPU winner without a second query", () => {
  const f = gestureFixture();
  f.down();
  const borrowed = f.value.store.hoveredPoint;
  assert.equal(f.value.graph._dtxPointPress.point.index, 0);
  // D3 starts a drag on mousedown even for a click with no pointer movement.
  f.value.dragInstance = { isActive: true };
  f.value.store.draggingPointIndex = 0;
  f.winner({ index: 1, position: [100, 200] });
  f.value.store.hoveredPoint = { index: 1, position: [100, 200] };
  borrowed.position[0] = 999; // The press owns a snapshot, not this mutable native row.
  f.up();
  f.calls.length = 0;
  f.value.onClick(clickEvent());
  assert.deepEqual(f.events, [["click", 0, [23.75, 45.75]], ["point", 0, [23.75, 45.75]]]);
  assert.equal(f.calls.includes("sync-point"), false);
  assert.equal(f.value.graph._dtxPointPress, undefined);
});

test("browser-compatible subpixel mouse clicks retain the precise pressed winner without a replacement query", () => {
  for (const delta of [.1, -.1]) {
    for (const event of [clickEvent(),
      clickEvent({ clientX: 300.75 + delta, clientY: 200.75 + delta }),
      clickEvent({ pointerId: undefined, pointerType: undefined })]) {
      const f = gestureFixture();
      f.down();
      pointerHandlers.move.call(f.value, pressEvent({ clientX: 300.75 + delta, clientY: 200.75 + delta }));
      f.up(pressEvent({ clientX: 300.75 + delta, clientY: 200.75 + delta, buttons: 0 }));
      f.winner({ index: 1, position: [100, 200] });
      f.value.store.hoveredPoint = { index: 1, position: [100, 200] };
      f.calls.length = 0;
      f.value.onClick(event);
      assert.deepEqual(f.events, [["click", 0, [23.75, 45.75]], ["point", 0, [23.75, 45.75]]]);
      assert.equal(f.calls.includes("sync-point"), false);
      assert.equal(f.value.graph._dtxPointPress, undefined);
    }
  }
});

test("stationary background and link-zero presses keep their original target rather than a point that appeared later", () => {
  for (const edge of [null, 0]) {
    const f = gestureFixture();
    f.winner(null, edge); f.down(); f.up();
    f.winner({ index: 1, position: [100, 200] });
    f.value.store.hoveredPoint = { index: 1, position: [100, 200] };
    f.calls.length = 0;
    f.value.onClick(clickEvent());
    assert.deepEqual(f.events, [["click", undefined, undefined], edge === null ? ["background"] : ["link", 0]]);
    assert.equal(f.calls.includes("sync-point"), false);
  }
});

test("actual movement, different pointer, mismatched release and unreleased presses suppress instead of querying a replacement target", () => {
  const mutations = [
    f => pointerHandlers.move.call(f.value, pressEvent({ clientX: 301 })),
    f => pointerHandlers.move.call(f.value, pressEvent({ pointerId: 8 })),
    f => f.up(pressEvent({ clientY: 201, buttons: 0 })),
    f => f.up(pressEvent({ button: 1, buttons: 0 })),
    () => {},
  ];
  for (const [index, mutate] of mutations.entries()) {
    const f = gestureFixture(); f.down(); mutate(f);
    if (index < 2) { assert.deepEqual(f.value.graph._dtxPointPress, { cancelled: true }); f.up(); }
    f.winner({ index: 1, position: [100, 200] }); f.calls.length = 0;
    f.value.onClick(clickEvent());
    assert.deepEqual(f.events, []);
    assert.equal(f.calls.includes("sync-point"), false);
    assert.equal(f.value.graph._dtxPointPress, undefined);
  }
});

test("crossing a mouse compatibility cell and returning cannot revive its cancelled pressed winner", () => {
  const f = gestureFixture(); f.down();
  pointerHandlers.move.call(f.value, pressEvent({ clientX: 301 }));
  pointerHandlers.move.call(f.value, pressEvent());
  f.up(); f.calls.length = 0;
  f.value.onClick(clickEvent());
  assert.deepEqual(f.events, []);
  assert.equal(f.calls.includes("sync-point"), false);
});

test("non-mouse pointer motion and pointer-type changes retain exact cancellation semantics", () => {
  for (const pointerType of ["pen", "touch"]) {
    const f = gestureFixture(); f.value._dtxPickPointerDown(pressEvent({ pointerType }));
    pointerHandlers.move.call(f.value, pressEvent({ pointerType, clientX: 300.75001 }));
    f.up(pressEvent({ pointerType, buttons: 0 })); f.calls.length = 0;
    f.value.onClick(clickEvent());
    assert.deepEqual(f.events, []);
    assert.equal(f.calls.includes("sync-point"), false);
  }
  const f = gestureFixture(); f.down();
  pointerHandlers.move.call(f.value, pressEvent({ pointerType: "pen" }));
  f.up(); f.calls.length = 0;
  f.value.onClick(clickEvent());
  assert.deepEqual(f.events, []);
});

test("pointerleave, pointercancel and context events erase press identity and retain only a cancellation tombstone", () => {
  for (const action of ["leave", "cancel", "context"]) {
    const f = gestureFixture(); f.down();
    if (action === "context") f.listeners.get("webglcontextlost.cosmos webglcontextrestored.cosmos")();
    else pointerHandlers.leave.call(f.value, pressEvent({ type: action === "leave" ? "pointerleave" : "pointercancel", buttons: 0 }));
    assert.deepEqual(f.value.graph._dtxPointPress, { cancelled: true });
    f.up(); f.calls.length = 0;
    f.value.onClick(clickEvent());
    assert.deepEqual(f.events, []);
    assert.equal(f.calls.includes("sync-point"), false);
  }
});

test("provider retirement and same-count pending/uploaded data changes cannot fresh-query an obsolete gesture", () => {
  for (const action of ["provider", "detach", "pending-data", "uploaded-data"]) {
    const f = gestureFixture(); f.down(); f.up();
    if (action === "provider") f.value.attachPointPicker(pickerFixture());
    else if (action === "detach") f.value.attachPointPicker(null);
    else if (action === "pending-data") f.value.setPointPositions(new Float32Array(20), false);
    else {
      f.value.store.pointsTextureSize = 4;
      assert.equal(Points.prototype.updatePositions.call(f.value.points), false);
    }
    assert.deepEqual(f.value.graph._dtxPointPress, { cancelled: true });
    if (action.endsWith("data")) assert.equal(f.value.graph._dtxPointPickEpoch, 5);
    f.calls.length = 0; f.value.onClick(clickEvent());
    assert.deepEqual(f.events, [], action);
    assert.equal(f.calls.includes("sync-point"), false, action);
  }
});

test("genuine owner/epoch/pointer/button/coordinate mismatch does not consume a different scene's target", () => {
  const mutations = [
    f => { f.value.graph._dtxPointPickEpoch += 1; },
    f => { f.value.graph._dtxPointPicker = pickerFixture(); },
    f => { f.value.graph._dtxPointPress.owner = pickerFixture(); },
    f => { f.value.graph._dtxPointPress.epoch -= 1; },
  ];
  for (const mutate of mutations) {
    const f = gestureFixture(); f.down(); f.up(); mutate(f); f.calls.length = 0;
    f.value.onClick(clickEvent());
    assert.deepEqual(f.events, []); assert.equal(f.calls.includes("sync-point"), false);
  }
  for (const changes of [{ pointerId: 8 }, { clientX: 301 }, { clientY: 199 }, { button: 1 }]) {
    const f = gestureFixture(); f.down(); f.up(); f.calls.length = 0;
    f.value.onClick(clickEvent(changes));
    assert.deepEqual(f.events, []); assert.equal(f.calls.includes("sync-point"), false);
  }
});

test("keyboard/programmatic clicks without a genuine released press query the current winner freshly", () => {
  for (const event of [clickEvent({ detail: 0 }), clickEvent({ isTrusted: false })]) {
    const f = gestureFixture(); f.down(); f.up();
    f.winner({ index: 1, position: [100, 200] }); f.calls.length = 0;
    f.value.onClick(event);
    assert.deepEqual(f.events, [["click", 1, [100, 200]], ["point", 1, [100, 200]]]);
    assert.equal(f.calls.filter(call => call === "sync-point").length, 1);
  }
  const f = gestureFixture(); f.winner({ index: 1, position: [100, 200] });
  f.value.onClick(clickEvent());
  assert.deepEqual(f.events, [["click", 1, [100, 200]], ["point", 1, [100, 200]]]);
});

test("fresh hover callback replacement during pointerdown tombstones the press before it can bind the new owner", () => {
  const f = gestureFixture();
  f.value.config.onPointMouseOver = () => { delete f.value.config.onPointMouseOver; f.value.attachPointPicker(pickerFixture()); };
  f.down();
  assert.deepEqual(f.value.graph._dtxPointPress, { cancelled: true });
  f.up(); f.calls.length = 0; f.value.onClick(clickEvent());
  assert.deepEqual(f.events, []); assert.equal(f.calls.includes("sync-point"), false);
});

test("hover retirement or generic-click callback reentrancy cannot dispatch a source callback into the replacement owner", () => {
  for (const phase of ["hover", "generic"]) {
    for (const action of ["replace", "epoch", "destroy"]) {
      const f = gestureFixture(); f.down(); f.up();
      const retire = () => {
        if (action === "replace") f.value.attachPointPicker(pickerFixture());
        else if (action === "epoch") f.value.setPointPositions(new Float32Array(20), false);
        else f.value.destroy();
      };
      if (phase === "hover") {
        f.value.store.hoveredPoint = { index: 1, position: [100, 200] };
        f.value.config.onPointMouseOver = () => { delete f.value.config.onPointMouseOver; retire(); };
      } else f.value.config.onClick = (index, position) => { f.events.push(["click", index, position]); retire(); };
      f.value.onClick(clickEvent());
      assert.deepEqual(f.events, phase === "hover" ? [] : [["click", 0, [23.75, 45.75]]], `${phase}:${action}`);
    }
  }
});

test("suppression, context-menu/long-press entry and destroy proactively retire the owned press", () => {
  const f = gestureFixture(); f.down(); f.up(); f.value._shouldSuppressNextClick = true;
  f.value.onClick(clickEvent());
  assert.equal(f.value.graph._dtxPointPress, undefined);
  assert.deepEqual(f.events, []);
  f.down();
  f.value.config.onContextMenu = () => assert.deepEqual(f.value.graph._dtxPointPress, { cancelled: true });
  f.value.fireContextMenu(pressEvent());
  assert.deepEqual(f.value.graph._dtxPointPress, { cancelled: true });
  f.down(); f.value.destroy();
  assert.equal(f.value.graph._dtxPointPress, undefined);
  assert.equal(f.value.graph._dtxPointPicker, null);
});

test("unattached genuine presses and clicks preserve the original native callback path", () => {
  const f = gestureFixture(); f.value.attachPointPicker(null); f.events.length = 0;
  f.winner({ index: 0, position: [23.75, 45.75] }); f.down(); f.up();
  assert.equal(f.value.graph._dtxPointPress, undefined);
  f.calls.length = 0; f.value.onClick(clickEvent());
  assert.deepEqual(f.events, [["click", 0, [23.75, 45.75]], ["point", 0, [23.75, 45.75]]]);
  assert.equal(f.calls.includes("sync-point"), false);
});

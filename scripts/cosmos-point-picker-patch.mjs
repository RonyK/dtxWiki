import assert from "node:assert/strict";
import { COSMOS_VERSION, COSMOS_SOURCE_SHA256, sha256 } from "./cosmos-rest-log-patch.mjs";
import { COSMOS_GPU_SIMULATION_PATCH_HUNKS } from "./cosmos-gpu-simulation-patch.mjs";
import { COSMOS_COLLISION_RADII_PATCH_HUNKS } from "./cosmos-collision-radii-patch.mjs";

const pinnedPointsComment = "  /**\n   * Sets which points are pinned (fixed) in position.";
const hostMethods = `  /** Optional point picking only; native rendering, forces and drag stay owned here. */
  attachPointPicker(picker) {
    if (picker !== null && (!picker ||
        ["pickSync", "request", "take", "discard", "destroy"].some(name => typeof picker[name] !== "function") ||
        typeof picker.hasPending !== "boolean")) {
      throw new TypeError("Point picker must implement its sync, async and ownership contract");
    }
    if (this._isDestroyed) { picker?.destroy(); return; }
    if ((this.graph._dtxPointPicker ?? null) === picker) return;
    this._dtxCancelPointPress();
    this.points?.discardPendingPick();
    this.lines?.discardPendingPick();
    const previous = this.graph._dtxPointPicker;
    this.graph._dtxPointPicker = null;
    try { previous?.destroy(); this.processHoverResult(null, null); }
    catch (error) { picker?.destroy(); throw error; }
    if (this._isDestroyed) { picker?.destroy(); return; }
    this.graph._dtxPointPicker = picker;
    this.canvasD3Selection?.on("webglcontextlost.cosmos webglcontextrestored.cosmos",
      picker ? () => { this._dtxCancelPointPress(); this.graph._dtxPointPicker?.discard(); } : null);
    this._shouldForceHoverDetection = true;
    this.requestRender();
  }
  /** Retain only cancellation, never an old scene's identity, until its click. */
  _dtxCancelPointPress() {
    if (this.graph._dtxPointPress) this.graph._dtxPointPress = { cancelled: true };
  }
  _dtxPickPointerDown(event) {
    this.graph._dtxPointPress = void 0;
    const owner = this.graph._dtxPointPicker, epoch = this.graph._dtxPointPickEpoch;
    this.findHoveredItem(true);
    if (!owner || event.button !== 0) return;
    if (this._isDestroyed || this.graph._dtxPointPicker !== owner || this.graph._dtxPointPickEpoch !== epoch) {
      this.graph._dtxPointPress = { cancelled: true };
      return;
    }
    const point = this.store.hoveredPoint;
    this.graph._dtxPointPress = { owner, epoch, pointerId: event.pointerId, pointerType: event.pointerType,
      x: event.clientX, y: event.clientY, released: false,
      point: point ? { index: point.index, position: Array.from(point.position) } : null,
      link: this.store.hoveredLinkIndex ?? null };
  }
  _dtxMovePointPress(event) {
    const press = this.graph._dtxPointPress;
    if (!press || press.cancelled) return;
    // Native D3 decides whether a mouse gesture produced a click from its
    // compatibility MouseEvents, which may truncate fractional coordinates.
    // Motion inside that same coordinate cell must not discard a real click.
    // This is event-coordinate equivalence, not a distance or time tolerance.
    const sameCoordinate = (value, original) => value === original ||
      press.pointerType === "mouse" && event.pointerType === "mouse" && Math.trunc(value) === Math.trunc(original);
    if (event.pointerId !== press.pointerId || event.pointerType !== press.pointerType ||
        !sameCoordinate(event.clientX, press.x) || !sameCoordinate(event.clientY, press.y)) this._dtxCancelPointPress();
  }
  _dtxReleasePointPress(event) {
    this._dtxMovePointPress(event);
    if (event.button !== 0) this._dtxCancelPointPress();
    const press = this.graph._dtxPointPress;
    if (press && !press.cancelled) press.released = true;
  }
  /** A stationary gesture selects the precise winner pressed, not a rounded
   * MouseEvent or a different point that moved under it before release. */
  _dtxDispatchPointClick(event) {
    const press = this.graph._dtxPointPress, owner = this.graph._dtxPointPicker;
    if (!press && !owner) return false;
    this.graph._dtxPointPress = void 0;
    const epoch = this.graph._dtxPointPickEpoch;
    const current = () => !this._isDestroyed && this.graph._dtxPointPicker === owner &&
      this.graph._dtxPointPickEpoch === epoch;
    if (!current()) return true;
    if (press && event.isTrusted && event.detail > 0) {
      // PointerEvent coordinates are fractional; the corresponding legacy
      // click may truncate client coordinates. Mouse movement already had to
      // stay in that same compatibility-coordinate cell; other pointers must
      // match their exact original pointerdown.
      const sameCoordinate = (value, original) => value === original || value === Math.trunc(original) ||
        press.pointerType === "mouse" && Math.trunc(value) === Math.trunc(original);
      if (press.cancelled || !press.released || event.button !== 0 || press.owner !== owner || press.epoch !== epoch ||
          (event.pointerId !== void 0 && event.pointerId !== press.pointerId) ||
          !sameCoordinate(event.clientX, press.x) || !sameCoordinate(event.clientY, press.y)) return true;
      this.processHoverResult(press.point, press.link);
    } else {
      this.updateMousePosition(event);
      this.findHoveredItem(true);
    }
    if (!current()) return true;
    const point = this.store.hoveredPoint, link = this.store.hoveredLinkIndex;
    this.config.onClick?.(point?.index, point?.position, event);
    if (!current()) return true;
    if (point) this.config.onPointClick?.(point.index, point.position, event);
    else if (link !== void 0) this.config.onLinkClick?.(link, event);
    else this.config.onBackgroundClick?.(event);
    return true;
  }
  /** Borrow the small tracked-position framebuffer; never perform CPU readback. */
  getTrackedPointPositionsFrame() {
    const points = this.points, gl = this.device?.gl;
    if (this._isDestroyed || !gl || gl.isContextLost() || !points) return null;
    const indices = points._dtxTrackedIndices, fbo = points.trackedPositionsFbo, count = this.graph.pointsNumber;
    if (!indices?.length || !fbo || fbo.destroyed || !fbo.handle ||
        !Number.isInteger(fbo.width) || fbo.width < 1 || !Number.isInteger(fbo.height) || fbo.height < 1 ||
        !Number.isInteger(count) || indices.some(index => !Number.isInteger(index) || index < 0 || index >= count) ||
        indices.length > fbo.width * fbo.height || !points.trackPointsCommand ||
        points.trackPointsCommand.destroyed || !points.currentPositionTexture || points.currentPositionTexture.destroyed ||
        !points.currentPositionTexture.handle || !points.trackedIndicesTexture || points.trackedIndicesTexture.destroyed ||
        !points.trackedIndicesTexture.handle || points.trackedIndicesTexture.width !== fbo.width ||
        points.trackedIndicesTexture.height !== fbo.height) return null;
    points.trackPoints();
    return { gl, framebuffer: fbo.handle, width: fbo.width, height: fbo.height, indices: Array.from(indices) };
  }
`;

const pointsComment = "  /** Whether an issued async pick is still awaiting its GPU readback. */";
const frameMethod = `  /** Capture borrowed draw inputs; never read positions back to the CPU. */
  _dtxPointPickFrame() {
    const gl = this.device?.gl, count = this.data.pointsNumber, size = this.store.pointsTextureSize;
    if (!gl || gl.isContextLost() || !Number.isInteger(count) || count < 1 ||
        !Number.isInteger(size) || size < 1 || count > size * size) return null;
    if (!this.pointStatusTexture || this.pointStatusTexture.destroyed) this.updatePointStatus();
    if (!this.exitTexture || this.exitTexture.destroyed) this.updateExit();
    const resources = [this.currentPositionTexture, this.pointStatusTexture, this.exitTexture];
    if (resources.some(value => !value || value.destroyed || !value.handle) ||
        resources.slice(0, 2).some(value => value.width !== size || value.height !== size) ||
        !(resources[2].width === 1 && resources[2].height === 1 ||
          resources[2].width === size && resources[2].height === size)) return null;
    const matrix = Array.from(this.store.transformationMatrix4x4 ?? []);
    const screen = Array.from(this.store.screenSize ?? []), pointer = Array.from(this.store.screenMousePosition ?? []);
    const config = this.config, space = this.store.adjustedSpaceSize;
    if (matrix.length !== 16 || !matrix.every(Number.isFinite) || screen.length !== 2 ||
        !screen.every(value => Number.isFinite(value) && value > 0) || pointer.length !== 2 ||
        !pointer.every(Number.isFinite) || !Number.isFinite(space) || space <= 0) return null;
    return {
      gl, positionTexture: resources[0].handle, pointStatusTexture: resources[1].handle, exitTexture: resources[2].handle,
      textureSize: { width: size, height: size }, nodeCount: count,
      transformationMatrix: matrix, screenSize: screen, pointer,
      spaceSize: space, pointSizeScale: config.pointSizeScale, scalePointsOnZoom: config.scalePointsOnZoom,
      maxPointSize: this.store.maxPointSize, pixelRatio: config.pixelRatio,
      pointOpacity: config.pointOpacity, pointGreyoutOpacity: config.pointGreyoutOpacity ?? null,
    };
  }
`;

const pendingGetter = `  get hasPendingPickReadback() {
    var e;
    return ((e = this.pickingReadback) == null ? void 0 : e.inFlight) ?? !1;
  }`;
const pointsHeader = `${pointsComment}\n${pendingGetter}\n  updatePositions() {`;
const pointDiscard = `  discardPendingPick() {
    var e;
    (e = this.pickingReadback) == null || e.cancel(), this.issuedPickingWindow = void 0;
  }`;
const pointData = "  updatePositions() {\n    var C, g;";
const clickStart = `    if (this._shouldSuppressNextClick) {
      this._shouldSuppressNextClick = !1;
      return;
    }
    (o = (n = this.config).onClick)`;
const pointerEnter = "l.isPrimary && (this._isPointerOnCanvas = !0, this._lastMouseX = l.clientX, this._lastMouseY = l.clientY, this.requestRender());";
const pointerLeave = "l.isPrimary && (this.cancelLongPress(), this._isPointerOnCanvas = !1,";
const movement = "const t = Math.abs(this._lastMouseX - this._lastCheckedMouseX), i = Math.abs(this._lastMouseY - this._lastCheckedMouseY), n = t > j || i > j;";
const pointerDown = "this.updateMousePosition(l), this.findHoveredItem(!0), this.requestRender(), l.pointerType !==";
const pointerMove = "this.currentEvent = l, this.updateMousePosition(l), this.isRightClickMouse =";
const pointerUp = "l.isPrimary && (this.cancelLongPress(), this.isRightClickMouse =";

/** Reversible optional delegation; every unattached branch retains upstream picking. */
export const COSMOS_POINT_PICKER_PATCH_HUNKS = Object.freeze([
  { id: "picker-host-api", before: pinnedPointsComment, after: hostMethods + pinnedPointsComment },
  { id: "picker-borrowed-frame", before: pointsHeader, after: frameMethod + pointsHeader },
  { id: "picker-pending", before: `${pendingGetter}\n  updatePositions() {`,
    after: `${pendingGetter.replace("    var e;", "    if (this.data._dtxPointPicker) return this.data._dtxPointPicker.hasPending;\n    var e;")}\n  updatePositions() {` },
  { id: "picker-sync", before: "  pickPointSync() {\n    this.updatePickingBuffer();",
    after: "  pickPointSync() {\n    if (this.data._dtxPointPicker) return this.data._dtxPointPicker.pickSync(this._dtxPointPickFrame());\n    this.updatePickingBuffer();" },
  { id: "picker-request", before: "  requestPickPoint() {\n    var n;",
    after: "  requestPickPoint() {\n    if (this.data._dtxPointPicker) return this.data._dtxPointPicker.request(this._dtxPointPickFrame());\n    var n;" },
  { id: "picker-take", before: "  takePickResult() {\n    if (!this.pickingReadback",
    after: "  takePickResult() {\n    if (this.data._dtxPointPicker) return this.data._dtxPointPicker.take(this._dtxPointPickFrame());\n    if (!this.pickingReadback" },
  { id: "picker-discard", before: pointDiscard,
    after: pointDiscard.replace("    var e;", "    this.data._dtxPointPicker?.discard();\n    var e;") },
  { id: "picker-data-epoch", before: pointData,
    after: pointData.replace("    var C, g;", "    this.data._dtxPointPicker?.discard();\n    if (this.data._dtxPointPress) this.data._dtxPointPress = { cancelled: true };\n    if (this.data._dtxPointPicker) this.data._dtxPointPickEpoch = (this.data._dtxPointPickEpoch ?? 0) + 1;\n    var C, g;") },
  { id: "picker-pending-data-epoch", before: "    this.graph.inputPointPositions = e, this.points.shouldSkipRescale = t,",
    after: "    this._dtxCancelPointPress();\n    if (this.graph._dtxPointPicker) this.graph._dtxPointPickEpoch = (this.graph._dtxPointPickEpoch ?? 0) + 1;\n    this.graph.inputPointPositions = e, this.points.shouldSkipRescale = t," },
  { id: "tracked-index-snapshot", before: "  trackPointsByIndices(e) {\n    const { device: t } = this;",
    after: "  trackPointsByIndices(e) {\n    this._dtxTrackedIndices = Array.from(e ?? []);\n    const { device: t } = this;" },
  { id: "picker-hover-cadence", before: "if (!e && this._findHoveredItemExecutionCount < Ct)",
    after: "if (!e && !this.graph._dtxPointPicker && this._findHoveredItemExecutionCount < Ct)" },
  { id: "picker-pointer-precision", before: movement,
    after: movement.replace("n = t > j || i > j", "n = this.graph._dtxPointPicker ? t > 0 || i > 0 : t > j || i > j") },
  { id: "picker-pointer-enter", before: pointerEnter,
    after: pointerEnter.replace("this.requestRender()", "this.graph._dtxPointPicker && (this.currentEvent = l, this.updateMousePosition(l), this._shouldForceHoverDetection = !0), this.requestRender()") },
  { id: "picker-pointer-leave", before: pointerLeave,
    after: pointerLeave.replace("this.cancelLongPress(),", "this.cancelLongPress(), this._dtxCancelPointPress(), this.graph._dtxPointPicker?.discard(),") },
  { id: "picker-precise-press", before: pointerDown,
    after: pointerDown.replace("this.findHoveredItem(!0)", "this._dtxPickPointerDown(l)") },
  { id: "picker-press-movement", before: pointerMove,
    after: pointerMove.replace("this.currentEvent = l,", "this._dtxMovePointPress(l), this.currentEvent = l,") },
  { id: "picker-press-release", before: pointerUp,
    after: pointerUp.replace("this.cancelLongPress(),", "this.cancelLongPress(), this._dtxReleasePointPress(l),") },
  { id: "picker-context-press-cancel", before: "  fireContextMenu(e) {\n    var t, i, n, o, s, r, d, h, u, l;",
    after: "  fireContextMenu(e) {\n    this._dtxCancelPointPress();\n    var t, i, n, o, s, r, d, h, u, l;" },
  { id: "picker-fresh-click", before: clickStart,
    after: clickStart.replace("this._shouldSuppressNextClick = !1;", "this._shouldSuppressNextClick = !1;\n      this.graph._dtxPointPress = void 0;")
      .replace("    (o =", "    if (this._dtxDispatchPointClick(e)) return;\n    (o =") },
  { id: "picker-owned-cleanup", before: "      const simulation = this._gpuSimulation;",
    after: `      const pointPicker = this.graph._dtxPointPicker;
      this.graph._dtxPointPicker = null;
      this.graph._dtxPointPress = void 0;
      try { pointPicker?.destroy(); }
      catch (error) { console.error("Point picker cleanup failed", error); }
      const simulation = this._gpuSimulation;` },
].map(hunk => Object.freeze(hunk)));

export function patchCosmosPointPicker(source, version) {
  assert.equal(version, COSMOS_VERSION, "Review point picking inputs before upgrading Cosmos");
  let original = source;
  for (const hunks of [COSMOS_COLLISION_RADII_PATCH_HUNKS, COSMOS_GPU_SIMULATION_PATCH_HUNKS]) {
    for (const { id, before, after } of [...hunks].reverse()) {
      assert.equal(original.split(after).length, 2, `Expected exactly one reviewed ${id} seam`);
      original = original.replace(after, before);
    }
  }
  assert.equal(sha256(original), COSMOS_SOURCE_SHA256, "Cosmos source differs from the reviewed package");
  let modified = source;
  for (const { id, before, after } of COSMOS_POINT_PICKER_PATCH_HUNKS) {
    assert.equal(modified.split(before).length, 2, `Expected exactly one ${id} seam`);
    modified = modified.replace(before, after);
  }
  let reversed = modified;
  for (const { id, before, after } of [...COSMOS_POINT_PICKER_PATCH_HUNKS].reverse()) {
    assert.equal(reversed.split(after).length, 2, `Expected exactly one patched ${id} seam`);
    reversed = reversed.replace(after, before);
  }
  assert.equal(reversed, source, "Unexpected extra point picker SDK change");
  return modified;
}

import { NativeGraph } from "../../frontend/src/features/graph/workspace/cosmosNative";
import { createWorkspaceNativePointPicker } from "../../frontend/src/features/graph/workspace/nativePointPicker";
import type { CosmosPointPickFrame, CosmosPointPicker } from "../../frontend/src/features/graph/workspace/cosmosPointPickingTypes";
import { withWorkspaceForceAtlas2GlState } from "../../frontend/src/features/graph/workspace/forceAtlas2GlState";

const container = document.querySelector<HTMLDivElement>("#graph")!;
const params = new URLSearchParams(location.search);
let cameraFinished: (() => void) | null = null;
// The camera is set only by configure(): Cosmos' own fit-on-init would re-fit it 250 ms after the first render, mid-test.
const graph = new NativeGraph(container, { spaceSize: 4096, rescalePositions: false, enableSimulation: false,
  fitViewOnInit: false, enableZoom: true, enableDrag: true, pixelRatio: Number(params.get("dpr") ?? 1), scalePointsOnZoom: false,
  pointSizeScale: 1, backgroundColor: "white", renderLinks: false,
  onPointMouseOver(index) { hovered = index; }, onPointMouseOut() { hovered = null; },
  onPointClick(index) { selected = index; }, onBackgroundClick() { selected = null; },
  onDragStart() { dragging = hovered; }, onDragEnd() { dragging = null; },
  onZoomEnd() { cameraFinished?.(); },
});
let hovered: number | null = null, selected: number | null = null, dragging: number | null = null;
let picker: CosmosPointPicker;
let contextIndex = 0, libraryIndex = 2000;
const readbacks: Array<{ width: number; height: number; floats: number | null; pbo: boolean }> = [];
type QueryObservation = { method: string; pointer: readonly [number, number] | undefined;
  matrix: readonly number[] | undefined; result: unknown; pending: boolean };
const queries: QueryObservation[] = [];
const releaseMotion: Array<{ type: string; hovered: number | null; selected: number | null;
  dragging: number | null; writes?: Array<{ index: number; position: [number, number] }> }> = [];

function native() {
  return graph as unknown as { points: { _dtxPointPickFrame(): CosmosPointPickFrame | null } };
}
function capturedFrame(pointer?: [number, number]) {
  const value = native().points._dtxPointPickFrame();
  if (!value) throw new Error("Native point-pick resources absent");
  return pointer ? { ...value, pointer: [pointer[0], value.screenSize[1] - pointer[1]] as const } : value;
}
function captureState(gl: WebGL2RenderingContext) {
  const parameters = [gl.CURRENT_PROGRAM, gl.VERTEX_ARRAY_BINDING, gl.DRAW_FRAMEBUFFER_BINDING, gl.READ_FRAMEBUFFER_BINDING,
    gl.READ_BUFFER, gl.VIEWPORT, gl.ACTIVE_TEXTURE, gl.COLOR_WRITEMASK, gl.PIXEL_PACK_BUFFER_BINDING,
    gl.PIXEL_UNPACK_BUFFER_BINDING, gl.PACK_ALIGNMENT, gl.PACK_ROW_LENGTH, gl.PACK_SKIP_PIXELS, gl.PACK_SKIP_ROWS,
    gl.DEPTH_FUNC, gl.DEPTH_WRITEMASK, gl.DEPTH_RANGE, gl.DEPTH_CLEAR_VALUE, gl.RENDERBUFFER_BINDING];
  const flags = [gl.BLEND, gl.DEPTH_TEST, gl.STENCIL_TEST, gl.SCISSOR_TEST, gl.CULL_FACE, gl.RASTERIZER_DISCARD];
  return { parameters: parameters.map(parameter => {
    const value: unknown = gl.getParameter(parameter);
    return ArrayBuffer.isView(value) || Array.isArray(value) ? Array.from(value as ArrayLike<unknown>) : value;
  }), flags: flags.map(flag => gl.isEnabled(flag)) };
}
function equalState(before: ReturnType<typeof captureState>, after: ReturnType<typeof captureState>) {
  return before.parameters.map((value, index) => Array.isArray(value)
    ? value.every((entry, offset) => entry === (after.parameters[index] as unknown[])[offset])
    : value === after.parameters[index]).concat(before.flags.map((value, index) => value === after.flags[index]));
}

async function configure(options: { reverse?: boolean; coincident?: boolean; fractional?: boolean;
  diameter?: number; zoom?: number; noProvider?: boolean } = {}) {
  graph.attachPointPicker(null); graph.pause();
  hovered = null; selected = null; dragging = null;
  contextIndex = options.reverse ? 2000 : 0; libraryIndex = options.reverse ? 0 : 2000;
  const positions = new Float32Array(2001 * 2).fill(4000), sizes = new Float32Array(2001).fill(3);
  const shapes = new Float32Array(2001), colors = new Float32Array(2001 * 4);
  const contextPosition: [number, number] = options.fractional ? [2048.75, 2047.25] : [2048, 2048];
  const libraryPosition: [number, number] = options.fractional ? [2049, 2047] : [options.coincident ? 2048 : 2050, 2048];
  positions.set(contextPosition, contextIndex * 2);
  positions.set(libraryPosition, libraryIndex * 2);
  sizes[contextIndex] = 6; sizes[libraryIndex] = options.diameter ?? 20; shapes[libraryIndex] = 1;
  for (let index = 0; index < 2001; index++) colors.set(index === libraryIndex ? [1, .4, .1, 1] : [.5, .15, 1, 1], index * 4);
  graph.setPointPositions(positions); graph.setPointSizes(sizes); graph.setPointShapes(shapes); graph.setPointColors(colors);
  graph.render(undefined, 0);
  // Isolate query precision at each controlled zoom; scaleTo alone retains
  // prior camera translation and can move this synthetic target off-canvas.
  await new Promise<void>(resolve => {
    cameraFinished = () => {
      const center = graph.spaceToScreenPosition([2048, 2048]);
      if (Math.abs(graph.getZoomLevel() - (options.zoom ?? 1)) > 1e-6
        || Math.abs(center[0] - 300) > .0001 || Math.abs(center[1] - 200) > .0001) return;
      cameraFinished = null; resolve();
    };
    graph.setZoomTransformByPointPositions(Float32Array.from([2048, 2048]), 0, options.zoom ?? 1, 0, false);
  });
  const owner = createWorkspaceNativePointPicker(sizes, shapes);
  const note = (method: string, frame: CosmosPointPickFrame | null, result: unknown) => {
    queries.push({ method, pointer: frame?.pointer, matrix: frame?.transformationMatrix, result, pending: owner.hasPending });
    if (queries.length > 64) queries.shift();
  };
  picker = {
    get hasPending() { return owner.hasPending; },
    pickSync(frame) { const result = owner.pickSync(frame); note("sync", frame, result); return result; },
    request(frame) { const result = owner.request(frame); note("request", frame, result); return result; },
    take(frame) { const pending = owner.hasPending, result = owner.take(frame); if (pending) note("take", frame, result); return result; },
    discard() { owner.discard(); }, destroy() { owner.destroy(); },
  };
  queries.length = 0;
  if (!options.noProvider) graph.attachPointPicker(picker);
  await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  readbacks.length = 0;
  return { context: graph.spaceToScreenPosition(contextPosition),
    library: graph.spaceToScreenPosition(libraryPosition), contextIndex, libraryIndex };
}

function armReleaseMotion() {
  releaseMotion.length = 0;
  const canvas = container.querySelector("canvas")!;
  // Known-row GPU writes model position motion between press and click. Do
  // NOT call setPointPositions: a replacement scene legitimately retires a
  // gesture, whereas ordinary native physics retains the same original IDs.
  // This fixture never reads GPU positions or changes the camera/force law.
  canvas.addEventListener("pointerup", () => {
    const frame = capturedFrame();
    const writes = [{ index: contextIndex, position: [2053, 2048] as [number, number] },
      { index: libraryIndex, position: [2048, 2048] as [number, number] }];
    withWorkspaceForceAtlas2GlState(frame.gl, () => {
      frame.gl.activeTexture(frame.gl.TEXTURE0);
      frame.gl.bindTexture(frame.gl.TEXTURE_2D, frame.positionTexture);
      for (const { index, position } of writes) frame.gl.texSubImage2D(frame.gl.TEXTURE_2D, 0,
        index % frame.textureSize.width, Math.floor(index / frame.textureSize.width), 1, 1,
        frame.gl.RGBA, frame.gl.FLOAT, new Float32Array([position[0], position[1], index, 0]));
    });
    releaseMotion.push({ type: "known-position-motion-after-native-pointerup", hovered, selected, dragging, writes });
  }, { once: true });
  canvas.addEventListener("pointerdown", () => {
    releaseMotion.push({ type: "after-native-pointerdown", hovered, selected, dragging });
  }, { once: true });
  canvas.addEventListener("click", () => {
    releaseMotion.push({ type: "after-native-click", hovered, selected, dragging });
  }, { once: true });
}

declare global {
  interface Window { __nativePointPicking: {
    configure: typeof configure;
    armReleaseMotion: typeof armReleaseMotion;
    snapshot(): { hovered: number | null; selected: number | null; dragging: number | null;
      readbacks: typeof readbacks; queries: QueryObservation[]; releaseMotion: typeof releaseMotion; glError: number };
    query(pointer: [number, number]): ReturnType<CosmosPointPicker["pickSync"]>;
    stale(kind: "pointer" | "view" | "scene"): { pendingBefore: boolean; pendingAfter: boolean; result: ReturnType<CosmosPointPicker["take"]> };
    state(): { restored: boolean[]; result: ReturnType<CosmosPointPicker["pickSync"]>; glError: number };
    lose(): Promise<{ pendingBefore: boolean; pendingAfter: boolean; result: ReturnType<CosmosPointPicker["pickSync"]> }>;
    debug(): unknown;
    destroy(): void;
  }; }
}

void graph.ready.then(async () => {
  const gl = container.querySelector("canvas")!.getContext("webgl2")!;
  const readPixels = gl.readPixels.bind(gl);
  gl.readPixels = ((...args: unknown[]) => {
    readbacks.push({ width: args[2] as number, height: args[3] as number,
      floats: args[6] instanceof Float32Array ? args[6].length : null,
      pbo: gl.getParameter(gl.PIXEL_PACK_BUFFER_BINDING) !== null });
    return (readPixels as (...args: unknown[]) => void)(...args);
  }) as typeof gl.readPixels;
  await configure();
  window.__nativePointPicking = {
    configure,
    armReleaseMotion,
    snapshot: () => ({ hovered, selected, dragging, readbacks: readbacks.slice(), queries: queries.slice(),
      releaseMotion: releaseMotion.slice(), glError: gl.getError() }),
    debug: () => {
      const host = graph as unknown as { store: { hoveredPoint: unknown; screenMousePosition: unknown };
        _isPointerOnCanvas: boolean; _lastMouseX: number; _lastCheckedMouseX: number; _shouldForceHoverDetection: boolean };
      return { hovered, selected, nativeHovered: host.store.hoveredPoint, pointer: host.store.screenMousePosition,
        onCanvas: host._isPointerOnCanvas, mouse: host._lastMouseX, checkedMouse: host._lastCheckedMouseX,
        forced: host._shouldForceHoverDetection, pending: picker.hasPending, queries: queries.slice(), glError: gl.getError() };
    },
    query: pointer => picker.pickSync(capturedFrame(pointer)),
    stale(kind) {
      const input = capturedFrame([300, 200]);
      picker.request(input); const pendingBefore = picker.hasPending;
      let changed: CosmosPointPickFrame | null = input;
      if (kind === "pointer") changed = { ...input, pointer: [input.pointer[0] + .5, input.pointer[1]] };
      if (kind === "view") changed = { ...input, screenSize: [input.screenSize[0] + 1, input.screenSize[1]] };
      if (kind === "scene") { graph.attachPointPicker(null); changed = null; }
      const result = picker.take(changed);
      return { pendingBefore, pendingAfter: picker.hasPending, result };
    },
    state() {
      gl.depthFunc(gl.GREATER); gl.depthMask(false); gl.depthRange(.2, .8); gl.clearDepth(.3);
      const renderbuffer = gl.createRenderbuffer(), pack = gl.createBuffer();
      gl.bindRenderbuffer(gl.RENDERBUFFER, renderbuffer);
      gl.bindBuffer(gl.PIXEL_PACK_BUFFER, pack); gl.bufferData(gl.PIXEL_PACK_BUFFER, 256, gl.STREAM_READ);
      gl.pixelStorei(gl.PACK_ROW_LENGTH, 11); gl.pixelStorei(gl.PACK_SKIP_PIXELS, 2); gl.pixelStorei(gl.PACK_SKIP_ROWS, 1);
      const before = captureState(gl);
      const result = picker.pickSync(capturedFrame([300, 200]));
      const restored = equalState(before, captureState(gl));
      gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null); gl.deleteBuffer(pack);
      gl.bindRenderbuffer(gl.RENDERBUFFER, null); gl.deleteRenderbuffer(renderbuffer);
      gl.pixelStorei(gl.PACK_ROW_LENGTH, 0); gl.pixelStorei(gl.PACK_SKIP_PIXELS, 0); gl.pixelStorei(gl.PACK_SKIP_ROWS, 0);
      gl.depthFunc(gl.LESS); gl.depthMask(true); gl.depthRange(0, 1); gl.clearDepth(1);
      return { restored, result, glError: gl.getError() };
    },
    async lose() {
      const extension = gl.getExtension("WEBGL_lose_context");
      if (!extension) throw new Error("Context-loss fixture extension absent");
      picker.request(capturedFrame([300, 200])); const pendingBefore = picker.hasPending;
      const lost = new Promise<void>(resolve => container.querySelector("canvas")!.addEventListener("webglcontextlost", () => resolve(), { once: true }));
      extension.loseContext(); await lost;
      return { pendingBefore, pendingAfter: picker.hasPending, result: picker.pickSync(null) };
    },
    destroy() { graph.destroy(); },
  };
});

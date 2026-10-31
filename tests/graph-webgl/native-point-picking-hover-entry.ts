import { NativeGraph } from "../../frontend/src/features/graph/workspace/cosmosNative";
import { createWorkspaceNativePointPicker, nativePointTargetsPointer, projectNativePoint } from "../../frontend/src/features/graph/workspace/nativePointPicker";
import type { CosmosPointPickFrame, CosmosPointPicker } from "../../frontend/src/features/graph/workspace/cosmosPointPickingTypes";

// A native scene to hover with a pointer that moves every frame, observed through the real
// Cosmos frame loop and the real picker. A second, never-attached picker answers fresh and
// aged queries without Cosmos consuming them, so the CPU target test can be set against the GPU.
const container = document.querySelector<HTMLDivElement>("#graph")!;
const params = new URLSearchParams(location.search);
const [canvasWidth, canvasHeight] = (params.get("size") ?? "600x400").split("x").map(Number);
container.style.width = `${canvasWidth}px`; container.style.height = `${canvasHeight}px`;
// Both pickers read this clock, one tick of 16 ms per animation frame: their rate limit counts frames, and a read that
// a loaded machine delays can never look slow to them and start a back-off.
const clock = () => frameNumber * 16;
const COLUMNS = 25, ROWS = 9;
type Body = { position: [number, number]; size: number; shape: 0 | 1 };
// An answer the picker delivered, with the pointer it was asked at and the pointer it was delivered for.
type Take = { pointer: [number, number] | null; asked: [number, number] | null; index: number | null };
const takes: Take[] = [], overs: number[] = [];
let frameNumber = 0, hovered: number | null = null, cameraFinished: (() => void) | null = null;
let bodies: Body[] = [], picker: CosmosPointPicker, probe: CosmosPointPicker | null = null;
let scene: { sizes: Float32Array; shapes: Float32Array } | null = null;
// The pointer of the query now in flight, taken when it was asked and not when it is taken.
let asked: [number, number] | null = null;

// The camera is set only by load(): Cosmos' own fit-on-init would re-zoom a scene 250 ms after its first render.
const graph = new NativeGraph(container, { spaceSize: 4096, rescalePositions: false, enableSimulation: false,
  fitViewOnInit: false, enableZoom: true, enableDrag: true, pixelRatio: Number(params.get("dpr") ?? 1), scalePointsOnZoom: params.get("scale") === "1",
  pointSizeScale: 1, backgroundColor: "white", renderLinks: false,
  onPointMouseOver(index) { hovered = index; overs.push(index); },
  onPointMouseOut() { hovered = null; },
  onZoomEnd() { cameraFinished?.(); },
});
const tick = () => { frameNumber += 1; requestAnimationFrame(tick); };
requestAnimationFrame(tick);

const frames = (count: number) => new Promise<void>(resolve => {
  const step = (left: number) => requestAnimationFrame(() => (left > 1 ? step(left - 1) : resolve()));
  step(count);
});

/**
 * Moves the pointer along a row from `from` to `to` in CSS pixels, `step` per animation frame: one pointermove per frame,
 * the first at once. Synthetic events, so that the pace is the page's own frame rate and never the speed of the test driver.
 */
function sweep(from: number, to: number, y: number, step: number) {
  const canvas = container.querySelector("canvas")!, box = canvas.getBoundingClientRect();
  return new Promise<void>(resolve => {
    let x = from;
    const move = () => {
      canvas.dispatchEvent(new PointerEvent("pointermove", { isPrimary: true, pointerType: "mouse", bubbles: true, clientX: box.left + x, clientY: box.top + y }));
      if (x >= to) { resolve(); return; }
      x = Math.min(x + step, to); requestAnimationFrame(move);
    };
    move();
  });
}

function native() {
  return graph as unknown as { points: { _dtxPointPickFrame(): CosmosPointPickFrame | null } };
}
/** The borrowed frame Cosmos would pass; `pointer` is in top-left CSS pixels here, as in page coordinates. */
function sceneFrame(pointer?: readonly [number, number]): CosmosPointPickFrame {
  const value = native().points._dtxPointPickFrame();
  if (!value) throw new Error("Native point-pick resources absent");
  return pointer ? { ...value, pointer: [pointer[0], value.screenSize[1] - pointer[1]] } : value;
}

function denseGrid(spacing: number): Body[] {
  return Array.from({ length: COLUMNS * ROWS }, (_, index) => ({
    position: [2048 + ((index % COLUMNS) - (COLUMNS - 1) / 2) * spacing, 2048 + (Math.floor(index / COLUMNS) - (ROWS - 1) / 2) * spacing],
    size: 6, shape: 0,
  }));
}

async function load(next: Body[], options: { zoom?: number; center?: [number, number] } = {}) {
  graph.attachPointPicker(null); graph.pause(); probe?.destroy();
  hovered = null; takes.length = 0; overs.length = 0; bodies = next;
  const positions = new Float32Array(next.length * 2), colors = new Float32Array(next.length * 4);
  next.forEach((body, index) => { positions.set(body.position, index * 2); colors.set([.5, .15, 1, 1], index * 4); });
  const sizes = Float32Array.from(next, body => body.size), shapes = Float32Array.from(next, body => body.shape);
  scene = { sizes, shapes };
  graph.setPointPositions(positions); graph.setPointSizes(sizes); graph.setPointShapes(shapes); graph.setPointColors(colors);
  graph.render(undefined, 0);
  const [centerX, centerY] = options.center ?? [2048, 2048], zoom = options.zoom ?? 1;
  await new Promise<void>(resolve => {
    cameraFinished = () => {
      const screen = graph.spaceToScreenPosition([centerX, centerY]), box = container.getBoundingClientRect();
      if (Math.abs(graph.getZoomLevel() - zoom) > 1e-6 || Math.abs(screen[0] - box.width / 2) > .0001 || Math.abs(screen[1] - box.height / 2) > .0001) return;
      cameraFinished = null; resolve();
    };
    graph.setZoomTransformByPointPositions(Float32Array.from([centerX, centerY]), 0, zoom, 0, false);
  });
  const owner = createWorkspaceNativePointPicker(sizes, shapes, undefined, clock);
  picker = {
    get hasPending() { return owner.hasPending; },
    pickSync(frame) { return owner.pickSync(frame); },
    request(frame) {
      const queued = owner.request(frame);
      if (queued && frame) asked = [frame.pointer[0], frame.pointer[1]];
      return queued;
    },
    take(frame) {
      const result = owner.take(frame);
      if (result !== undefined) takes.push({ pointer: frame ? [frame.pointer[0], frame.pointer[1]] : null, asked, index: result?.index ?? null });
      return result;
    },
    discard() { owner.discard(); }, destroy() { owner.destroy(); },
  };
  probe = createWorkspaceNativePointPicker(sizes, shapes, undefined, clock);
  graph.attachPointPicker(picker);
  await frames(2);
  takes.length = 0; overs.length = 0;
  return bodies.map(body => graph.spaceToScreenPosition(body.position));
}

async function loadGrid(spacing: number) {
  const screen = await load(denseGrid(spacing)), middle = Math.floor(ROWS / 2) * COLUMNS;
  return { spacing, columns: COLUMNS, rows: ROWS, rowStart: middle, rowY: screen[middle][1],
    columnX: screen.slice(middle, middle + COLUMNS).map(point => point[0]) };
}

async function loadIsolated(options: { zoom: number; center: [number, number] }) {
  // Three bodies far apart, so a query near one can only ever answer that one.
  const next: Body[] = [
    { position: [options.center[0], options.center[1]], size: 6, shape: 0 },
    { position: [options.center[0] + 130 / options.zoom, options.center[1] - 60 / options.zoom], size: 40, shape: 0 },
    { position: [options.center[0] - 130 / options.zoom, options.center[1] + 60 / options.zoom], size: 40, shape: 1 },
  ];
  return load(next, options);
}

/** Fresh GPU answer versus the CPU test, with the pointer `offset` CSS pixels from each body's projected centre. */
function agreement(samples: ReadonlyArray<{ node: number; offset: [number, number] }>) {
  return samples.map(({ node, offset }) => {
    const body = bodies[node], center = graph.spaceToScreenPosition(body.position);
    const frame = sceneFrame([center[0] + offset[0], center[1] + offset[1]]);
    const gpu = probe!.pickSync(frame), projected = projectNativePoint(frame, body.position);
    return { node, offset, gpu: gpu?.index ?? null, cpu: nativePointTargetsPointer(frame, body.position, body.size, body.shape),
      projectedX: projected[0], projectedY: frame.screenSize[1] - projected[1], centerX: center[0], centerY: center[1] };
  });
}

type Change = "near" | "left" | "far" | "pan";
/**
 * Ask a picker of its own (never attached, so its rate limit holds no earlier case) at body 0, then take it as `change`.
 * A fence that has not resolved yet keeps its query for the next frame, so a slow machine only waits longer; it can
 * never change the outcome.
 */
async function aged(change: Change) {
  const asker = createWorkspaceNativePointPicker(scene!.sizes, scene!.shapes, undefined, clock);
  try {
    const target = graph.spaceToScreenPosition(bodies[0].position), input = sceneFrame(target);
    const queued = asker.request(input) && asker.hasPending;
    await frames(1);
    const shift = (dx: number): CosmosPointPickFrame => ({ ...input, pointer: [input.pointer[0] + dx, input.pointer[1]] });
    // A pan changes only the translation column, which a check of the scale alone would not see.
    const panned = { ...input, transformationMatrix: input.transformationMatrix.map((value, index) => index === 8 ? value + 1e-4 : value) };
    const later = change === "near" ? shift(5) : change === "left" ? shift(13) : change === "far" ? shift(200) : panned;
    let result = asker.take(later);
    for (let waited = 0; result === undefined && asker.hasPending && waited < 600; waited++) { await frames(1); result = asker.take(later); }
    return { queued, pendingAfter: asker.hasPending, outcome: result === undefined ? "obsolete" : result === null ? "no-point" : `index-${result.index}` };
  } finally { asker.destroy(); }
}

declare global {
  interface Window { __nativeHover: {
    loadGrid: typeof loadGrid;
    loadIsolated: typeof loadIsolated;
    agreement: typeof agreement;
    aged: typeof aged;
    sweep: typeof sweep;
    /** Forget what was recorded so far, so a phase of the test starts from what is shown now. */
    reset(): void;
    snapshot(): { hovered: number | null; overs: number[]; takes: Take[]; glError: number };
  }; }
}

void graph.ready.then(async () => {
  const gl = container.querySelector("canvas")!.getContext("webgl2")!;
  await loadGrid(16);
  window.__nativeHover = {
    loadGrid, loadIsolated, agreement, aged, sweep,
    reset() { takes.length = 0; overs.length = 0; },
    snapshot: () => ({ hovered, overs: overs.slice(), takes: takes.slice(), glError: gl.getError() }),
  };
});

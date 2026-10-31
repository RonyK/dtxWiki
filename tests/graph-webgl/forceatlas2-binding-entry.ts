import { RestLogGraph as Graph } from "../../frontend/src/features/graph/workspace/cosmosRestLog";
import { createWorkspaceForceAtlas2Binding, type WorkspaceForceAtlas2BindingInput } from "../../frontend/src/features/graph/workspace/forceAtlas2Binding";
import { withWorkspaceForceAtlas2GlState } from "../../frontend/src/features/graph/workspace/forceAtlas2GlState";
import type { CosmosGpuSimulation, CosmosGpuSimulationFrame } from "../../frontend/src/features/graph/workspace/cosmosGpuTypes";

type InputRead = { positions: number[]; radii: number[]; sources: number[]; targets: number[]; weights: number[]; origin: number[] };
export type BindingSnapshot = {
  positions: number[];
  nativeRgba: number[];
  generations: number;
  invalidations: number;
  simulationSteps: number;
  afterDragCalls: number;
  simulationDestroys: number;
  readPixelsDuringSteps: number;
  running: boolean;
  contextLost: boolean;
  glError: number;
  inputReads: InputRead[];
  rendererLinkUploads: number[][];
};

type TransitionEvidence = { before: BindingSnapshot; after: BindingSnapshot };
declare global {
  interface Window {
    __forceAtlas2Binding: {
      status: "loading" | "ready" | "error";
      error?: string;
      initialRendererPositions: number[];
      snapshot(): BindingSnapshot;
      manual(iterations: number): TransitionEvidence;
      fitAndDraw(): Promise<TransitionEvidence>;
      styleOnly(): Promise<TransitionEvidence>;
      replaceSameCount(): Promise<TransitionEvidence & { committed: BindingSnapshot; replacement: number[] }>;
      dragRow(index: number, position: [number, number]): TransitionEvidence;
      pauseResume(): Promise<TransitionEvidence & { resumedRunning: boolean }>;
      detach(): { simulationDestroys: number; frameStillAvailable: boolean; glError: number };
      destroy(): void;
    };
  }
}

const initialRendererPositions = [-5000, -6000, 7000, 8000];
const origin = [2048, 2048] as const;
const inputReads: InputRead[] = [], rendererLinkUploads: number[][] = [];
let input: WorkspaceForceAtlas2BindingInput = {
  positions: Float32Array.from(initialRendererPositions, (value, index) => value - origin[index % 2]),
  radii: new Float32Array([1, 1]), sources: new Uint32Array([0]),
  targets: new Uint32Array([1]), weights: new Float64Array([1]), pins: [0], origin,
};
let graph: Graph | null = null, gl: WebGL2RenderingContext | null = null;
let generations = 0, invalidations = 0, simulationSteps = 0, afterDragCalls = 0, simulationDestroys = 0;
let countingPhysics = false, readPixelsDuringSteps = 0;
let originalReadPixels: WebGL2RenderingContext["readPixels"] | null = null;

const binding = createWorkspaceForceAtlas2Binding(() => {
  generations++;
  inputReads.push({ positions: Array.from(input.positions), radii: Array.from(input.radii),
    sources: Array.from(input.sources), targets: Array.from(input.targets),
    weights: Array.from(input.weights), origin: [...input.origin] });
  return input;
});
const simulation: CosmosGpuSimulation = {
  step(frame) { simulationSteps++; binding.step(frame); },
  afterDrag(frame, index) { afterDragCalls++; binding.afterDrag(frame, index); },
  invalidate() { invalidations++; binding.invalidate(); },
  destroy() { simulationDestroys++; binding.destroy(); },
};

function nativeFrame(): CosmosGpuSimulationFrame {
  const frame = graph?.getGpuSimulationFrame();
  if (!frame) throw new Error("Native public GPU frame is unavailable");
  return frame;
}
function snapshot(): BindingSnapshot {
  if (!graph || !gl) throw new Error("Native graph is not ready");
  const frame = nativeFrame();
  const pixels = new Float32Array(frame.textureSize.width * frame.textureSize.height * 4);
  // Diagnostics happen OUTSIDE the counted physics interval. Read only the
  // borrowed public native target; do not inspect private SDK/module fields.
  withWorkspaceForceAtlas2GlState(gl, () => {
    gl!.bindFramebuffer(gl!.READ_FRAMEBUFFER, frame.positionFramebuffer);
    gl!.readBuffer(gl!.COLOR_ATTACHMENT0);
    gl!.readPixels(0, 0, frame.textureSize.width, frame.textureSize.height, gl!.RGBA, gl!.FLOAT, pixels);
  });
  return { positions: Array.from(graph.getPointPositions()), nativeRgba: Array.from(pixels.slice(0, frame.nodeCount * 4)),
    generations, invalidations, simulationSteps, afterDragCalls, simulationDestroys, readPixelsDuringSteps,
    running: graph.isSimulationRunning, contextLost: gl.isContextLost(), glError: gl.getError(),
    inputReads: inputReads.map(read => ({ ...read, positions: [...read.positions], radii: [...read.radii],
      sources: [...read.sources], targets: [...read.targets], weights: [...read.weights], origin: [...read.origin] })),
    rendererLinkUploads: rendererLinkUploads.map(links => [...links]) };
}
function manual(iterations: number): TransitionEvidence {
  if (!graph || !Number.isSafeInteger(iterations) || iterations < 1) throw new Error("Positive manual iterations required");
  graph.pause();
  const before = snapshot();
  countingPhysics = true;
  try {
    // The upstream method is private in its declarations. This exact narrow
    // test-only cast exercises the native swap/step seam without scheduling
    // extra draws; production must not use this diagnostic call.
    const manualGraph = graph as unknown as { runSimulationStep(force: boolean): void };
    for (let iteration = 0; iteration < iterations; iteration++) manualGraph.runSimulationStep(true);
  } finally { countingPhysics = false; }
  return { before, after: snapshot() };
}
const rendered = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
function uploadLinks(links: Float32Array): void {
  rendererLinkUploads.push(Array.from(links));
  graph!.setLinks(links);
}

window.__forceAtlas2Binding = {
  status: "loading", initialRendererPositions, snapshot, manual,
  async fitAndDraw() {
    const before = snapshot();
    graph!.fitView(0, .08, false); graph!.pause();
    await rendered();
    return { before, after: snapshot() };
  },
  async styleOnly() {
    const before = snapshot();
    graph!.setPointColors(new Float32Array([.1, .6, .3, 1, .7, .2, .8, 1]));
    graph!.render(undefined, 0); graph!.pause();
    await rendered();
    return { before, after: snapshot() };
  },
  async replaceSameCount() {
    const before = snapshot();
    const replacement = [-4500, -5500, 8500, 10000];
    input = { ...input, positions: Float32Array.from(replacement, (value, index) => value - origin[index % 2]),
      sources: new Uint32Array([1]), targets: new Uint32Array([0]), weights: new Float64Array([7]) };
    graph!.setPointPositions(Float32Array.from(replacement));
    uploadLinks(new Float32Array([1, 0]));
    graph!.render(undefined, 0); graph!.pause();
    await rendered();
    const committed = snapshot();
    const { after } = manual(1);
    return { before, committed, after, replacement };
  },
  dragRow(index, position) {
    const before = snapshot(), frame = nativeFrame();
    if (!gl || !Number.isInteger(index) || index < 0 || index >= frame.nodeCount
      || !position.every(Number.isFinite)) throw new Error("Valid native drag diagnostic row required");
    // Explicit diagnostic upload to the ACTUAL native texture substitutes for
    // the native drag shader write. This is not a pointer-gesture UX test and
    // does not write CPU coordinates into the numerical force textures.
    withWorkspaceForceAtlas2GlState(gl, () => {
      gl!.activeTexture(gl!.TEXTURE0);
      gl!.bindTexture(gl!.TEXTURE_2D, frame.positionTexture);
      gl!.texSubImage2D(gl!.TEXTURE_2D, 0, index % frame.textureSize.width,
        Math.floor(index / frame.textureSize.width), 1, 1, gl!.RGBA, gl!.FLOAT,
        new Float32Array([position[0], position[1], index, 0]));
    });
    simulation.afterDrag(frame, index);
    const { after } = manual(2);
    return { before, after };
  },
  async pauseResume() {
    graph!.pause();
    const before = snapshot();
    await rendered();
    const after = snapshot();
    // Exercise public resume, then pause in the same JS turn so automatic RAF
    // physics cannot contaminate the separately counted manual-step oracle.
    graph!.unpause();
    const resumedRunning = graph!.isSimulationRunning;
    graph!.pause();
    return { before, after, resumedRunning };
  },
  detach() {
    graph!.attachGpuSimulation(null);
    return { simulationDestroys, frameStillAvailable: graph!.getGpuSimulationFrame() !== null, glError: gl!.getError() };
  },
  destroy() {
    if (gl && originalReadPixels) gl.readPixels = originalReadPixels;
    graph?.destroy(); graph = null;
  },
};

void (async () => {
  const container = document.querySelector<HTMLDivElement>("#graph")!;
  graph = new Graph(container, { backgroundColor: "#f8fafc", spaceSize: 4096,
    rescalePositions: false, enableSimulation: false, fitViewOnInit: false,
    transitionDuration: 0, pointSizeScale: 1, scalePointsOnZoom: false,
    linkDefaultWidth: 2, linkVisibilityMinTransparency: 1, renderLinks: true,
    linkVisibilityDistanceRange: [100000, 1000000] });
  await graph.ready;
  graph.setPointPositions(Float32Array.from(initialRendererPositions));
  graph.setPointSizes(new Float32Array([18, 18]));
  graph.setPointColors(new Float32Array([.85, .2, .1, 1, .4, .2, .9, 1]));
  uploadLinks(new Float32Array([0, 1]));
  graph.setLinkColors(new Float32Array([.3, .4, .6, 1]));
  graph.render(undefined, 0); graph.pause();
  await rendered();
  graph.setPinnedPoints([0]);
  graph.attachGpuSimulation(simulation);
  gl = nativeFrame().gl;
  originalReadPixels = gl.readPixels;
  gl.readPixels = function (this: WebGL2RenderingContext, ...args: Parameters<WebGL2RenderingContext["readPixels"]>) {
    if (countingPhysics) readPixelsDuringSteps++;
    return Reflect.apply(originalReadPixels!, this, args);
  } as WebGL2RenderingContext["readPixels"];
  graph.setConfigPartial({ enableSimulation: true }); graph.pause();
  graph.fitView(0, .08, false); graph.pause();
  await rendered();
  window.__forceAtlas2Binding.status = "ready";
})().catch(error => {
  window.__forceAtlas2Binding.status = "error";
  window.__forceAtlas2Binding.error = error instanceof Error ? error.message : String(error);
});

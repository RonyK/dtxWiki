import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { runInNewContext } from "node:vm";
import { createSourceFile, isClassDeclaration, isMethodDeclaration, ModuleKind, ScriptTarget, transpileModule } from "typescript";
import type { GraphConfig } from "../cosmosNative";

import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import { createWorkspaceCosmosAdapter as createProductWorkspaceCosmosAdapter, type WorkspaceCosmosAdapter, type WorkspaceCosmosOptions } from "../cosmosAdapter";
import type { WorkspaceD3LayoutInput, WorkspaceD3LayoutOptions } from "../d3MapLayout";
import type { WorkspaceMapCommunityOutcome } from "../../map/communityJob";
import { buildCosmosSceneBuffers } from "../cosmosScene";
import { buildWorkspaceRelationCollisionBasis, buildWorkspaceRelationForceConfig } from "../relationPhysics";
import { readGraphTokens } from "../../shared/graphTokens";
import { WORKSPACE_MAP_DEFAULT_BUDGET, WORKSPACE_MAP_PC_10K_BUDGET, type WorkspaceMapBudget } from "../../shared/workspaceMapBudget";
import { workspaceContainerIndices, workspaceDirectoryStartPositions } from "../directoryStartPositions";
import * as nativeTracking from "../gpuTrackedPositions";

const { graph, community, createGraph, createNativeGraph } = vi.hoisted(() => {
  let positions = new Float32Array();
  const graph = {
    ready: Promise.resolve(),
    attachPointPicker: vi.fn(),
    getTrackedPointPositionsFrame: vi.fn(() => null),
    get isSimulationRunning() { return false; },
    destroy: vi.fn(),
    create: vi.fn(),
    fitView: vi.fn(),
    getPointPositions: vi.fn(() => positions.slice()),
    getTrackedPointPositionsMap: vi.fn(() => new Map()),
    getZoomLevel: vi.fn(() => 2),
    pause: vi.fn(),
    render: vi.fn(),
    screenToSpacePosition: vi.fn((_position: [number, number]) => [2048, 2048]),
    setClusterPositions: vi.fn(),
    setConfigPartial: vi.fn(),
    setLinkColors: vi.fn(),
    setLinkStyles: vi.fn(),
    setLinkStrength: vi.fn(),
    setLinkWidths: vi.fn(),
    setLinks: vi.fn(),
    setPinnedPoints: vi.fn(),
    setPointClusters: vi.fn(),
    setPointColors: vi.fn(),
    setPointPositions: vi.fn((next: Float32Array) => { positions = next.slice(); }),
    setPointShapes: vi.fn(),
    setPointSizes: vi.fn(),
    setPointCollisionRadii: vi.fn(),
    setImageData: vi.fn(),
    setPointImageIndices: vi.fn(),
    setPointImageSizes: vi.fn(),
    setZoomLevel: vi.fn(),
    setZoomTransformByPointPositions: vi.fn(),
    spaceToScreenPosition: vi.fn((_position: [number, number]) => [10, 10]),
    start: vi.fn(),
    stop: vi.fn(),
    trackPointPositionsByIndices: vi.fn(),
    unpause: vi.fn(),
  };
  const createGraph = vi.fn(function (container: HTMLDivElement, _config: GraphConfig) {
    const canvas = container.querySelector("canvas") ?? container.appendChild(document.createElement("canvas"));
    for (const [field, value] of [["clientWidth", 734], ["clientHeight", 608]] as const) {
      if (!Object.getOwnPropertyDescriptor(canvas, field)) Object.defineProperty(canvas, field, { configurable: true, value });
    }
    return graph;
  });
  return {
    graph, createGraph,
    createNativeGraph: vi.fn(function (container: HTMLDivElement, config: GraphConfig) { return createGraph(container, config); }),
    community: {
      cancel: vi.fn(),
      compute: vi.fn<(...args: unknown[]) => Promise<WorkspaceMapCommunityOutcome>>(async () => ({ kind: "cancelled", identityKey: "test" })),
      destroy: vi.fn(),
    },
  };
});

const cpu = vi.hoisted(() => ({
  instances: [] as Array<{
    input: WorkspaceD3LayoutInput; options: WorkspaceD3LayoutOptions;
    start: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn>;
    setPositions: ReturnType<typeof vi.fn>; destroy: ReturnType<typeof vi.fn>; isRunning: () => boolean;
    setReducedMotion: ReturnType<typeof vi.fn>;
  }>,
  create: vi.fn(),
}));
vi.mock("../d3MapLayout", () => ({ createWorkspaceD3Layout: cpu.create }));

// Keep the pre-existing SDK force/lifecycle regressions on the explicitly
// admitted PC native branch. Default 2K is tested separately below; a native
// mock must not pretend that an official CPU Worker calls graph.start().
function createWorkspaceCosmosAdapter(container: HTMLDivElement, input: WorkspaceMapColumns, options: WorkspaceCosmosOptions) {
  if (options.budget !== undefined) {
    return createProductWorkspaceCosmosAdapter(container, input, options);
  }
  return createProductWorkspaceCosmosAdapter(container, input, { ...options, budget: WORKSPACE_MAP_PC_10K_BUDGET,
    initialCoordinates: options.initialCoordinates ? { ...options.initialCoordinates,
      budget: options.initialCoordinates.budget ?? WORKSPACE_MAP_PC_10K_BUDGET } : options.initialCoordinates });
}

// Execute the installed SDK's transition state machine, not a hand-written
// approximation of render() calls. Its source map ships with the pinned SDK.
const sdkRequire = createRequire(import.meta.url);
const sdkMap = JSON.parse(readFileSync(`${sdkRequire.resolve("@cosmos.gl/graph")}.map`, "utf8")) as {
  sources: string[]; sourcesContent: string[];
};
function sdkSource(path: string): string {
  const source = sdkMap.sourcesContent[sdkMap.sources.indexOf(path)];
  if (!source) throw new Error(`Installed Cosmos source missing: ${path}`);
  return source;
}
type SdkTransition = {
  progress: number;
  isActive: boolean;
  isActiveFor(property: string): boolean;
  queue(property: string): void;
  setDurationOverride(duration?: number): void;
  start(): void;
  step(): void;
  end(interrupted: boolean): void;
  dequeue(property: string): void;
};
const sdkExports: { Transition?: new (config: GraphConfig) => SdkTransition } = {};
runInNewContext(transpileModule(sdkSource("../src/modules/Transition/index.ts"), {
  compilerOptions: { module: ModuleKind.CommonJS },
}).outputText, { exports: sdkExports, require: sdkRequire, performance });
if (!sdkExports.Transition) throw new Error("Installed Cosmos Transition export missing");
const InstalledTransition = sdkExports.Transition;

type SdkSimulationState = {
  alpha: number;
  isSimulationRunning: boolean;
  simulationProgress: number;
};
type SdkGraphLifecycle = {
  config: GraphConfig;
  store: SdkSimulationState;
  transition: SdkTransition;
  start(alpha?: number): void;
  pause(): void;
  stop(): void;
  end(): void;
  applyEnableSimulationConfigChange(previous: GraphConfig): void;
};
// Execute the pinned SDK's permission side effects too: false -> true wakes
// physics at alpha=1 even if the adapter never calls start(). A call-count-only
// mock cannot detect a paused scene being resumed by setConfigPartial().
const sdkGraphAst = createSourceFile("cosmos-sdk.ts", sdkSource("../src/index.ts"), ScriptTarget.Latest, true);
const sdkGraphClass = sdkGraphAst.statements.find((statement) => isClassDeclaration(statement)
  && statement.name?.text === "Graph");
if (!sdkGraphClass || !isClassDeclaration(sdkGraphClass)) throw new Error("Installed Cosmos Graph class missing");
const lifecycleMethods = ["start", "pause", "stop", "end", "applyEnableSimulationConfigChange"].map((name) => {
  const method = sdkGraphClass.members.find((member) => isMethodDeclaration(member)
    && member.name.getText(sdkGraphAst) === name);
  if (!method) throw new Error(`Installed Cosmos lifecycle method missing: ${name}`);
  return method.getText(sdkGraphAst);
});
const sdkLifecycleExports: { GraphLifecycle?: new () => SdkGraphLifecycle } = {};
runInNewContext(transpileModule(`class GraphLifecycle { ${lifecycleMethods.join("\n")} }
exports.GraphLifecycle = GraphLifecycle;`, {
  compilerOptions: { module: ModuleKind.CommonJS },
}).outputText, {
  exports: sdkLifecycleExports,
  TransitionProperty: { Positions: "positions" },
});
if (!sdkLifecycleExports.GraphLifecycle) throw new Error("Installed Cosmos lifecycle export missing");
const InstalledGraphLifecycle = sdkLifecycleExports.GraphLifecycle;

type SdkCamera = { x: number; y: number; k: number };
type SdkZoom = {
  eventTransform: SdkCamera;
  behavior: { on(name: string): (event: { transform: SdkCamera; sourceEvent: Event | null }) => void };
  getTransform(positions: Float32Array, scale?: number, padding?: number): SdkCamera;
  convertScreenToSpacePosition(position: [number, number]): [number, number];
};
const sdkZoomExports: { Zoom?: new (store: unknown, config: GraphConfig) => SdkZoom } = {};
runInNewContext(transpileModule(sdkSource("../src/modules/Zoom/index.ts"), {
  compilerOptions: { module: ModuleKind.CommonJS },
}).outputText, { exports: sdkZoomExports, require: (name: string) => name === "@/graph/helper"
  ? { clamp: (value: number, minimum: number, maximum: number) => Math.min(Math.max(value, minimum), maximum) }
  : sdkRequire(name) });
if (!sdkZoomExports.Zoom) throw new Error("Installed Cosmos Zoom export missing");
const InstalledZoom = sdkZoomExports.Zoom;

function installedSdkCamera(width = 734, height = 608) {
  const space = 4096;
  const container = document.createElement("div"), canvas = document.createElement("canvas");
  Object.defineProperties(canvas, { clientWidth: { value: width }, clientHeight: { value: height } });
  container.appendChild(canvas); document.body.appendChild(container);
  const zoom = new InstalledZoom({ screenSize: [width, height], adjustedSpaceSize: space,
    scaleX: (x: number) => x + (width - space) / 2,
    scaleY: (y: number) => (height + space) / 2 - y }, {
    onZoomEnd: (...args) => createGraph.mock.calls.at(-1)?.[1].onZoomEnd?.(...args),
  });
  graph.screenToSpacePosition.mockImplementation((point) => zoom.convertScreenToSpacePosition(point));
  graph.getZoomLevel.mockImplementation(() => zoom.eventTransform.k);
  graph.setZoomTransformByPointPositions.mockImplementation((positions, _duration, scale) => {
    zoom.eventTransform = zoom.getTransform(positions, scale, 0);
  });
  return { container, zoom,
    end: (sourceEvent: Event | null = null) => zoom.behavior.on("end")({ transform: zoom.eventTransform, sourceEvent }) };
}

function installedSdkDrag(config: GraphConfig, index: number) {
  const handlers = new Map<string, (event: unknown) => void>();
  let subject: (event: { x: number; y: number }) => unknown = () => undefined;
  const behavior = {
    subject(callback: typeof subject) { subject = callback; return behavior; },
    on(name: string, callback: (event: unknown) => void) { handlers.set(name, callback); return behavior; },
  };
  const exports: { Drag?: new (store: unknown, config: GraphConfig, transition: unknown) => unknown } = {};
  runInNewContext(transpileModule(sdkSource("../src/modules/Drag/index.ts"), {
    compilerOptions: { module: ModuleKind.CommonJS },
  }).outputText, { exports, require: (name: string) => name === "d3-drag"
    ? { drag: () => behavior } : { TransitionProperty: { Positions: "positions", PointSizes: "pointSizes" } } });
  if (!exports.Drag) throw new Error("Installed Cosmos Drag export missing");
  new exports.Drag({ hoveredPoint: { index }, isSpaceKeyPressed: false }, config, { isActiveFor: () => false });
  const event = { x: 20, y: 30, subject: subject({ x: 20, y: 30 }), sourceEvent: { type: "mousedown" } };
  return { event, start: (type = "mousedown") => { event.sourceEvent = { type }; handlers.get("start")!(event); },
    move: (x: number, y: number) => { event.x = x; event.y = y; handlers.get("drag")!(event); },
    end: (type = "mouseup") => { event.sourceEvent = { type }; handlers.get("end")!(event); } };
}

// Default asynchronous GPU title rows: a read completes after the task that
// requested it, with the rows a test placed here. Tests that inspect the tracker
// install their own controller; the coordinate-upload tests restore the real one.
const trackedRows = new Map<number, [number, number]>();
function setTrackedRows(rows: Iterable<readonly [number, [number, number]]>) {
  trackedRows.clear();
  for (const [index, point] of rows) trackedRows.set(index, point);
}
const trackedDelivery = () => new Promise<void>(resolve => window.setTimeout(resolve, 0));
function installAsyncTrackedRows() {
  vi.spyOn(nativeTracking, "createWorkspaceTrackedPositions").mockImplementation(({ onPositions }) => {
    let generation = 0, destroyed = false;
    return {
      request: vi.fn(() => {
        if (destroyed) return false;
        const issued = generation;
        queueMicrotask(() => { if (!destroyed && issued === generation) onPositions(new Map(trackedRows)); });
        return true;
      }),
      invalidate: vi.fn(() => { generation += 1; }),
      destroy: vi.fn(() => { destroyed = true; generation += 1; }),
    };
  });
}

const defaultMockImplementations = {
  positions: graph.getPointPositions.getMockImplementation()!,
  setPositions: graph.setPointPositions.getMockImplementation()!,
  createGraph: createGraph.getMockImplementation()!,
};

function installedSdkLifecycle() {
  let clock = 100;
  vi.spyOn(performance, "now").mockImplementation(() => clock);
  let config: GraphConfig = { transitionDuration: 0, transitionEasing: "cubic-in-out" };
  let transition = new InstalledTransition(config);
  let positions = new Float32Array();
  let source = positions;
  let target = positions;
  let pendingPositions = false;
  let inputColors = new Float32Array();
  let committedColors = inputColors;
  let uploadedColors = inputColors;
  const interruptions: boolean[] = [];
  const store: SdkSimulationState = { alpha: 0, isSimulationRunning: false, simulationProgress: 0 };
  const lifecycle = Object.assign(new InstalledGraphLifecycle(), {
    _isDestroyed: false,
    config,
    store,
    transition,
    graph: { pointsNumber: 0 },
    points: { ensureSimulationResources: vi.fn() },
    ensureDevice: () => false,
    ensureSimulationModules: vi.fn(),
    destroySimulationModules: vi.fn(),
    create: vi.fn(),
    initPrograms: vi.fn(),
    requestRender: vi.fn(),
  });
  createGraph.mockImplementation(function (_container, nextConfig) {
    defaultMockImplementations.createGraph(_container, nextConfig);
    config = { ...config, ...nextConfig, onTransitionEnd(interrupted) {
      interruptions.push(interrupted);
      nextConfig.onTransitionEnd?.(interrupted);
    } };
    transition = new InstalledTransition(config);
    lifecycle.config = config;
    lifecycle.transition = transition;
    store.alpha = 1;
    store.isSimulationRunning = Boolean(config.enableSimulation);
    return graph;
  });
  graph.setConfigPartial.mockImplementation((next) => {
    const previous = { ...config };
    Object.assign(config, next);
    lifecycle.applyEnableSimulationConfigChange(previous);
  });
  graph.start.mockImplementation((alpha) => lifecycle.start(alpha));
  graph.pause.mockImplementation(() => lifecycle.pause());
  graph.stop.mockImplementation(() => lifecycle.stop());
  vi.spyOn(graph, "isSimulationRunning", "get").mockImplementation(() => store.isSimulationRunning);
  graph.setPointPositions.mockImplementation((next) => {
    target = next.slice(); pendingPositions = true; transition.queue("positions");
    lifecycle.graph.pointsNumber = next.length / 2;
  });
  graph.getPointPositions.mockImplementation(() => positions.slice());
  graph.setLinkColors.mockImplementation((next) => { inputColors = next; transition.queue("linkColors"); });
  graph.create.mockImplementation(() => {
    // SDK create() uploads GraphData.linkColors, not its uncommitted input.
    uploadedColors = committedColors.slice();
  });
  graph.render.mockImplementation((_alpha, duration) => {
    committedColors = inputColors;
    if (pendingPositions) {
      source = positions.slice();
      if (!positions.length || duration === 0) positions = target.slice();
      pendingPositions = false;
    }
    graph.create();
    transition.setDurationOverride(duration);
    transition.start();
  });
  return {
    interruptions,
    activePositions: () => transition.isActiveFor("positions"),
    running: () => store.isSimulationRunning,
    end: () => lifecycle.end(),
    alpha: () => store.alpha,
    simulationEnabled: () => config.enableSimulation,
    colors: () => uploadedColors.slice(),
    advance(ms: number) {
      clock += ms;
      const interpolate = transition.isActiveFor("positions");
      transition.step();
      if (interpolate) positions = target.map((value, index) =>
        source[index] + (value - source[index]) * transition.progress);
    },
  };
}

vi.mock("../cosmosNative", () => ({ NativeGraph: createNativeGraph }));
vi.mock("../webglSupport", () => ({ detectWorkspaceMapWebGL: () => ({ supported: true }) }));
vi.mock("../../map/communityJob", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../map/communityJob")>()),
  createWorkspaceMapCommunityJob: () => community,
}));
function columns(): WorkspaceMapColumns {
  return {
    identity: { workspaceId: "workspace-a", actorId: "actor-a", surface: "workspace", selectionKey: "links", visibleFingerprint: "visible-a" },
    nodeIds: ["a", "b", "c"],
    nodeKeys: ["context:a", "context:b", "context:c"],
    nodeLabels: ["Alpha", "Beta", "Gamma"],
    nodeFolderKeys: [null, null, null],
    nodeKinds: new Uint8Array(3),
    nodeSizes: Float32Array.from([3, 3, 3]),
    edgeIds: ["ab", "bc"],
    edgeSources: Uint32Array.from([0, 1]),
    edgeTargets: Uint32Array.from([1, 2]),
    edgeKinds: new Uint8Array(2),
    edgeCounts: Uint32Array.from([1, 1]),
    edgeWeights: Float64Array.from([1, 1]),
    edgeCrossFolder: new Uint8Array(2),
  };
}

function sizedColumns(nodeCount: number, edgeCount = 2): WorkspaceMapColumns {
  return { ...columns(),
    nodeIds: Array.from({ length: nodeCount }, (_, index) => `node:${index}`),
    nodeKeys: Array.from({ length: nodeCount }, (_, index) => `context:${String(index).padStart(5, "0")}`),
    nodeLabels: new Array<string>(nodeCount).fill("Document"),
    nodeFolderKeys: new Array<null>(nodeCount).fill(null),
    nodeKinds: new Uint8Array(nodeCount), nodeSizes: new Float32Array(nodeCount).fill(3),
    edgeIds: Array.from({ length: edgeCount }, (_, index) => `edge:${index}`),
    edgeSources: Uint32Array.from({ length: edgeCount }, (_, index) => index % nodeCount),
    edgeTargets: Uint32Array.from({ length: edgeCount }, (_, index) => (index + 1) % nodeCount),
    edgeKinds: new Uint8Array(edgeCount), edgeCounts: new Uint32Array(edgeCount).fill(1),
    edgeWeights: new Float64Array(edgeCount).fill(1), edgeCrossFolder: new Uint8Array(edgeCount),
  };
}

function nativeContext(textureLimit = 16_384): WebGL2RenderingContext {
  return {
    MAX_TEXTURE_SIZE: 0x0D33,
    ALIASED_POINT_SIZE_RANGE: 0x846D,
    getParameter: (parameter: number) => parameter === 0x0D33 ? textureLimit
      : parameter === 0x846D ? Float32Array.of(1, 1024) : null,
  } as unknown as WebGL2RenderingContext;
}

let adapters: WorkspaceCosmosAdapter[] = [];
beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(nativeContext());
  graph.ready = Promise.resolve();
  graph.render.mockReset();
  graph.create.mockReset();
  graph.setLinkColors.mockReset();
  graph.setConfigPartial.mockReset();
  graph.start.mockReset();
  graph.pause.mockReset();
  graph.stop.mockReset();
  graph.screenToSpacePosition.mockImplementation(() => [2048, 2048]);
  // A completed SDK camera transition emits ZoomEnd. Individual async camera
  // tests override these defaults to hold the real pending-ownership boundary.
  const finishCamera = () => createGraph.mock.calls.at(-1)?.[1].onZoomEnd?.(
    { transform: { k: graph.getZoomLevel() } } as Parameters<NonNullable<GraphConfig["onZoomEnd"]>>[0], false,
  );
  graph.fitView.mockReset(); graph.fitView.mockImplementation(finishCamera);
  graph.setZoomTransformByPointPositions.mockReset();
  graph.setZoomTransformByPointPositions.mockImplementation(finishCamera);
  graph.getPointPositions.mockImplementation(defaultMockImplementations.positions);
  graph.setPointPositions.mockImplementation(defaultMockImplementations.setPositions);
  createGraph.mockImplementation(defaultMockImplementations.createGraph);
  graph.getZoomLevel.mockReturnValue(2);
  graph.spaceToScreenPosition.mockImplementation(() => [10, 10]);
  trackedRows.clear(); installAsyncTrackedRows();
  adapters = [];
  cpu.instances = [];
  cpu.create.mockImplementation((input: WorkspaceD3LayoutInput, options: WorkspaceD3LayoutOptions) => {
    let running = false, reducedMotion = input.reducedMotion;
    const instance = { input, options,
      start: vi.fn(() => { running = !reducedMotion; }), stop: vi.fn(() => { running = false; }),
      setPositions: vi.fn(), destroy: vi.fn(() => { running = false; }), isRunning: () => running,
      setReducedMotion: vi.fn((enabled: boolean) => { reducedMotion = enabled; if (enabled) running = false; }) };
    cpu.instances.push(instance);
    return instance;
  });
  community.compute.mockImplementation(async (input) => ({ kind: "error",
    identityKey: communityResult(input as WorkspaceMapColumns).identityKey }));
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
    window.setTimeout(() => callback(performance.now()), 0));
});
afterEach(() => {
  for (const adapter of adapters) adapter.destroy();
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mount(input = columns(), reducedMotion = true, onSelect = vi.fn(), initialPaused = false,
  onPin = vi.fn(), budget?: WorkspaceMapBudget) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const adapter = createWorkspaceCosmosAdapter(container, input, {
    budget,
    forcedColors: false,
    reducedMotion,
    initialPaused,
    callbacks: { onHover: vi.fn(), onSelect, onPin },
  });
  adapters.push(adapter);
  await adapter.whenSettled();
  return adapter;
}

it.each([2048, 4096])("chooses the supported native space before upload at texture limit %i and keeps 2k snapshots canonical", async limit => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(nativeContext(limit));
  const input = sizedColumns(2000);
  const adapter = await mount(input);
  const spaceSize = limit / 2;
  const spaceCall = graph.setConfigPartial.mock.calls.findIndex(([config]) => config.spaceSize === spaceSize);
  expect(spaceCall).toBeGreaterThanOrEqual(0);
  expect(graph.setConfigPartial.mock.invocationCallOrder[spaceCall])
    .toBeLessThan(graph.setPointPositions.mock.invocationCallOrder[0]);
  const original = buildCosmosSceneBuffers(input, readGraphTokens(document.body, "light")).positions;
  const native = graph.getPointPositions();
  expect([...native]).toEqual([...original].map(value => value * spaceSize / 4096));
  expect(native.every(value => value > 0 && value < spaceSize)).toBe(true);
  expect(adapter.captureCoordinates()?.positions).toEqual(original);
});

it("keeps low-limit pins, drag callbacks, camera and cross-capability remounts in canonical units", async () => {
  const context = vi.spyOn(HTMLCanvasElement.prototype, "getContext");
  context.mockReturnValue(nativeContext(4096));
  const input = columns(), onPin = vi.fn();
  const adapter = await mount(input, true, vi.fn(), true, onPin);
  expect(adapter.pin("a", true, { x: 1234, y: 2345 })).toEqual({ x: 1234, y: 2345 });
  expect([...graph.getPointPositions().slice(0, 2)]).toEqual([617, 1172.5]);
  const config = createGraph.mock.calls.at(-1)![1];
  config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
  const drag = installedSdkDrag(config, 0);
  drag.start(); drag.move(30, 50); drag.end();
  expect(onPin).toHaveBeenLastCalledWith("a", { x: 1234, y: 2345 });
  graph.screenToSpacePosition.mockReturnValue([100, 200]);
  expect(adapter.getViewport()).toEqual({ zoom: 1, pan: { x: 200, y: 400 } });
  adapter.zoomBy(3);
  expect(graph.setZoomLevel).toHaveBeenLastCalledWith(6, 0, false);
  const saved = { zoom: .5, pan: { x: 300, y: 400 } };
  adapter.setViewport(saved);
  expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(Float32Array.from([150, 200]), 0, 1, 0, false);
  const before = adapter.captureCoordinates()!;
  const uploads = graph.setPointPositions.mock.calls.length;
  adapter.replaceColumns({ ...input, nodeLabels: ["Renamed", "Beta", "Gamma"] });
  expect(graph.setPointPositions).toHaveBeenCalledTimes(uploads);
  expect(adapter.captureCoordinates()!.positions).toEqual(before.positions);
  adapter.destroy();
  context.mockReturnValue(nativeContext());
  const container = document.createElement("div"); document.body.appendChild(container);
  const restored = createWorkspaceCosmosAdapter(container, input, {
    forcedColors: false, reducedMotion: true, initialPaused: true,
    initialCoordinates: before, initialViewport: saved,
    callbacks: { onHover: vi.fn(), onSelect: vi.fn() },
  });
  adapters.push(restored); await restored.whenSettled();
  expect(graph.setPointPositions).toHaveBeenLastCalledWith(before.positions);
  expect(restored.captureCoordinates()!.positions).toEqual(before.positions);
  expect(restored.captureCoordinates()!.collisionBasis).toEqual(before.collisionBasis);
  expect(restored.captureCoordinates()!.forceBasis).toEqual(before.forceBasis);
  expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(Float32Array.from([300, 400]), 0, .5, 0, false);
});

function freshCollisionBasis(input = columns(), width = 734, height = 608) {
  const buffers = buildCosmosSceneBuffers(input, readGraphTokens(document.body, "light"));
  return buildWorkspaceRelationCollisionBasis(buffers.positions, buffers.pointSizes, width, height)!;
}

describe("default 2K D3 Worker with Cosmos GPU drawing", () => {
  async function mountCpu(input = columns(), overrides: Partial<WorkspaceCosmosOptions> = {}) {
    const container = document.createElement("div"); document.body.appendChild(container);
    const onPin = vi.fn(), onError = vi.fn();
    const adapter = createProductWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn(), onPin, onRuntimeError: onError }, ...overrides });
    adapters.push(adapter); await adapter.whenSettled();
    return { adapter, container, onPin, onError };
  }

  it("uses the original 2K weighted rows and canonical glyph radii without enabling GPU forces", async () => {
    const sdk = installedSdkLifecycle(), input = sizedColumns(2000, 6000);
    input.edgeWeights[0] = 8;
    let complete!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValueOnce(new Promise(resolve => { complete = resolve; }));
    const { adapter, container } = await mountCpu(input);
    // Community metadata may arrive after renderer readiness. Its old layout
    // hints must neither replace D3 coordinates nor leave the solver waiting.
    expect(cpu.instances).toHaveLength(0);
    const firstRows = graph.getPointPositions();
    complete({ ...communityResult(input), initialPositions: new Float32Array(4000).fill(9999),
      communityIds: new Uint32Array(2000) });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(graph.getPointPositions()).toEqual(firstRows);
    const worker = cpu.instances[0];
    expect(worker.isRunning()).toBe(true); expect(sdk.running()).toBe(false);
    expect(graph.start).not.toHaveBeenCalled();
    expect(graph.setConfigPartial.mock.calls.every(([config]) => config.enableSimulation !== true)).toBe(true);
    expect(worker.input.columns).toBe(input);
    expect(worker.input.diameters).toHaveLength(2000);
    expect(worker.input.shapes).toHaveLength(2000);
    expect(worker.input.pins).toEqual([]);
    expect(container.dataset.mapPhysics).toBe("d3-worker");
    expect(container.dataset.mapLayoutRunning).toBe("true"); expect(container.dataset.mapLayoutTicks).toBe("0");
    graph.setZoomTransformByPointPositions.mockClear();
    adapter.resumeLayout(false);
    await new Promise(resolve => setTimeout(resolve, 5));
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
  });

  it.each(["reduced-motion", "inactive", "paused-before-first-frame"] as const)("fits the first solved %s frame once and preserves solved snapshot ownership", async startup => {
    const { adapter } = await mountCpu(columns(), { reducedMotion: startup === "reduced-motion", initialPaused: startup === "inactive" });
    if (startup === "inactive") {
      expect(cpu.instances).toHaveLength(0);
      adapter.resumeLayout();
    } else if (startup === "paused-before-first-frame") {
      adapter.stopLayout(); adapter.resumeLayout(false);
    }
    const worker = cpu.instances[0], solved = Float32Array.from([100, 200, 300, 400, 500, 600]);
    // Saved-pin reconciliation is not a camera gesture and cannot spend the
    // first solved Fit or wake a completed remount.
    adapter.pin("a", true, { x: 100, y: 200 }, { reconcile: true });
    expect(worker.setPositions).toHaveBeenLastCalledWith(expect.any(Float32Array), [0], { reheat: false });
    graph.setZoomTransformByPointPositions.mockClear(); graph.fitView.mockClear();
    worker.options.onPositions(solved);
    worker.options.onState({ ready: true, running: false, phase: "cooled" });
    await new Promise(resolve => setTimeout(resolve, 5));
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledExactlyOnceWith(solved, 0, undefined, .08, false);
    expect(graph.fitView).not.toHaveBeenCalled();
    const snapshot = adapter.captureCoordinates()!;
    expect(snapshot.positions).toEqual(solved); expect(snapshot.layoutReady).toBe(true);
    worker.options.onState({ ready: true, running: false, phase: "cooled" });
    await new Promise(resolve => setTimeout(resolve, 5));
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledTimes(1);
    adapter.destroy();
    const restored = await mountCpu(columns(), { initialCoordinates: snapshot, initialPaused: true });
    expect(restored.adapter.captureCoordinates()!.positions).toEqual(solved);
    restored.adapter.pin("a", true, { x: 100, y: 200 }, { reconcile: true });
    restored.adapter.resumeLayout();
    expect(cpu.instances.at(-1)!.input.restoredSolved).toBe(true);
    expect(cpu.instances.at(-1)!.input.pins).toEqual([0]);
    expect(cpu.instances.at(-1)!.start).toHaveBeenLastCalledWith(true);
  });

  it("waits for solved D3 rows before a pending navigation can take the camera", async () => {
    const { adapter } = await mountCpu(); const worker = cpu.instances[0], onFinished = vi.fn();
    const controller = new AbortController();
    graph.setZoomTransformByPointPositions.mockClear(); graph.fitView.mockClear();
    adapter.revealAfterLayout("b", controller.signal, () => true, onFinished);
    await new Promise(resolve => setTimeout(resolve, 5));
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    expect(onFinished).not.toHaveBeenCalled();
    const solved = Float32Array.from([100, 200, 300, 400, 500, 600]);
    worker.options.onPositions(solved);
    worker.options.onState({ ready: true, running: false, phase: "cooled" });
    await new Promise(resolve => setTimeout(resolve, 5));
    expect(graph.setZoomTransformByPointPositions.mock.lastCall?.[0]).toEqual(Float32Array.from([300, 400]));
    expect(onFinished).toHaveBeenCalledOnce();
  });

  it("uploads CPU frames without GPU readback, topology replacement or user-camera movement", async () => {
    const { adapter, container } = await mountCpu(columns(), { initialViewport: { zoom: 2, pan: { x: 50, y: 60 } },
      relationLabel: () => "Link" });
    const frame = Float32Array.from([700, 800, 900, 1000, 1100, 1200]);
    graph.getPointPositions.mockClear(); graph.setLinks.mockClear(); graph.fitView.mockClear();
    graph.setZoomTransformByPointPositions.mockClear(); graph.setZoomLevel.mockClear();
    cpu.instances[0].options.onPositions(frame);
    expect(graph.setPointPositions).toHaveBeenLastCalledWith(frame);
    expect(graph.render).toHaveBeenLastCalledWith(undefined, 0);
    expect(graph.getPointPositions).not.toHaveBeenCalled(); expect(graph.setLinks).not.toHaveBeenCalled();
    expect(graph.fitView).not.toHaveBeenCalled(); expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    expect(graph.setZoomLevel).not.toHaveBeenCalled(); expect(container.dataset.mapLayoutTicks).toBe("1");
    expect(adapter.captureCoordinates()!.positions).toEqual(frame);
    // A native pick resolves asynchronously and can name a link with no rows in this frame.
    expect(() => createGraph.mock.calls.at(-1)![1].onLinkMouseOver?.(99)).not.toThrow();
    expect(container.querySelector<HTMLElement>(".graph-hover-chip")!.hidden).toBe(true);
  });

  it("publishes titles in each CPU frame, under the live camera and at the dragged pointer", async () => {
    graph.spaceToScreenPosition.mockImplementation(point => point);
    graph.screenToSpacePosition.mockImplementation(point => point);
    const { adapter, container } = await mountCpu(), worker = cpu.instances[0];
    const config = createGraph.mock.calls.at(-1)![1];
    const title = () => container.querySelector<HTMLElement>('[data-workspace-map-labels] span[data-node-id="a"]')!.dataset;
    // No heartbeat runs between these synchronous steps: each source publishes itself.
    worker.options.onPositions(Float32Array.from([300, 400, 500, 600, 700, 800]));
    expect(title()).toMatchObject({ pointX: "300", pointY: "400" });
    // Zoom while the layout runs reprojects the current frame, not an emptied cache.
    graph.spaceToScreenPosition.mockImplementation(([x, y]) => [x * 2, y * 2]);
    config.onZoom?.({ transform: { k: 4 } } as Parameters<NonNullable<GraphConfig["onZoom"]>>[0], false);
    expect(title()).toMatchObject({ pointX: "600", pointY: "800" });
    // A drag stops the worker; the title follows the pointer, then the committed row.
    graph.spaceToScreenPosition.mockImplementation(point => point);
    config.onPointMouseOver?.(0, [300, 400], undefined, false, false);
    const drag = installedSdkDrag(config, 0); drag.start(); drag.move(340, 420);
    expect(worker.isRunning()).toBe(false);
    expect(title()).toMatchObject({ pointX: "340", pointY: "420" });
    drag.move(360, 430); drag.end();
    expect(adapter.captureCoordinates()!.positions.slice(0, 2)).toEqual(Float32Array.from([360, 430]));
    // The released title follows the resumed worker again, not the last pointer.
    expect(worker.isRunning()).toBe(true);
    worker.options.onPositions(Float32Array.from([380, 440, 500, 600, 700, 800]));
    expect(title()).toMatchObject({ pointX: "380", pointY: "440" });
  });

  it("starts every title fade at the opacity on screen when an entering or leaving fade is interrupted", async () => {
    // jsdom has no Web Animations: a fake whose progress the test sets.
    type FakeAnimation = { element: Element; keyframes: Keyframe[]; progress: number;
      cancel: ReturnType<typeof vi.fn>; onfinish: (() => void) | null };
    const animations: FakeAnimation[] = [];
    Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true,
      value(this: Element, keyframes: Keyframe[]) {
        const animation: FakeAnimation & { effect: object } = { element: this, keyframes, progress: 0, cancel: vi.fn(),
          onfinish: null, effect: { getComputedTiming: () => ({ progress: animation.progress }) } };
        animations.push(animation);
        return animation;
      } });
    vi.spyOn(HTMLDivElement.prototype, "getBoundingClientRect").mockReturnValue(
      { x: 0, y: 0, top: 0, left: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) });
    try {
      graph.spaceToScreenPosition.mockImplementation(point => point);
      const { container } = await mountCpu(), worker = cpu.instances[0];
      const label = container.querySelector<HTMLElement>('[data-workspace-map-labels] span[data-node-id="a"]')!;
      const frame = (x: number) => worker.options.onPositions(Float32Array.from([x, 100, 400, 300, 600, 400]));
      const fades = () => animations.filter(animation => animation.element === label);
      const opacities = (animation: FakeAnimation) => animation.keyframes.map(keyframe => keyframe.opacity);
      frame(-500); expect(label.style.display).toBe("none"); animations.length = 0;
      frame(100);
      expect(fades().map(opacities)).toEqual([[0, 1]]);
      // Hidden a quarter of the way in: fade out from 0.25, not from 1.
      fades()[0].progress = .25; frame(-500);
      expect(fades()[0].cancel).toHaveBeenCalled(); expect(opacities(fades()[1])).toEqual([.25, 0]);
      // Shown again halfway out: fade back in from 0.125, never from 0.
      fades()[1].progress = .5; frame(100);
      expect(fades()[1].cancel).toHaveBeenCalled(); expect(opacities(fades()[2])).toEqual([.125, 1]);
      // An entering title that moves keeps its fade.
      frame(110); expect(fades()).toHaveLength(3);
      fades()[2].onfinish?.(); frame(-500);
      expect(opacities(fades()[3])).toEqual([1, 0]); expect(label.style.display).toBe("block");
      fades()[3].onfinish?.(); expect(label.style.display).toBe("none");
    } finally { delete (HTMLElement.prototype as { animate?: unknown }).animate; }
  });

  it("reports arranging, live, pause and reduced-motion from current worker ownership", async () => {
    const onStatus = vi.fn();
    const { adapter, container } = await mountCpu(columns(), { callbacks: { onHover: vi.fn(), onSelect: vi.fn(),
      onLayoutStatusChange: onStatus } });
    const worker = cpu.instances[0];
    expect(container.dataset.mapLayoutPhase).toBe("arranging");
    worker.options.onState({ ready: true, running: true, phase: "running" });
    expect(container.dataset.mapLayoutPhase).toBe("live");
    expect(onStatus.mock.lastCall?.[0]).toBe("live");
    adapter.stopLayout(); expect(onStatus.mock.lastCall?.[0]).toBe("paused");
    adapter.setReducedMotion(true); expect(onStatus.mock.lastCall?.[0]).toBe("motion-off");
    adapter.destroy(); onStatus.mockClear();
    worker.options.onState({ ready: true, running: false, phase: "cooled" });
    expect(onStatus).not.toHaveBeenCalled();
  });

  it.each(["pause", "reduced"] as const)("rejects CPU output during %s and resumes without losing pins", async mode => {
    const { adapter, container } = await mountCpu(); const worker = cpu.instances[0];
    worker.options.onPositions(adapter.captureCoordinates()!.positions);
    worker.options.onState({ ready: true, running: true, phase: "running" });
    adapter.pin("a", true, { x: 77, y: 88 });
    expect(worker.setPositions).toHaveBeenLastCalledWith(expect.any(Float32Array), [0]);
    if (mode === "pause") adapter.stopLayout(); else adapter.setReducedMotion(true);
    expect(worker.isRunning()).toBe(false); expect(container.dataset.mapLayoutRunning).toBe("false");
    const positions = graph.getPointPositions(); graph.setPointPositions.mockClear();
    worker.options.onPositions(new Float32Array(6).fill(999));
    expect(graph.setPointPositions).not.toHaveBeenCalled(); expect(graph.getPointPositions()).toEqual(positions);
    if (mode === "pause") adapter.resumeLayout(); else adapter.setReducedMotion(false);
    expect(worker.isRunning()).toBe(true); expect(container.dataset.mapLayoutRunning).toBe("true");
    expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([0]); expect(graph.start).not.toHaveBeenCalled();
  });

  it("stops CPU at native drag start, rejects frames during drag, commits the final event and exact pin", async () => {
    const { adapter, onPin } = await mountCpu(); const worker = cpu.instances[0];
    const config = createGraph.mock.calls.at(-1)![1]; config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    const drag = installedSdkDrag(config, 0); drag.start();
    expect(worker.isRunning()).toBe(false); graph.setPointPositions.mockClear();
    worker.options.onPositions(new Float32Array(6).fill(999));
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    graph.screenToSpacePosition.mockReturnValue([1234, 2345]); drag.move(30, 50); drag.end();
    expect(graph.screenToSpacePosition).toHaveBeenLastCalledWith([30, 50]);
    expect(onPin).toHaveBeenLastCalledWith("a", { x: 1234, y: 2345 });
    expect(worker.setPositions).toHaveBeenLastCalledWith(expect.any(Float32Array), [0]);
    expect([...worker.setPositions.mock.lastCall![0].slice(0, 2)]).toEqual([1234, 2345]);
    expect(worker.isRunning()).toBe(true); expect(adapter.captureCoordinates()!.positions.slice(0, 2))
      .toEqual(Float32Array.from([1234, 2345]));
  });

  it("stationary touch is not a pin and restarting after a tap preserves the current CPU scene", async () => {
    const { onPin } = await mountCpu(); const worker = cpu.instances[0];
    const config = createGraph.mock.calls.at(-1)![1]; config.onPointMouseOver?.(1, [0, 0], undefined, false, false);
    const drag = installedSdkDrag(config, 1); drag.start("touchstart"); drag.end("touchend");
    expect(onPin).not.toHaveBeenCalled(); expect(worker.isRunning()).toBe(true);
    expect(worker.start).toHaveBeenLastCalledWith(false);
    expect(worker.setPositions).not.toHaveBeenCalled();
  });

  it("keeps the current Worker through metadata updates but rejects old output after authority replacement", async () => {
    const input = columns(), { adapter } = await mountCpu(input), original = cpu.instances[0];
    adapter.replaceColumns({ ...input, nodeLabels: ["Renamed", "Beta", "Gamma"] });
    expect(cpu.instances).toHaveLength(1); expect(original.destroy).not.toHaveBeenCalled();
    const next = { ...input, identity: { ...input.identity, actorId: "actor-b", visibleFingerprint: "visible-b" } };
    adapter.replaceColumns(next); await new Promise(done => setTimeout(done, 0));
    expect(original.destroy).toHaveBeenCalledOnce(); expect(cpu.instances).toHaveLength(2);
    graph.setPointPositions.mockClear(); original.options.onPositions(new Float32Array(6).fill(999));
    expect(graph.setPointPositions).not.toHaveBeenCalled();
  });

  it.each(["loss", "destroy"] as const)("retires CPU work on %s and rejects terminal late callbacks", async boundary => {
    const { adapter, container } = await mountCpu(), worker = cpu.instances[0];
    if (boundary === "loss") container.querySelector("canvas")!.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    else adapter.destroy();
    expect(worker.destroy).toHaveBeenCalledOnce(); expect(worker.isRunning()).toBe(false);
    graph.setPointPositions.mockClear(); worker.options.onPositions(new Float32Array(6).fill(999));
    expect(graph.setPointPositions).not.toHaveBeenCalled(); expect(container.dataset.mapLayoutRunning).toBe("false");
  });

  it("reports Worker failure once and stops rather than running native GPU forces", async () => {
    const { adapter, container, onError } = await mountCpu(), worker = cpu.instances[0]; const error = new Error("Worker unavailable");
    worker.options.onError(error); worker.options.onError(error);
    const rendererCalls = [graph.create, graph.setPointPositions, graph.setPinnedPoints, graph.fitView,
      graph.setZoomTransformByPointPositions, graph.setZoomLevel, graph.setConfigPartial, graph.start, graph.pause,
      graph.setLinkColors];
    rendererCalls.forEach(method => method.mockClear());
    adapter.setReducedMotion(true); adapter.resumeLayout(); adapter.stopLayout();
    adapter.reset(); adapter.fit(); adapter.resize(); adapter.zoomBy(2);
    adapter.focus("a"); adapter.select("a"); adapter.setSearchMatches(new Set(["a"]));
    adapter.setEdgesVisible(false); adapter.setLabelsVisible(false);
    adapter.setViewport({ zoom: 2, pan: { x: 5, y: 6 } }); adapter.replaceColumns(columns());
    expect(adapter.pin("a", true, { x: 5, y: 6 })).toBeNull();
    expect(adapter.getViewport()).toBeNull(); expect(adapter.captureCoordinates()).toBeNull();
    for (const method of rendererCalls) expect(method).not.toHaveBeenCalled();
    expect(cpu.instances).toHaveLength(1);
    expect(onError).toHaveBeenCalledExactlyOnceWith(error); expect(worker.destroy).toHaveBeenCalledOnce();
    expect(container.dataset.mapPhysics).toBe("failed"); expect(container.dataset.mapLayoutRunning).toBe("false");
    expect(graph.start).not.toHaveBeenCalled();
  });

  it("uploads reduced-native-space frames and final drag pins in canonical units", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(nativeContext(4096));
    const { adapter, onPin } = await mountCpu(), worker = cpu.instances[0];
    const frame = Float32Array.from([500, 600, 700, 800, 900, 1000]); worker.options.onPositions(frame);
    expect(graph.getPointPositions()).toEqual(Float32Array.from([250, 300, 350, 400, 450, 500]));
    const config = createGraph.mock.calls.at(-1)![1]; config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    const drag = installedSdkDrag(config, 0); drag.start(); graph.screenToSpacePosition.mockReturnValue([321, 432]);
    drag.move(30, 50); drag.end(); expect(onPin).toHaveBeenLastCalledWith("a", { x: 642, y: 864 });
    expect(adapter.captureCoordinates()!.positions.slice(0, 2)).toEqual(Float32Array.from([642, 864]));
  });

  it("stops a hidden page's CPU Worker and resumes the same coordinates and pins without camera replay", async () => {
    const { adapter, container } = await mountCpu(), worker = cpu.instances[0];
    worker.options.onPositions(Float32Array.from([700, 800, 900, 1000, 1100, 1200]));
    adapter.pin("a", true, { x: 77, y: 88 });
    const saved = adapter.captureCoordinates()!, starts = worker.start.mock.calls.length;
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(worker.isRunning()).toBe(false); expect(container.dataset.mapLayoutRunning).toBe("false");
    graph.setPointPositions.mockClear(); graph.getPointPositions.mockClear();
    worker.options.onPositions(new Float32Array(6).fill(999));
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    graph.fitView.mockClear(); graph.setZoomTransformByPointPositions.mockClear(); graph.setZoomLevel.mockClear();
    worker.setPositions.mockClear(); visibility.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(worker.isRunning()).toBe(true); expect(worker.start).toHaveBeenCalledTimes(starts + 1);
    expect(worker.start).toHaveBeenLastCalledWith(false);
    expect(container.dataset.mapLayoutRunning).toBe("true"); expect(cpu.instances).toHaveLength(1);
    expect(worker.setPositions).not.toHaveBeenCalled(); expect(graph.getPointPositions).not.toHaveBeenCalled();
    expect(graph.setPointPositions).not.toHaveBeenCalled(); expect(graph.start).not.toHaveBeenCalled();
    expect(graph.fitView).not.toHaveBeenCalled(); expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    expect(graph.setZoomLevel).not.toHaveBeenCalled();
    const restored = adapter.captureCoordinates()!;
    expect(restored.positions).toEqual(saved.positions);
    expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([0]);
  });

  it.each(["pause", "reduced"] as const)("does not restore CPU permission on visibility return after hidden %s", async mode => {
    const { adapter, container } = await mountCpu(), worker = cpu.instances[0];
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    if (mode === "pause") adapter.stopLayout(); else adapter.setReducedMotion(true);
    const before = adapter.captureCoordinates()!.positions;
    graph.setPointPositions.mockClear(); worker.start.mockClear(); visibility.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(worker.isRunning()).toBe(false);
    // Reduced motion may release a pending static first frame, never animation.
    if (mode === "pause") expect(worker.start).not.toHaveBeenCalled();
    else expect(worker.start.mock.calls.every(([reheat]) => reheat === false)).toBe(true);
    expect(adapter.captureCoordinates()!.positions).toEqual(before);
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(container.dataset.mapLayoutRunning).toBe("false"); expect(graph.start).not.toHaveBeenCalled();
  });

  it("waits through an initially hidden page then fits its first solved frame once", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const { adapter, container } = await mountCpu();
    expect(cpu.create).not.toHaveBeenCalled(); expect(container.dataset.mapLayoutRunning).toBe("false");
    adapter.resumeLayout(); expect(cpu.create).not.toHaveBeenCalled(); expect(graph.start).not.toHaveBeenCalled();
    visibility.mockReturnValue("visible"); document.dispatchEvent(new Event("visibilitychange"));
    const worker = cpu.instances[0], solved = Float32Array.from([100, 200, 300, 400, 500, 600]);
    graph.setZoomTransformByPointPositions.mockClear(); graph.fitView.mockClear();
    worker.options.onPositions(solved);
    worker.options.onState({ ready: true, running: false, phase: "cooled" });
    await new Promise(resolve => setTimeout(resolve, 5));
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledExactlyOnceWith(solved, 0, undefined, .08, false);
    expect(graph.fitView).not.toHaveBeenCalled();
  });
});

describe("explicit PC complete upstream GPU simulation", () => {
  it.each([3, 2_000])("uses native Cosmos without a custom driver for the default %i-node scene", async count => {
    await mount(sizedColumns(count), false);
    expect(createNativeGraph).toHaveBeenCalledOnce();
    expect(createNativeGraph.mock.calls[0][0].dataset.mapPhysics).toBe("cosmos-native");
    expect(createGraph.mock.calls[0][0].dataset.mapLayoutPhase).toBeUndefined();
    expect(createGraph.mock.calls[0][1].simulationCluster).toBe(0);
    expect(graph.setPointClusters).not.toHaveBeenCalled();
    expect(graph.setClusterPositions).not.toHaveBeenCalled();
  });
});

describe("PC native scene admission", () => {
  it("keeps default native GPU physics at admitted PC sizes above 2K on creation and replacement", async () => {
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, sizedColumns(2_001), {
      budget: WORKSPACE_MAP_PC_10K_BUDGET, forcedColors: false, reducedMotion: true,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() },
    });
    adapters.push(adapter);
    await adapter.whenSettled();
    expect(container.dataset.mapPhysics).toBe("cosmos-native");
    expect(createNativeGraph).toHaveBeenCalledOnce();
    adapter.replaceColumns(sizedColumns(2_002));
    expect(container.dataset.mapPhysics).toBe("cosmos-native");
    expect(adapter.captureCoordinates()!.columns.nodeIds).toHaveLength(2_002);
  });
});

function workerCollisionBasis(input = columns(), seed = communityResult(input).initialPositions,
  width = 734, height = 608) {
  const buffers = buildCosmosSceneBuffers(input, readGraphTokens(document.body, "light"));
  return buildWorkspaceRelationCollisionBasis(seed, buffers.pointSizes, width, height)!;
}

function expectedCollisionCommit(basis: { radius: number; padding: number }) {
  return { simulationCollision: 1,
    simulationCollisionRadius: basis.radius, simulationCollisionPadding: basis.padding };
}

function collisionCommits() {
  return graph.setConfigPartial.mock.calls.map(([config]) => config)
    .filter((config) => config.simulationCollisionRadius !== undefined);
}

function forceCommits() {
  const fields = ["simulationGravity", "simulationRepulsion", "simulationLinkSpring",
    "simulationLinkDistance", "simulationLinkDistRandomVariationRange"] as const;
  return graph.setConfigPartial.mock.calls.map(([config]) => config)
    .filter((config) => fields.some((field) => config[field] !== undefined));
}

function expectedPhysicsCommit(input = columns(), basis = freshCollisionBasis(input), forceBasis = basis) {
  return { ...buildWorkspaceRelationForceConfig(forceBasis), ...expectedCollisionCommit(basis) };
}

function communityResult(input = columns()): Extract<WorkspaceMapCommunityOutcome, { kind: "complete" }> {
  return { kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId, input.identity.actorId,
    input.identity.surface, input.identity.selectionKey, input.identity.visibleFingerprint]),
    initialPositions: Float32Array.from([400, 500, 1800, 1900, 3000, 3100]),
    communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 };
}

describe("Cosmos Map scene-owned collision lifecycle", () => {
  it.each([[0, 0, 100, 100], [10, 10], [0, 0, .5, .25], [0, 0, 1_000_000, 1_000_000]])(
    "matches the actual pinned SDK pure Fit transform for %s", (...positions) => {
      const { zoom } = installedSdkCamera();
      const seed = Float32Array.from(positions);
      const size = new Float32Array(seed.length / 2).fill(12);
      const nativeZoom = zoom.getTransform(seed, undefined, .08).k;
      expect(buildWorkspaceRelationCollisionBasis(seed, size, 734, 608))
        .toEqual({ radius: 12 / (2 * nativeZoom), padding: 1 / nativeZoom });
    },
  );

  it("starts no GPU frame before positive calibration and initial Fit settle", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.push(callback); return frames.length; });
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter);
    expect(sdk.running()).toBe(false); expect(createGraph.mock.calls[0][1].enableSimulation).toBe(false);
    await new Promise(done => setTimeout(done, 0));
    expect(collisionCommits()).toHaveLength(1); expect(sdk.running()).toBe(false); expect(frames).toHaveLength(1);
    frames.shift()!(performance.now()); await new Promise(done => setTimeout(done, 0));
    expect(sdk.running()).toBe(false); expect(frames).toHaveLength(1);
    frames.shift()!(performance.now()); await adapter.whenSettled();
    expect(sdk.running()).toBe(true); expect(collisionCommits()).toHaveLength(1);
  });

  it("renders but does not animate arbitrary fallback while the relation worker is pending", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValueOnce(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input, false);
    expect(sdk.running()).toBe(false); expect(sdk.simulationEnabled()).toBe(false);
    expect(graph.start).not.toHaveBeenCalled();
    const positions = Float32Array.from([500, 600, 700, 800, 900, 1000]);
    resolve({ ...communityResult(input), initialPositions: positions });
    await new Promise(done => setTimeout(done, 0));
    expect(graph.getPointPositions()).toEqual(positions);
    expect(sdk.running()).toBe(true); expect(sdk.simulationEnabled()).toBe(true);
    expect(graph.start).toHaveBeenLastCalledWith(.35);
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(workerCollisionBasis(input, positions));
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(freshCollisionBasis(input));
  });

  it.each(["error", "timeout"] as const)("releases the static fallback after a current worker %s", async (kind) => {
    const sdk = installedSdkLifecycle(), input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValueOnce(new Promise(complete => { resolve = complete; }));
    await mount(input, false);
    const positions = graph.getPointPositions();
    expect(sdk.running()).toBe(false);
    resolve({ kind, identityKey: communityResult(input).identityKey });
    await new Promise(done => setTimeout(done, 0));
    expect(graph.getPointPositions()).toEqual(positions);
    expect(sdk.running()).toBe(true); expect(sdk.simulationEnabled()).toBe(true);
    expect(graph.start).toHaveBeenLastCalledWith(.35);
  });

  it.each(["pointer", "viewport"] as const)("keeps pre-ready %s camera intent over initial and late worker Fit", async (intent) => {
    const input = columns();
    let nativeReady!: () => void, resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    graph.ready = new Promise(complete => { nativeReady = complete; });
    community.compute.mockReturnValueOnce(new Promise(complete => { resolve = complete; }));
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter);
    if (intent === "pointer") container.dispatchEvent(new Event("pointerdown"));
    else adapter.setViewport({ zoom: .001, pan: { x: 3900, y: 250 } });
    nativeReady(); await adapter.whenSettled();
    if (intent === "pointer") expect(graph.fitView).not.toHaveBeenCalled();
    else expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(Float32Array.from([3900, 250]), 0, .001, 0, false);
    vi.clearAllMocks(); resolve(communityResult(input)); await new Promise(done => setTimeout(done, 0));
    expect(graph.fitView).not.toHaveBeenCalled(); expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(workerCollisionBasis(input));
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(freshCollisionBasis(input));
    expect(forceCommits()).toEqual([]);
    expect(collisionCommits()).toEqual([expectedCollisionCommit(workerCollisionBasis(input))]);
  });

  it("retires an older pending saved camera when explicit navigation happens after ready but before Fit", async () => {
    const input = columns(), frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.push(callback); return frames.length; });
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      initialViewport: { zoom: .3, pan: { x: 888, y: 999 } }, callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await new Promise(done => setTimeout(done, 0));
    adapter.setViewport({ zoom: .001, pan: { x: 3900, y: 250 } });
    frames.shift()!(performance.now()); await new Promise(done => setTimeout(done, 0));
    frames.shift()!(performance.now()); await adapter.whenSettled();
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledTimes(1);
    expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(Float32Array.from([3900, 250]), 0, .001, 0, false);
    expect(graph.fitView).not.toHaveBeenCalled();
    expect(collisionCommits()).toHaveLength(1);
  });

  it.each([true, false])("keeps immutable fallback force units and worker collision units regardless of workerBeforeReady=%s or current pins", async (workerBeforeReady) => {
    const input = columns();
    let nativeReady!: () => void, resolve!: (result: WorkspaceMapCommunityOutcome) => void;
    graph.ready = new Promise(complete => { nativeReady = complete; });
    community.compute.mockReturnValueOnce(new Promise(complete => { resolve = complete; }));
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter);
    const outcome = communityResult(input);
    if (workerBeforeReady) { resolve(outcome); await new Promise(done => setTimeout(done, 0)); }
    expect(collisionCommits()).toEqual([]); expect(forceCommits()).toEqual([]);
    nativeReady(); await adapter.whenSettled();
    const forceBasis = freshCollisionBasis(input), collisionBasis = workerCollisionBasis(input);
    expect(collisionBasis).not.toEqual(forceBasis);
    expect(forceCommits()).toEqual([expectedPhysicsCommit(input, forceBasis)]);
    const originalForce = forceCommits()[0];
    expect(originalForce.simulationGravity).toBe(.25);
    expect(originalForce.simulationLinkSpring).toBe(1);
    expect(originalForce.simulationLinkDistance).toBe(4 * (forceBasis.radius + forceBasis.padding));
    expect(originalForce.simulationRepulsion).toBe((4 * (forceBasis.radius + forceBasis.padding) / 10) ** 2);
    expect(originalForce.simulationLinkDistRandomVariationRange).toEqual([1, 1]);
    adapter.pin("a", true, { x: 3500, y: 3500 });
    if (!workerBeforeReady) { resolve(outcome); await new Promise(done => setTimeout(done, 0)); }
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(collisionBasis);
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(forceBasis);
    expect(collisionCommits()).toEqual([
      expectedPhysicsCommit(input, forceBasis), expectedCollisionCommit(collisionBasis),
    ]);
    expect(forceCommits()).toEqual([expectedPhysicsCommit(input, forceBasis)]);
    expect(graph.setPointClusters).not.toHaveBeenCalled();
    expect(graph.setClusterPositions).not.toHaveBeenCalled();
  });

  it("calibrates a late worker against the first positive CSS dimensions, not an intervening resize or pin", async () => {
    const input = columns();
    let resolve!: (result: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValueOnce(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input, false);
    const forceBasis = freshCollisionBasis(input), physics = expectedPhysicsCommit(input, forceBasis);
    expect(forceCommits()).toEqual([physics]);
    adapter.pin("a", true, { x: 3500, y: 3500 });
    graph.getZoomLevel.mockReturnValue(.001);
    const canvas = document.querySelector("canvas")!;
    Object.defineProperties(canvas, { clientWidth: { configurable: true, value: 390 }, clientHeight: { configurable: true, value: 400 } });
    adapter.resize(); adapter.setViewport({ zoom: .001, pan: { x: 3900, y: 250 } });
    const outcome = communityResult(input), expected = workerCollisionBasis(input, outcome.initialPositions);
    expect(workerCollisionBasis(input, outcome.initialPositions, 390, 400)).not.toEqual(expected);
    resolve(outcome); await new Promise(done => setTimeout(done, 0));
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(expected);
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(forceBasis);
    expect(forceCommits()).toEqual([physics]);
    expect(collisionCommits()).toEqual([physics, expectedCollisionCommit(expected)]);
    const actual = graph.getPointPositions();
    expect(Array.from(actual.slice(0, 2))).toEqual([3500, 3500]);
    expect(workerCollisionBasis(input, actual)).not.toEqual(expected);
  });

  it.each([
    { ids: [0, 0, 0], count: 1 },
    { ids: [0, 1, 0], count: 2 },
    { ids: [0, 1, 2], count: 3 },
  ])("keeps original weighted forces when a late worker supplies $count communities", async ({ ids, count }) => {
    const input = columns();
    input.edgeWeights[1] = 64;
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValueOnce(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input, false);
    expect([...graph.setLinkStrength.mock.lastCall![0]]).toEqual([1, 7]);
    expect(graph.setLinkStrength).toHaveBeenCalledTimes(1);
    expect(forceCommits()).toEqual([expectedPhysicsCommit(input)]);
    const target = Float32Array.from([500, 600, 700, 800, 900, 1000]);
    vi.clearAllMocks();
    resolve({ ...communityResult(input), initialPositions: target,
      communityIds: Uint32Array.from(ids), communityCount: count });
    await new Promise(done => setTimeout(done, 0));
    expect(graph.getPointPositions()).toEqual(target);
    expect(graph.setPointPositions).toHaveBeenCalledTimes(1);
    expect(document.body.lastElementChild?.getAttribute("data-map-community-count")).toBe(String(count));
    expect(forceCommits()).toEqual([]);
    expect(collisionCommits()).toEqual([expectedCollisionCommit(workerCollisionBasis(input, target))]);
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(freshCollisionBasis(input));
    for (const method of [graph.setLinkStrength, graph.setPointClusters, graph.setClusterPositions]) {
      expect(method).not.toHaveBeenCalled();
    }
    vi.clearAllMocks();
    adapter.stopLayout(); adapter.setReducedMotion(true); adapter.setReducedMotion(false); adapter.resumeLayout();
    expect(graph.getPointPositions()).toEqual(target);
    expect(forceCommits()).toEqual([]);
    expect(collisionCommits()).toEqual([]);
    for (const method of [graph.setPointPositions, graph.setLinkStrength, graph.setPointClusters, graph.setClusterPositions]) {
      expect(method).not.toHaveBeenCalled();
    }
  });

  it.each([.001, 1000])("keeps collision independent from extreme saved camera zoom%s", async (zoom) => {
    const input = columns(), container = document.createElement("div"); document.body.appendChild(container);
    community.compute.mockResolvedValueOnce(communityResult(input));
    const viewport = { zoom, pan: { x: 3900, y: 250 } };
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      initialViewport: viewport, callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(workerCollisionBasis(input));
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(freshCollisionBasis(input));
    expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(Float32Array.from([3900, 250]), 0, zoom, 0, false);
  });

  it.each([[true, false], [false, true], [true, true]])("prepares a paused%s/reduced%s basis without permitting pre-fit GPU physics", async (paused, reduced) => {
    const sdk = installedSdkLifecycle(), input = columns();
    const adapter = await mount(input, reduced, vi.fn(), paused);
    expect(createGraph.mock.calls[0][1].enableSimulation).toBe(false);
    expect(sdk.running()).toBe(false);
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(freshCollisionBasis(input));
    const before = graph.getPointPositions(); graph.getZoomLevel.mockReturnValue(.001);
    adapter.resumeLayout(); adapter.setReducedMotion(false);
    expect(sdk.running()).toBe(true); expect(collisionCommits()).toHaveLength(1);
    expect(graph.getPointPositions()).toEqual(before);
  });

  it("defers a zero-size scene until one positive resize, excluding pins and current camera from its seed", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    const container = document.createElement("div"), canvas = document.createElement("canvas");
    Object.defineProperties(canvas, { clientWidth: { configurable: true, value: 0 }, clientHeight: { configurable: true, value: 0 } });
    container.appendChild(canvas); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      initialViewport: { zoom: .001, pan: { x: 3900, y: 250 } }, callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    expect(collisionCommits()).toEqual([]); expect(sdk.running()).toBe(false);
    adapter.resumeLayout(); expect(sdk.running()).toBe(false);
    adapter.pin("a", true, { x: 3500, y: 3500 });
    const before = graph.getPointPositions();
    Object.defineProperties(canvas, { clientWidth: { configurable: true, value: 734 }, clientHeight: { configurable: true, value: 608 } });
    graph.setZoomTransformByPointPositions.mockClear(); graph.fitView.mockClear();
    adapter.resize();
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(freshCollisionBasis(input));
    expect(collisionCommits()).toHaveLength(1); expect(sdk.running()).toBe(true);
    expect(graph.getPointPositions()).toEqual(before);
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled(); expect(graph.fitView).not.toHaveBeenCalled();
    adapter.resize(); expect(collisionCommits()).toHaveLength(1);
  });

  it("preserves exact basis, coordinates and pause through Fit/Zoom/search/selection/metadata/resize and Reset", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    community.compute.mockResolvedValueOnce(communityResult(input));
    const adapter = await mount(input, false);
    expect(forceCommits()).toEqual([expectedPhysicsCommit(input)]);
    adapter.stopLayout(); adapter.pin("a", true, { x: 3500, y: 3500 });
    const before = graph.getPointPositions(), basis = adapter.captureCoordinates()!.collisionBasis;
    const forceBasis = adapter.captureCoordinates()!.forceBasis;
    expect(basis).toEqual(workerCollisionBasis(input));
    expect(forceBasis).toEqual(freshCollisionBasis(input));
    expect(basis).not.toEqual(forceBasis);
    vi.clearAllMocks();
    graph.getZoomLevel.mockReturnValue(.001);
    adapter.fit(); adapter.zoomBy(2); adapter.focus("bc"); adapter.select("a"); adapter.setSearchMatches(new Set(["a"]));
    adapter.setReducedMotion(true); adapter.setReducedMotion(false);
    adapter.replaceColumns({ ...input, nodeFolderKeys: ["one", "two", "one"] });
    const canvas = document.querySelector("canvas")!;
    Object.defineProperties(canvas, { clientWidth: { configurable: true, value: 390 }, clientHeight: { configurable: true, value: 400 } });
    adapter.resize();
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(basis);
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(forceBasis);
    expect(graph.getPointPositions()).toEqual(before); expect(sdk.running()).toBe(false);
    expect(collisionCommits()).toEqual([]);
    expect(forceCommits()).toEqual([]);
    for (const method of [graph.setPointPositions, graph.setPinnedPoints, graph.start, community.compute]) expect(method).not.toHaveBeenCalled();
    adapter.reset();
    expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([]);
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(basis);
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(forceBasis);
    expect(graph.getPointPositions()).toEqual(before); expect(collisionCommits()).toEqual([]);
    expect(forceCommits()).toEqual([]);
    adapter.resumeLayout();
    expect(sdk.running()).toBe(true); expect(graph.getPointPositions()).toEqual(before);
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(basis);
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(forceBasis);
    expect(collisionCommits()).toEqual([]); expect(forceCommits()).toEqual([]);
  });

  it("copies/reuses both fenced snapshot bases independently instead of recalibrating restored coordinates", async () => {
    const input = columns(), snapshot = { columns: input, positions: Float32Array.from([333, 444, 555, 666, 777, 888]),
      collisionBasis: { radius: 13, padding: 5 }, forceBasis: { radius: 3, padding: 2 } };
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      initialCoordinates: snapshot, callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    snapshot.collisionBasis.radius = 9999;
    snapshot.forceBasis.radius = 9999;
    adapters.push(adapter); await adapter.whenSettled();
    const captured = adapter.captureCoordinates()!;
    expect(captured.positions).toEqual(snapshot.positions);
    expect(captured.collisionBasis).toEqual({ radius: 13, padding: 5 });
    expect(captured.collisionBasis).not.toBe(snapshot.collisionBasis);
    expect(captured.forceBasis).toEqual({ radius: 3, padding: 2 });
    expect(captured.forceBasis).not.toBe(snapshot.forceBasis);
    captured.collisionBasis!.radius = 9999;
    captured.forceBasis!.radius = 9999;
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual({ radius: 13, padding: 5 });
    expect(adapter.captureCoordinates()!.forceBasis).toEqual({ radius: 3, padding: 2 });
    expect(collisionCommits()).toEqual([expectedPhysicsCommit(input, { radius: 13, padding: 5 }, { radius: 3, padding: 2 })]);
    expect(forceCommits()).toEqual(collisionCommits());
  });

  it.each([{ radius: NaN, padding: 1 }, { radius: -1, padding: 1 }, { radius: 1, padding: 0 }])(
    "rebuilds an explicitly invalid force basis%s from fallback, never the independently valid collision basis", async (forceBasis) => {
      const input = columns(), collisionBasis = { radius: 13, padding: 5 };
      const actual = Float32Array.from([333, 444, 555, 666, 777, 888]);
      const container = document.createElement("div"); document.body.appendChild(container);
      const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
        initialCoordinates: { columns: input, positions: actual, collisionBasis, forceBasis },
        callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
      adapters.push(adapter); await adapter.whenSettled();
      expect(adapter.captureCoordinates()!.positions).toEqual(actual);
      expect(adapter.captureCoordinates()!.collisionBasis).toEqual(collisionBasis);
      expect(adapter.captureCoordinates()!.forceBasis).toEqual(freshCollisionBasis(input));
      expect(forceCommits()).toEqual([expectedPhysicsCommit(input, collisionBasis, freshCollisionBasis(input))]);
    },
  );

  it.each([
    { radius: 1e100, padding: 1 },
    { radius: 1, padding: 1e100 },
    { radius: 1e-100, padding: 1 },
    { radius: 1, padding: 1e-100 },
    { radius: 1e38, padding: 1e38 },
  ])("rebuilds an unrepresentable Float32 snapshot collision basis%s without changing valid force units or user coordinates", async (collisionBasis) => {
    const input = columns(), forceBasis = { radius: 13, padding: 5 };
    const actual = Float32Array.from([333, 444, 555, 666, 777, 888]);
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      initialCoordinates: { columns: input, positions: actual, collisionBasis, forceBasis },
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    const captured = adapter.captureCoordinates()!, rebuilt = freshCollisionBasis(input);
    expect(captured.positions).toEqual(actual);
    expect(graph.getPointPositions()).toEqual(actual);
    expect(captured.collisionBasis).toEqual(rebuilt);
    expect(captured.forceBasis).toEqual(forceBasis);
    expect(collisionCommits()).toEqual([expectedPhysicsCommit(input, rebuilt, forceBasis)]);
    expect(forceCommits()).toEqual(collisionCommits());
  });

  it("reuses the same scene physics through a palette remount with another canvas size and saved camera", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    community.compute.mockResolvedValueOnce(communityResult(input));
    const initial = await mount(input, false);
    expect(forceCommits()).toEqual([expectedPhysicsCommit(input)]);
    initial.stopLayout(); initial.pin("a", true, { x: 3500, y: 3500 });
    const snapshot = initial.captureCoordinates()!;
    const physics = expectedPhysicsCommit(input, snapshot.collisionBasis!, snapshot.forceBasis!);
    expect(snapshot.forceBasis).toEqual(freshCollisionBasis(input));
    expect(snapshot.collisionBasis).toEqual(workerCollisionBasis(input));
    initial.destroy();
    vi.clearAllMocks();
    const container = document.createElement("div"), canvas = document.createElement("canvas");
    Object.defineProperties(canvas, { clientWidth: { value: 390 }, clientHeight: { value: 400 } });
    container.appendChild(canvas); document.body.appendChild(container);
    document.documentElement.setAttribute("data-theme", "dark");
    community.compute.mockReturnValueOnce(new Promise(() => {}));
    try {
      const restored = createWorkspaceCosmosAdapter(container, input, {
        forcedColors: false, initialPaused: true, reducedMotion: false,
        initialCoordinates: snapshot, initialViewport: { zoom: .001, pan: { x: 3900, y: 250 } },
        callbacks: { onHover: vi.fn(), onSelect: vi.fn() },
      });
      adapters.push(restored); await restored.whenSettled();
      expect(forceCommits()).toEqual([physics]);
      expect(collisionCommits()).toEqual([physics]);
      expect(restored.captureCoordinates()!.collisionBasis).toEqual(snapshot.collisionBasis);
      expect(restored.captureCoordinates()!.forceBasis).toEqual(snapshot.forceBasis);
      expect(graph.getPointPositions()).toEqual(snapshot.positions);
      expect(sdk.running()).toBe(false);
      expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(Float32Array.from([3900, 250]), 0, .001, 0, false);
      vi.clearAllMocks();
      restored.setReducedMotion(true); restored.setReducedMotion(false); restored.resumeLayout();
      expect(sdk.running()).toBe(true);
      expect(graph.getPointPositions()).toEqual(snapshot.positions);
      expect(forceCommits()).toEqual([]);
      expect(collisionCommits()).toEqual([]);
      for (const method of [graph.setPointPositions, graph.setLinkStrength, graph.setPointClusters, graph.setClusterPositions]) {
        expect(method).not.toHaveBeenCalled();
      }
    } finally { document.documentElement.removeAttribute("data-theme"); }
  });

  it("retains a valid restored basis while hidden, applying it exactly once on positive resize before physics", async () => {
    const sdk = installedSdkLifecycle(), input = columns(), positions = Float32Array.from([333, 444, 555, 666, 777, 888]);
    const container = document.createElement("div"), canvas = document.createElement("canvas");
    Object.defineProperties(canvas, { clientWidth: { configurable: true, value: 0 }, clientHeight: { configurable: true, value: 0 } });
    container.appendChild(canvas); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      initialCoordinates: { columns: input, positions, collisionBasis: { radius: 13, padding: 5 } },
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    expect(sdk.running()).toBe(false); expect(sdk.simulationEnabled()).toBe(false);
    expect(collisionCommits()).toEqual([]); expect(adapter.captureCoordinates()!.collisionBasis).toEqual({ radius: 13, padding: 5 });
    expect(adapter.captureCoordinates()!.forceBasis).toEqual({ radius: 13, padding: 5 });
    adapter.resumeLayout(); expect(sdk.running()).toBe(false); expect(graph.start).not.toHaveBeenCalled();
    Object.defineProperties(canvas, { clientWidth: { configurable: true, value: 390 }, clientHeight: { configurable: true, value: 400 } });
    graph.setZoomTransformByPointPositions.mockClear(); graph.fitView.mockClear();
    adapter.resize();
    expect(collisionCommits()).toEqual([expectedPhysicsCommit(input, { radius: 13, padding: 5 })]);
    expect(forceCommits()).toEqual(collisionCommits());
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual({ radius: 13, padding: 5 });
    expect(adapter.captureCoordinates()!.forceBasis).toEqual({ radius: 13, padding: 5 });
    expect(graph.getPointPositions()).toEqual(positions); expect(sdk.running()).toBe(true);
    expect(graph.start).toHaveBeenLastCalledWith(.35);
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled(); expect(graph.fitView).not.toHaveBeenCalled();
    adapter.resize(); expect(collisionCommits()).toHaveLength(1);
  });

  it.each([undefined, { radius: NaN, padding: 1 }, { radius: -1, padding: 1 }, { radius: 1, padding: 0 }])(
    "ignores invalid/legacy snapshot basis%s but still restores valid same-scene coordinates", async (collisionBasis) => {
      const input = columns(), positions = Float32Array.from([333, 444, 555, 666, 777, 888]);
      const container = document.createElement("div"); document.body.appendChild(container);
      const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: true,
        initialCoordinates: { columns: input, positions, collisionBasis }, callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
      adapters.push(adapter); await adapter.whenSettled();
      expect(adapter.captureCoordinates()!.positions).toEqual(positions);
      expect(adapter.captureCoordinates()!.collisionBasis).toEqual(freshCollisionBasis(input));
      expect(adapter.captureCoordinates()!.forceBasis).toEqual(freshCollisionBasis(input));
    },
  );

  it("retires old collision/force ownership on changed indexed geometry even with identical identity strings", async () => {
    const input = columns(); community.compute.mockResolvedValueOnce(communityResult(input));
    const adapter = await mount(input, false), previous = adapter.captureCoordinates()!.collisionBasis;
    const previousForce = adapter.captureCoordinates()!.forceBasis;
    const next = { ...input, nodeSizes: Float32Array.from([8, 2, 2]) };
    community.compute.mockReturnValueOnce(new Promise(() => {})); vi.clearAllMocks();
    adapter.replaceColumns(next);
    expect(graph.setConfigPartial).toHaveBeenCalledWith(expect.objectContaining({ simulationCluster: 0, enableSimulation: false }));
    expect(collisionCommits()).toHaveLength(1);
    expect(forceCommits()).toEqual([expectedPhysicsCommit(next)]);
    expect(forceCommits()).toEqual(collisionCommits());
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(freshCollisionBasis(next));
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(freshCollisionBasis(next));
    expect(adapter.captureCoordinates()!.collisionBasis).not.toEqual(previous);
    expect(adapter.captureCoordinates()!.forceBasis).not.toEqual(previousForce);
    expect(graph.setPointClusters).not.toHaveBeenCalled();
    expect(graph.setClusterPositions).not.toHaveBeenCalled();
  });
});

describe("Cosmos Map presentation-only search", () => {
  it("owns native picking only above the 2K boundary and asynchronous titles on both sides", async () => {
    let now = 1_000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const controllers: Array<{ options: Parameters<typeof nativeTracking.createWorkspaceTrackedPositions>[0];
      request: ReturnType<typeof vi.fn>; invalidate: ReturnType<typeof vi.fn>; destroy: ReturnType<typeof vi.fn> }> = [];
    vi.spyOn(nativeTracking, "createWorkspaceTrackedPositions").mockImplementation(options => {
      const controller = { options, request: vi.fn(() => true), invalidate: vi.fn(), destroy: vi.fn() };
      controllers.push(controller); return controller;
    });
    const input = sizedColumns(2_001), adapter = await mount(input);
    const [container, config] = createGraph.mock.calls.at(-1)!;
    expect(graph.attachPointPicker.mock.lastCall?.[0]).toMatchObject({ pickSync: expect.any(Function), request: expect.any(Function) });
    expect(controllers).toHaveLength(1);
    const owner = controllers[0];
    graph.getTrackedPointPositionsMap.mockClear(); graph.getPointPositions.mockClear();
    owner.options.onPositions(new Map([[0, [250, 300]]]));
    expect(container.querySelector<HTMLElement>('[data-node-id="node:0"]')?.dataset.pointX).toBeDefined();
    expect(graph.getTrackedPointPositionsMap).not.toHaveBeenCalled(); expect(graph.getPointPositions).not.toHaveBeenCalled();
    const invalidations = owner.invalidate.mock.calls.length;
    config.onPointMouseOver?.(2_000, [200, 250], undefined, false, false);
    expect(owner.invalidate.mock.calls.length).toBeGreaterThan(invalidations);
    const beforeHide = owner.invalidate.mock.calls.length;
    adapter.setLabelsVisible(false); expect(owner.invalidate).toHaveBeenCalledTimes(beforeHide + 1);
    owner.request.mockClear(); now += 200; adapter.setLabelsVisible(true);
    expect(owner.request).not.toHaveBeenCalled(); await Promise.resolve(); expect(owner.request).toHaveBeenCalledOnce();
    graph.getPointPositions.mockClear();
    config.onBackgroundClick?.(new MouseEvent("click", { clientX: 700, clientY: 500 }));
    config.onLinkClick?.(0, new MouseEvent("click", { clientX: 700, clientY: 500 }));
    expect(graph.getPointPositions).not.toHaveBeenCalled();
    adapter.replaceColumns({ ...input, identity: { ...input.identity, actorId: "actor-b", visibleFingerprint: "visible-b" } });
    expect(owner.destroy).toHaveBeenCalledOnce(); expect(controllers).toHaveLength(2);
    container.querySelector("canvas")!.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    expect(controllers[1].destroy).toHaveBeenCalledOnce(); expect(graph.attachPointPicker.mock.lastCall?.[0]).toBeNull();
    adapter.destroy();
    graph.attachPointPicker.mockClear();
    await mount(sizedColumns(2_000));
    expect(graph.attachPointPicker.mock.calls.every(([picker]) => picker === null)).toBe(true);
    // The 2K scene tracks its titles the same way: a simulation frame reads
    // asynchronously, never through the synchronous getter.
    expect(controllers).toHaveLength(3);
    graph.getTrackedPointPositionsMap.mockClear(); controllers[2].request.mockClear();
    createGraph.mock.calls.at(-1)![1].onSimulationTick?.(.3); await Promise.resolve();
    expect(controllers[2].request).toHaveBeenCalledOnce(); expect(graph.getTrackedPointPositionsMap).not.toHaveBeenCalled();
  });

  it("publishes a newly picked full title immediately without a throttled GPU label read", async () => {
    let now = 1_000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const input = sizedColumns(10_000, 30_000);
    graph.getTrackedPointPositionsMap.mockReturnValue(new Map([[0, [150, 150]]]));
    graph.spaceToScreenPosition.mockImplementation(position => position);
    await mount(input);
    const [container, config] = createGraph.mock.calls[0];
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 734, 608));
    Object.defineProperties(container, { clientWidth: { value: 734 }, clientHeight: { value: 608 } });
    // The initial refresh just consumed the six-Hz position budget.
    now += 1;
    graph.getTrackedPointPositionsMap.mockClear(); graph.getPointPositions.mockClear();
    config.onPointMouseOver?.(9_999, [250, 300], undefined, false, false);
    const chip = container.querySelector<HTMLElement>(".graph-hover-chip")!;
    expect(chip.textContent).toBe(input.nodeLabels[9_999]);
    expect(chip.dataset.hoverNodeId).toBe("node:9999");
    expect(container.querySelector('[data-node-id="node:9999"]')).toBeNull();
    expect(graph.getTrackedPointPositionsMap).not.toHaveBeenCalled();
    expect(graph.getPointPositions).not.toHaveBeenCalled();
    expect(container.querySelectorAll("[data-workspace-map-labels] span")).toHaveLength(40);
  });

  describe("label point identity publication", () => {
    function observeTrackedLabels() {
      let publish!: Parameters<typeof nativeTracking.createWorkspaceTrackedPositions>[0]["onPositions"];
      const controller = { request: vi.fn(() => true), invalidate: vi.fn(), destroy: vi.fn() };
      vi.spyOn(nativeTracking, "createWorkspaceTrackedPositions").mockImplementation(options => {
        publish = options.onPositions;
        return controller;
      });
      return { controller, publish: (positions: ReadonlyMap<number, [number, number]>) => publish(positions) };
    }

    function pointRows(start = 0): Map<number, [number, number]> {
      return new Map(Array.from({ length: 40 }, (_, slot): [number, [number, number]] =>
        [start + slot, [100 + start + slot, 200 + start + slot]]));
    }

    it("reads tracked rows once per simulation frame, advances them to the step on screen, and never reads on the heartbeat", async () => {
      const tracking = observeTrackedLabels(), { request } = tracking.controller;
      graph.spaceToScreenPosition.mockImplementation(point => point);
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      try {
      const adapter = await mount(sizedColumns(2_001));
      const config = createGraph.mock.calls[0][1];
      const frame = async () => { config.onSimulationTick?.(.3); await Promise.resolve(); };
      tracking.publish(pointRows()); await Promise.resolve(); request.mockClear();
      // The read waits for the end of the native frame's task, then once per frame.
      config.onSimulationTick?.(.3);
      expect(request).not.toHaveBeenCalled();
      await Promise.resolve(); await frame();
      expect(request).toHaveBeenCalledTimes(2);
      // Still rows read every frame too: a running simulation has no read back-off.
      tracking.publish(pointRows()); tracking.publish(pointRows()); request.mockClear();
      await frame(); await frame(); await frame();
      expect(request).toHaveBeenCalledTimes(3);
      // Rows reach titles after the step that drew them: each title advances
      // its row along the per-step velocity to the step on screen, at most 4.
      const title = () => createGraph.mock.calls[0][0].querySelector<HTMLElement>('[data-node-id="node:0"]')!.dataset.pointX;
      const rowsAt = (x: number) => new Map([...pointRows()].map(([index, [, y]]): [number, [number, number]] => [index, [x + index, y]]));
      await frame(); tracking.publish(rowsAt(200));
      await frame(); tracking.publish(rowsAt(210));
      expect(title()).toBe("210");
      await frame(); expect(title()).toBe("220");
      for (let step = 0; step < 6; step += 1) await frame();
      expect(title()).toBe("250");
      // Only a free node moved by the layout is extrapolated: a pinned row is exact.
      const titleOf = (index: number) => createGraph.mock.calls[0][0]
        .querySelector<HTMLElement>(`[data-node-id="node:${index}"]`)!.dataset.pointX;
      adapter.pin("node:1", true); await frame();
      expect(titleOf(1)).toBe("211"); expect(title()).toBe("250");
      // A hand-moved row is no velocity: a drag cancelled without a pin keeps
      // its released point after a later read that still measured the drag.
      graph.screenToSpacePosition.mockImplementation(point => point);
      const withNode0 = (rows: Map<number, [number, number]>, point: [number, number]) =>
        new Map([...rows].map(([index, row]): [number, [number, number]] => [index, index === 0 ? point : row]));
      config.onPointMouseOver?.(0, [250, 200], undefined, false, false);
      const drag = installedSdkDrag(config, 0); drag.start(); drag.move(300, 200);
      await frame(); tracking.publish(withNode0(rowsAt(220), [300, 200]));
      drag.move(320, 200); drag.end("pointercancel");
      await frame(); tracking.publish(withNode0(rowsAt(230), [320, 200]));
      await frame(); await frame();
      expect(title()).toBe("320");
      // A stopped simulation has no frames; the heartbeat republishes without reading.
      request.mockClear();
      vi.advanceTimersByTime(1_000); await Promise.resolve();
      expect(request).not.toHaveBeenCalled();
      } finally { vi.useRealTimers(); }
    });

    it("clears old slot coordinates before publishing disjoint node IDs, then publishes the new readback", async () => {
      let now = 1_000;
      vi.spyOn(performance, "now").mockImplementation(() => now);
      const tracking = observeTrackedLabels();
      graph.spaceToScreenPosition.mockImplementation(point => point);
      const adapter = await mount(sizedColumns(2_001));
      const container = createGraph.mock.calls[0][0];
      const labels = [...container.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span")];
      tracking.publish(pointRows());
      expect(labels[0].dataset).toMatchObject({ nodeId: "node:0", pointX: "100", pointY: "200" });
      tracking.controller.request.mockClear();
      now += 1;
      adapter.setSearchMatches(new Set(Array.from({ length: 40 }, (_, slot) => `node:${100 + slot}`)));
      labels.forEach((label, slot) => {
        expect(label.dataset.nodeId).toBe(`node:${100 + slot}`);
        expect(label).not.toHaveAttribute("data-point-x");
        expect(label).not.toHaveAttribute("data-point-y");
      });
      // New rows are read once, after this task, never inside it.
      expect(tracking.controller.request).not.toHaveBeenCalled();
      await Promise.resolve(); expect(tracking.controller.request).toHaveBeenCalledOnce();
      tracking.publish(pointRows(100));
      labels.forEach((label, slot) => {
        expect(label.dataset).toMatchObject({ nodeId: `node:${100 + slot}`,
          pointX: String(200 + slot), pointY: String(300 + slot) });
      });
    });

    it("removes coordinates from newly undefined slots without waiting for another GPU read", async () => {
      let now = 1_000;
      vi.spyOn(performance, "now").mockImplementation(() => now);
      const tracking = observeTrackedLabels();
      graph.spaceToScreenPosition.mockImplementation(point => point);
      const adapter = await mount(sizedColumns(2_001));
      const container = createGraph.mock.calls[0][0];
      const labels = [...container.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span")];
      tracking.publish(pointRows());
      expect(labels.every(label => label.hasAttribute("data-point-x") && label.hasAttribute("data-point-y"))).toBe(true);
      tracking.controller.request.mockClear();
      now += 1;
      adapter.setSearchMatches(new Set());
      labels.forEach(label => {
        expect(label.dataset.nodeId).toBe("");
        expect(label).not.toHaveAttribute("data-point-x");
        expect(label).not.toHaveAttribute("data-point-y");
      });
      expect(tracking.controller.request).not.toHaveBeenCalled();
      await Promise.resolve(); expect(tracking.controller.request).toHaveBeenCalledOnce();
    });

    it("clears only rebound IDs during hidden-label reordering and reprojects their identity-keyed cache correctly", async () => {
      let now = 1_000;
      vi.spyOn(performance, "now").mockImplementation(() => now);
      const tracking = observeTrackedLabels();
      graph.spaceToScreenPosition.mockImplementation(point => point);
      const adapter = await mount(sizedColumns(2_001));
      const [container, config] = createGraph.mock.calls[0];
      const labels = [...container.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span")];
      tracking.publish(pointRows());
      const before = labels.map(label => ({ ...label.dataset }));
      adapter.setLabelsVisible(false);
      tracking.controller.request.mockClear();
      now += 1;
      adapter.focus("node:1");
      expect(labels[0].dataset.nodeId).toBe("node:1");
      expect(labels[1].dataset.nodeId).toBe("node:0");
      labels.forEach((label, slot) => {
        if (label.dataset.nodeId === before[slot].nodeId) {
          expect(label.dataset.pointX).toBe(before[slot].pointX);
          expect(label.dataset.pointY).toBe(before[slot].pointY);
        } else {
          expect(label).not.toHaveAttribute("data-point-x");
          expect(label).not.toHaveAttribute("data-point-y");
        }
      });
      expect(tracking.controller.request).not.toHaveBeenCalled();
      await Promise.resolve(); expect(tracking.controller.request).toHaveBeenCalledOnce();
      tracking.controller.request.mockClear();
      adapter.setLabelsVisible(true);
      config.onZoom?.({ transform: { k: 2 } } as Parameters<NonNullable<GraphConfig["onZoom"]>>[0], false);
      // The cached rows reproject at once; showing titles reads once after this task.
      expect(labels[0].dataset).toMatchObject({ nodeId: "node:1", pointX: "101", pointY: "201" });
      expect(labels[1].dataset).toMatchObject({ nodeId: "node:0", pointX: "100", pointY: "200" });
      expect(tracking.controller.request).not.toHaveBeenCalled();
      await Promise.resolve(); expect(tracking.controller.request).toHaveBeenCalledOnce();
    });
  });

  it("reprojects cached GPU-tracked labels immediately on ordinary native zoom without an extra GPU read", async () => {
    let scale = 1;
    setTrackedRows(Array.from({ length: 40 }, (_, index): [number, [number, number]] => [index, [100 + index * 10, 109]]));
    graph.spaceToScreenPosition.mockImplementation(([x, y]) => [x * scale, y * scale]);
    await mount(sizedColumns(60));
    const [container, config] = createGraph.mock.calls[0];
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 1_000, 500));
    const label = container.querySelector<HTMLElement>('[data-node-id="node:0"]')!;
    const candidateIds = () => Array.from(container.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span"))
      .map(element => element.dataset.nodeId).filter(Boolean);
    const initialIds = candidateIds();
    expect(initialIds).toHaveLength(40);
    await vi.waitFor(() => {
      expect(label.dataset.pointX).toBe("100");
      expect(label.dataset.pointY).toBe("109");
    });
    graph.getTrackedPointPositionsMap.mockClear();
    const { request } = vi.mocked(nativeTracking.createWorkspaceTrackedPositions).mock.results.at(-1)!.value;
    request.mockClear();
    const projectionCalls = graph.spaceToScreenPosition.mock.calls.length;
    scale = 2;
    // This is an ordinary SDK camera callback, not an initial-Fit positions frame.
    config.onZoom?.({ transform: { k: 2 } } as Parameters<NonNullable<GraphConfig["onZoom"]>>[0], false);
    expect(label.dataset.pointX).toBe("200");
    expect(label.dataset.pointY).toBe("218");
    expect(label.style.transform).toBe("translate(208px, 209px)");
    expect(graph.spaceToScreenPosition.mock.calls.length - projectionCalls).toBe(40);
    expect(graph.getTrackedPointPositionsMap).not.toHaveBeenCalled();
    await Promise.resolve(); expect(request).not.toHaveBeenCalled();
    expect(candidateIds()).toEqual(initialIds);
    expect(container.querySelectorAll("[data-workspace-map-labels] span")).toHaveLength(40);
  });

  it("restricts search labels to matching nodes, reserves interaction titles, and restores ordinary labels on clear", async () => {
    const input = columns();
    input.nodeIds = Array.from({ length: 60 }, (_, index) => `node-${index}`);
    input.nodeKeys = [...input.nodeIds]; input.nodeLabels = input.nodeIds.map((id) => `Title ${id}`);
    input.nodeFolderKeys = input.nodeIds.map(() => null);
    input.nodeKinds = new Uint8Array(60); input.nodeSizes = Float32Array.from({ length: 60 }, (_, index) => 60 - index);
    const adapter = await mount(input);
    const currentIds = () => [...document.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span")]
      .map((label) => label.dataset.nodeId).filter(Boolean);
    const normal = currentIds();
    vi.clearAllMocks();
    adapter.setSearchMatches(new Set(["node-59", "not-visible"]));
    expect(currentIds()).toEqual(["node-59"]);
    adapter.focus("node-1"); expect(currentIds()).toEqual(["node-1", "node-59"]);
    adapter.focus(null); adapter.setSearchMatches(new Set()); expect(currentIds()).toEqual([]);
    adapter.setSearchMatches(null); expect(currentIds()).toEqual(normal);
    for (const method of [graph.setPointPositions, graph.setLinks, graph.setPinnedPoints,
      graph.fitView, graph.setZoomLevel, graph.start, graph.stop, community.compute]) expect(method).not.toHaveBeenCalled();
  });

  it.each(["select", "focus", "edge-focus"] as const)(
    "reserves direct endpoint labels beyond the global top40 for %s without losing context or mutating the scene",
    async interaction => {
      const input = sizedColumns(60, 1);
      input.edgeSources[0] = 58; input.edgeTargets[0] = 59;
      const adapter = await mount(input);
      const currentIds = () => [...document.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span")]
        .map(label => label.dataset.nodeId).filter(Boolean);
      const normal = currentIds();
      expect(normal).toEqual(Array.from({ length: 40 }, (_, index) => `node:${index}`));
      const positions = graph.getPointPositions();
      const viewport = adapter.getViewport();
      const activate = () => {
        if (interaction === "select") adapter.select("node:58");
        else adapter.focus(interaction === "edge-focus" ? "edge:0" : "node:58");
      };
      const clear = () => {
        if (interaction === "select") adapter.select(null);
        else adapter.focus(null);
      };
      vi.clearAllMocks();
      // Searching does not admit unrelated neighbors; explicit node titles
      // still outrank matches, and focusing an edge does not invent a title.
      adapter.setSearchMatches(new Set(["node:5"]));
      activate();
      expect(currentIds()).toEqual(interaction === "edge-focus" ? ["node:5"] : ["node:58", "node:5"]);
      clear(); adapter.setSearchMatches(null);
      expect(currentIds()).toEqual(normal);
      activate();
      expect(graph.getPointPositions()).toEqual(positions);
      expect(adapter.getViewport()).toEqual(viewport);
      for (const mutation of [graph.setPointPositions, graph.setLinks, graph.setPinnedPoints,
        graph.fitView, graph.setZoomLevel, graph.setZoomTransformByPointPositions,
        graph.start, graph.stop, graph.pause, community.compute, community.cancel, createGraph]) {
        expect(mutation).not.toHaveBeenCalled();
      }
      expect(currentIds().slice(0, 2)).toEqual(["node:58", "node:59"]);
      expect(currentIds()).toHaveLength(40);
      expect(currentIds()).toEqual(["node:58", "node:59", ...normal.slice(0, 38)]);
      expect(graph.trackPointPositionsByIndices).toHaveBeenLastCalledWith([58, 59,
        ...Array.from({ length: 38 }, (_, index) => index)]);
      clear();
      expect(currentIds()).toEqual(normal);
      expect(graph.getPointPositions()).toEqual(positions);
      expect(adapter.getViewport()).toEqual(viewport);
      for (const mutation of [graph.setPointPositions, graph.setLinks, graph.setPinnedPoints,
        graph.fitView, graph.setZoomLevel, graph.setZoomTransformByPointPositions,
        graph.start, graph.stop, graph.pause, community.compute, community.cancel, createGraph]) {
        expect(mutation).not.toHaveBeenCalled();
      }
    },
  );

  it("blocks ordinary labels overlapping the actual rectangle of a mandatory multiline title", async () => {
    let now = 1_000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const input = columns();
    input.nodeLabels[0] = "A complete document title that occupies three lines in the rendered label. ".repeat(2);
    const adapter = await mount(input);
    const container = createGraph.mock.calls[0][0];
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 500, 300));
    setTrackedRows([[0, [100, 109]], [1, [100, 139]]]);
    graph.spaceToScreenPosition.mockImplementation(position => position);
    const full = container.querySelector<HTMLElement>('[data-node-id="a"]')!;
    const ordinary = container.querySelector<HTMLElement>('[data-node-id="b"]')!;
    Object.defineProperty(full, "offsetHeight", { configurable: true,
      get: () => full.style.display === "none" ? 0 : 54 });
    now += 200;
    adapter.select("a"); await trackedDelivery();
    expect(full.style.display).toBe("block");
    expect(full.style.transform).toMatch(/, 100px\)$/);
    expect(full.dataset.fullTitle).toBe("true");
    expect(full.textContent).toBe(input.nodeLabels[0]);
    // Its occupied y-range is [100,154), so an ordinary label anchored
    // at y=130 overlaps even though their top coordinates differ by >18px.
    expect(ordinary.style.display).toBe("none");
  });

  it("fits newly admitted Korean and multiline titles using their first painted DOM rectangle", async () => {
    let now = 1_000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const input = sizedColumns(2_000);
    input.nodeLabels[1_999] = "한글문서제목경계확인";
    const adapter = await mount(input);
    const container = createGraph.mock.calls[0][0];
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 300, 100));
    setTrackedRows([[1_999, [290, 99]]]);
    graph.spaceToScreenPosition.mockImplementation(position => position);
    const label = container.querySelector<HTMLElement>("[data-workspace-map-labels] span")!;
    Object.defineProperties(label, {
      offsetWidth: { configurable: true, get: () => label.style.display === "none" ? 0 : 132 },
      offsetHeight: { configurable: true, get: () => label.style.display === "none" ? 0 : 54 },
    });
    graph.getPointPositions.mockClear();
    graph.setPointPositions.mockClear();
    graph.setLinks.mockClear();
    graph.setPinnedPoints.mockClear();
    graph.setZoomTransformByPointPositions.mockClear();
    community.compute.mockClear();
    now += 200;
    adapter.select("node:1999"); await trackedDelivery();
    // Real display:none geometry is zero. The first paint, not a later timer
    // correction, must reserve the complete measured 132x54 title rectangle.
    expect(label.dataset.nodeId).toBe("node:1999");
    expect(label.textContent).toBe(input.nodeLabels[1_999]);
    expect(label.style.display).toBe("block");
    expect(label.style.visibility).not.toBe("hidden");
    expect(label.style.transform).toBe("translate(168px, 46px)");
    for (const mutation of [graph.getPointPositions, graph.setPointPositions, graph.setLinks,
      graph.setPinnedPoints, graph.setZoomTransformByPointPositions, community.compute]) {
      expect(mutation).not.toHaveBeenCalled();
    }
  });

  it.each([390, 734])("preserves focus and selection full titles during hover without overprinting at %ipx", async width => {
    let now = 1_000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const input = columns(); input.nodeLabels = input.nodeLabels.map((title) => `${title} ${"complete long title ".repeat(8)}`);
    const adapter = await mount(input), [container, config] = createGraph.mock.calls[0];
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, width, 600));
    setTrackedRows([[0, [width - 12, 440]], [1, [width - 12, 455]], [2, [width - 12, 470]]]);
    graph.spaceToScreenPosition.mockImplementation(position => position);
    for (const element of container.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span")) {
      Object.defineProperties(element, {
        offsetWidth: { configurable: true, get: () => element.style.display === "none" ? 0 : Math.min(374, width - 16) },
        offsetHeight: { configurable: true, get: () => element.style.display === "none" ? 0 : 58 },
      });
    }
    const positions = graph.getPointPositions(), viewport = adapter.getViewport();
    graph.setPointPositions.mockClear(); graph.setLinks.mockClear(); graph.setPinnedPoints.mockClear();
    graph.setZoomTransformByPointPositions.mockClear(); graph.setZoomLevel.mockClear(); community.compute.mockClear();
    now += 200; adapter.select("a"); now += 200; adapter.focus("b"); now += 200;
    config.onPointMouseOver?.(2, [width - 12, 470], undefined, false, false); await trackedDelivery();
    const titles = [...container.querySelectorAll<HTMLElement>('[data-full-title="true"]')];
    expect(titles.map(element => element.dataset.nodeId)).toEqual(["b", "a"]);
    expect(titles.map(element => element.dataset.labelPriority)).toEqual(["active", "explicit"]);
    const rectangles = titles.map(element => {
      expect(element.style.display).toBe("block"); expect(element.dataset.placementReason).toBe("placed");
      expect(element.textContent).toBe(input.nodeLabels[input.nodeIds.indexOf(element.dataset.nodeId!)]);
      const [, x, y] = element.style.transform.match(/translate\(([\d.]+)px, ([\d.]+)px\)/)!;
      expect(Number(x) + Math.min(374, width - 16)).toBeLessThanOrEqual(width);
      expect(Number(y) + 58).toBeLessThanOrEqual(600);
      return { x: Number(x), y: Number(y) };
    });
    for (let index = 0; index < rectangles.length; index += 1) for (const other of rectangles.slice(index + 1)) {
      expect(rectangles[index].y + 63 <= other.y || other.y + 63 <= rectangles[index].y
        || rectangles[index].x + Math.min(374, width - 16) + 5 <= other.x
        || other.x + Math.min(374, width - 16) + 5 <= rectangles[index].x).toBe(true);
    }
    expect(titles[0].dataset.pointY).toBe("455");
    expect(container.querySelectorAll("[data-workspace-map-labels] span")).toHaveLength(40);
    expect(graph.getPointPositions()).toEqual(positions); expect(adapter.getViewport()).toEqual(viewport);
    for (const mutation of [graph.setPointPositions, graph.setLinks, graph.setPinnedPoints,
      graph.setZoomTransformByPointPositions, graph.setZoomLevel, community.compute]) expect(mutation).not.toHaveBeenCalled();
  });

  it("keeps oversized active full text visible and clears constrained metadata when the title changes", async () => {
    let now = 1_000; vi.spyOn(performance, "now").mockImplementation(() => now);
    const input = columns(); input.nodeLabels[0] = "Oversized full title ".repeat(40);
    const adapter = await mount(input), container = createGraph.mock.calls[0][0];
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 300, 100));
    setTrackedRows([[0, [290, 99]], [1, [100, 50]]]);
    graph.spaceToScreenPosition.mockImplementation(position => position);
    const label = container.querySelector<HTMLElement>("[data-workspace-map-labels] span")!;
    Object.defineProperties(label, {
      offsetWidth: { configurable: true, get: () => 284 },
      offsetHeight: { configurable: true, get: () => label.textContent === input.nodeLabels[0] ? 150 : 20 },
    });
    now += 200; adapter.select("a"); await trackedDelivery();
    expect(label.style.display).toBe("block"); expect(label.textContent).toBe(input.nodeLabels[0]);
    expect(label.title).toBe(input.nodeLabels[0]); expect(label.dataset.viewportConstrained).toBe("true");
    expect(label.dataset.placementReason).toBe("viewport-constrained"); expect(label.style.maxHeight).toBe("100px");
    // No new readback is available yet; changing active intent must still
    // retire metadata belonging to the previous title rather than reuse it.
    adapter.focus("b");
    expect(label.dataset.nodeId).toBe("b"); expect(label.dataset.viewportConstrained).toBeUndefined();
    expect(label.dataset.placementReason).toBe("placed"); expect(label.style.maxHeight).toBe("");
  });

  it.each([false, true])("outlines measured labels without covering edges in forcedColors=%s", async forcedColors => {
    const container = document.createElement("div"); container.style.color = "rgb(0, 0, 0)";
    container.style.backgroundColor = "rgb(255, 255, 255)"; document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, columns(), { forcedColors, reducedMotion: true,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    const label = container.querySelector<HTMLElement>("[data-workspace-map-labels] span")!;
    expect(label.style.backgroundColor).toBe("transparent");
    expect(label.style.webkitTextStroke).toBe(`3px ${forcedColors ? "Canvas" : "#ffffff"}`);
    expect(label.style.paintOrder).toBe("stroke fill");
    expect(label.style.color).toBe("rgb(0, 0, 0)"); expect(label.style.padding).toBe("2px 4px");
    expect(label.style.boxSizing).toBe("border-box"); expect(label.style.forcedColorAdjust).not.toBe("none");
    expect(label.parentElement!.getAttribute("aria-hidden")).toBe("true");
  });

  it("emphasizes matching visible nodes and relations without touching geometry, camera, pins or the worker", async () => {
    const adapter = await mount();
    adapter.pin("a", true, { x: 1234, y: 2345 });
    const before = graph.getPointPositions();
    const viewport = adapter.getViewport();
    vi.clearAllMocks();

    adapter.setSearchMatches(new Set(["a", "b", "hidden-id"]));
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: [0, 1], highlightedLinkIndices: [0],
      pointGreyoutOpacity: 0.55,
    }));
    adapter.setSearchMatches(new Set());
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: [], highlightedLinkIndices: [],
    }));
    adapter.setSearchMatches(null);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: undefined, highlightedLinkIndices: undefined,
    }));
    expect(graph.getPointPositions()).toEqual(before);
    expect(adapter.getViewport()).toEqual(viewport);
    for (const method of [graph.setPointPositions, graph.setPointColors, graph.setLinks,
      graph.setPinnedPoints, graph.fitView, graph.setZoomLevel,
      graph.setZoomTransformByPointPositions, graph.start, graph.stop, graph.pause,
      community.compute, createGraph]) expect(method).not.toHaveBeenCalled();
  });

  it("still replaces layout identity on real server-filter or actor changes", async () => {
    const adapter = await mount();
    const filtered = columns();
    filtered.identity.selectionKey = "links:server-filtered";
    adapter.replaceColumns(filtered);
    expect(graph.stop).toHaveBeenCalledOnce();
    expect(graph.fitView).toHaveBeenCalledTimes(2);
    expect(community.compute).toHaveBeenCalledTimes(2);
    const nextActor = columns();
    nextActor.identity.actorId = "actor-b";
    adapter.replaceColumns(nextActor);
    expect(graph.stop).toHaveBeenCalledTimes(2);
    expect(community.compute).toHaveBeenCalledTimes(3);
  });
});

describe("Cosmos Map native lifecycle and semantic ownership", () => {
  it.each(["fractional", "integer"] as const)("roundtrips the SDK camera using its client-size center for %s CSS bounds", async (boundsKind) => {
    const width = 734, height = 608;
    const { container, zoom } = installedSdkCamera(width, height);
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0,
      boundsKind === "fractional" ? 733.671875 : width,
      boundsKind === "fractional" ? 607.671875 : height));
    const initial = { x: 74.8436608579463, y: 85.125, k: .4330218600670054 };
    zoom.eventTransform = initial;
    const adapter = createWorkspaceCosmosAdapter(container, columns(), { forcedColors: false,
      reducedMotion: true, callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    const viewport = adapter.getViewport();
    expect(viewport).not.toBeNull();
    adapter.setViewport(viewport!);
    // Preserve the existing real-app precision. Float32's expected error at
    // these world coordinates is <.000053px, not the .164px CSS-center drift.
    expect(zoom.eventTransform.k).toBe(initial.k);
    expect(zoom.eventTransform.x).toBeCloseTo(initial.x, 2);
    expect(zoom.eventTransform.y).toBeCloseTo(initial.y, 2);
    expect(graph.screenToSpacePosition).toHaveBeenLastCalledWith([width / 2, height / 2]);
  });

  it("does not re-persist Float32 camera quantization as new intent after an asynchronous native restore", async () => {
    const saved = { zoom: .17320874402680214,
      pan: { x: Number("2041.9024963378906"), y: Number("2044.6194458007812") } };
    let stored = saved;
    const sdk = installedSdkCamera();
    graph.fitView.mockImplementationOnce(() => sdk.end()); // initial internal fit is not saved intent either
    const onViewport = vi.fn((viewport) => { stored = viewport; });
    const adapter = createWorkspaceCosmosAdapter(sdk.container, columns(), { forcedColors: false,
      reducedMotion: true, initialPaused: true, initialViewport: saved,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn(), onViewport } });
    adapters.push(adapter); await adapter.whenSettled();
    // The SDK's duration=0 transition still ends asynchronously. Execute its
    // shipped Zoom end handler after readiness, with no user source event.
    await new Promise<void>(resolve => requestAnimationFrame(() => { sdk.end(); resolve(); }));
    expect(adapter.getViewport()!.pan).not.toEqual(saved.pan); // actual Float32 native readback
    expect(stored).toEqual(saved); // restoration is not new navigation intent
    expect(onViewport).not.toHaveBeenCalled();
  });

  it.each(["native-user", "pointerdown", "wheel", "fit", "center", "zoom", "reset", "scene-change"] as const)(
    "publishes genuine %s camera intent after a saved viewport restore", async (intent) => {
      const sdk = installedSdkCamera(), onViewport = vi.fn();
      const adapter = createWorkspaceCosmosAdapter(sdk.container, columns(), { forcedColors: false,
        reducedMotion: true, initialViewport: { zoom: .3,
          pan: { x: Number("2041.9024963378906"), y: Number("2044.6194458007812") } },
        callbacks: { onHover: vi.fn(), onSelect: vi.fn(), onViewport } });
      adapters.push(adapter); await adapter.whenSettled();
      sdk.end(); onViewport.mockClear();
      // This test exercises the installed SDK's asynchronous camera end,
      // rather than the default mock's immediate end used by other tests.
      graph.fitView.mockImplementation(() => {});
      graph.setZoomTransformByPointPositions.mockImplementation(() => {});
      if (intent === "fit") adapter.fit();
      else if (intent === "center") adapter.centerOn("a");
      else if (intent === "zoom") adapter.zoomBy(1.2);
      else if (intent === "reset") adapter.reset();
      else if (intent === "scene-change") {
        const next = columns(); next.identity.visibleFingerprint = "new-visible-scene";
        adapter.replaceColumns(next);
      } else if (intent !== "native-user") sdk.container.dispatchEvent(new Event(intent));
      sdk.zoom.eventTransform = { x: 101.125, y: 202.25, k: .4 };
      sdk.end(intent === "native-user" ? new WheelEvent("wheel") : null);
      expect(onViewport).toHaveBeenCalledExactlyOnceWith(adapter.getViewport());
    },
  );

  it.each([true, false])("restores finite session coordinates instead of replaying worker starts (workerBeforeReady=%s)", async (workerBeforeReady) => {
    const input = columns();
    const actual = Float32Array.from([333, 444, 555, 666, 777, 888]);
    const computed = Float32Array.from([1500, 1600, 1700, 1800, 1900, 2000]);
    const outcome: WorkspaceMapCommunityOutcome = { kind: "complete", identityKey: JSON.stringify([
      input.identity.workspaceId, input.identity.actorId, input.identity.surface,
      input.identity.selectionKey, input.identity.visibleFingerprint]), initialPositions: computed,
    communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 };
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    if (workerBeforeReady) community.compute.mockResolvedValue(outcome);
    else community.compute.mockReturnValue(new Promise(complete => { resolve = complete; }));
    const container = document.createElement("div"); document.body.appendChild(container);
    const viewport = { zoom: .3, pan: { x: 444, y: 555 } };
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false,
      reducedMotion: true, initialViewport: viewport,
      initialCoordinates: { columns: input, positions: actual },
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    expect(graph.getPointPositions()).toEqual(actual);
    if (!workerBeforeReady) { resolve(outcome); await new Promise(done => setTimeout(done, 0)); }
    expect(graph.getPointPositions()).toEqual(actual);
    expect(graph.getPointPositions()).not.toEqual(computed);
    adapter.stopLayout(); adapter.resumeLayout();
    expect(graph.getPointPositions()).toEqual(actual);
  });

  it.each(["actor", "workspace", "selection", "fingerprint", "geometry", "semantic-owner", "nonfinite", "length"])(
    "rejects a session coordinate snapshot with changed %s",
    async (change) => {
      const previous = columns(), next = columns();
      let positions = Float32Array.from([333, 444, 555, 666, 777, 888]);
      if (change === "actor") next.identity.actorId = "another-actor";
      if (change === "workspace") next.identity.workspaceId = "another-workspace";
      if (change === "selection") next.identity.selectionKey = "another-lens";
      if (change === "fingerprint") next.identity.visibleFingerprint = "new-visible-input";
      if (change === "geometry") next.edgeWeights[0] = 2;
      if (change === "semantic-owner") next.nodeKeys[0] = "context:new-owner";
      if (change === "nonfinite") positions[2] = Number.NaN;
      if (change === "length") positions = positions.slice(0, 4);
      const container = document.createElement("div"); document.body.appendChild(container);
      const adapter = createWorkspaceCosmosAdapter(container, next, { forcedColors: false,
        reducedMotion: true, initialCoordinates: { columns: previous, positions, collisionBasis: { radius: 9999, padding: 9999 },
          forceBasis: { radius: 8888, padding: 8888 } },
        callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
      adapters.push(adapter); await adapter.whenSettled();
      expect(graph.getPointPositions()).not.toEqual(positions);
      expect(graph.getPointPositions().every(Number.isFinite)).toBe(true);
      expect(adapter.captureCoordinates()!.collisionBasis).toEqual(freshCollisionBasis(next));
      expect(adapter.captureCoordinates()!.forceBasis).toEqual(freshCollisionBasis(next));
    },
  );

  it("retains restored coordinates while a user-paused worker waits through preference changes and Resume", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    const actual = Float32Array.from([333, 444, 555, 666, 777, 888]);
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValue(new Promise(complete => { resolve = complete; }));
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false,
      reducedMotion: false, initialPaused: true, initialCoordinates: { columns: input, positions: actual },
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    resolve({ kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId,
      input.identity.actorId, input.identity.surface, input.identity.selectionKey,
      input.identity.visibleFingerprint]), initialPositions: Float32Array.from([1500, 1600, 1700, 1800, 1900, 2000]),
    communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 });
    await new Promise(done => setTimeout(done, 0));
    expect(container.dataset.mapCommunityLayoutStatus).toBe("queued");
    adapter.setReducedMotion(true); adapter.setReducedMotion(false);
    expect(sdk.running()).toBe(false);
    expect(graph.getPointPositions()).toEqual(actual);
    vi.clearAllMocks(); adapter.resumeLayout();
    expect(sdk.running()).toBe(true);
    expect(graph.getPointPositions()).toEqual(actual);
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(graph.setPointClusters).not.toHaveBeenCalled();
    expect(graph.setClusterPositions).not.toHaveBeenCalled();
    expect(graph.setLinkStrength).not.toHaveBeenCalled();
    expect(forceCommits()).toEqual([]);
  });

  it.each([[2_001, 2], [3, 6_001]])("rejects the default-budget %i/%i scene before GPU creation or snapshot restoration", (count, edgeCount) => {
    const input = sizedColumns(count, edgeCount), positions = new Float32Array(count * 2).fill(333);
    const container = document.createElement("div"); document.body.appendChild(container);
    expect(() => createProductWorkspaceCosmosAdapter(container, input, { forcedColors: false,
      reducedMotion: true, initialCoordinates: { columns: input, positions },
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } })).toThrow("exceeds its requested budget");
    expect(createGraph).not.toHaveBeenCalled();
    expect(community.compute).not.toHaveBeenCalled();
    expect(graph.setPointPositions).not.toHaveBeenCalled();
  });

  it.each([[10_001, 2], [3, 100_001]])("rejects the explicit-PC %i/%i scene before GPU construction", (count, edgeCount) => {
    const container = document.createElement("div"); document.body.appendChild(container);
    expect(() => createWorkspaceCosmosAdapter(container, sizedColumns(count, edgeCount), {
      budget: WORKSPACE_MAP_PC_10K_BUDGET, forcedColors: false, reducedMotion: false,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } })).toThrow("exceeds its requested budget");
    expect(createGraph).not.toHaveBeenCalled();
    expect(community.compute).not.toHaveBeenCalled();
  });

  it("admits exact PC 10K/100K native GPU physics and scoped copied snapshots", async () => {
    const sdk = installedSdkLifecycle(), input = sizedColumns(10_000, 100_000);
    const budget = { ...WORKSPACE_MAP_PC_10K_BUDGET };
    const adapter = await mount(input, false, vi.fn(), false, vi.fn(), budget);
    expect(createNativeGraph).toHaveBeenCalledOnce();
    expect(sdk.running()).toBe(true);
    expect(collisionCommits()).toHaveLength(1);
    expect(graph.setPointCollisionRadii).toHaveBeenLastCalledWith(expect.any(Float32Array));
    expect(graph.setPointCollisionRadii.mock.lastCall![0]).toHaveLength(10_000);
    expect(collisionCommits()[0]).toMatchObject({ simulationCollisionRadius: 0 });
    expect(forceCommits()).toEqual([expect.objectContaining({ simulationGravity: .25,
      simulationRepulsion: 1, simulationLinkSpring: 1, simulationLinkDistance: 10 })]);
    expect(graph.setLinks).toHaveBeenLastCalledWith(expect.any(Float32Array));
    expect(graph.setLinks.mock.lastCall![0]).toHaveLength(200_000);
    expect(graph.getPointPositions()).toHaveLength(20_000);
    const snapshot = adapter.captureCoordinates()!;
    expect(snapshot.budget).toEqual(WORKSPACE_MAP_PC_10K_BUDGET);
    expect(snapshot.budget).not.toBe(budget);
    budget.documents = 2_000;
    expect(snapshot.budget!.documents).toBe(10_000);
    adapter.stopLayout(); expect(sdk.running()).toBe(false);
    adapter.resumeLayout(); expect(sdk.running()).toBe(true);
    adapter.destroy();
    const container = document.createElement("div"); document.body.appendChild(container);
    const restored = createWorkspaceCosmosAdapter(container, input, {
      budget: WORKSPACE_MAP_PC_10K_BUDGET, forcedColors: false, reducedMotion: true,
      initialCoordinates: snapshot, callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(restored); await restored.whenSettled();
    expect(graph.getPointPositions()).toEqual(snapshot.positions);
    expect(restored.captureCoordinates()!.budget).toEqual(WORKSPACE_MAP_PC_10K_BUDGET);
  });

  it.each([
    { width: 734, height: 608, glyph: 3, zoom: .2, textureLimit: 16_384 },
    { width: 1_200, height: 800, glyph: 8.5, zoom: 100, textureLimit: 16_384 },
    { width: 390, height: 800, glyph: 1.5, zoom: .001, textureLimit: 4_096 },
  ])("keeps PC canonical force units independent of glyphs, viewport and saved camera %j", async scenario => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(nativeContext(scenario.textureLimit));
    const input = sizedColumns(10_000, 30_895);
    input.nodeSizes.fill(scenario.glyph);
    const container = document.createElement("div"), canvas = document.createElement("canvas");
    Object.defineProperties(canvas, { clientWidth: { value: scenario.width }, clientHeight: { value: scenario.height } });
    container.appendChild(canvas); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { budget: WORKSPACE_MAP_PC_10K_BUDGET,
      forcedColors: false, reducedMotion: true, initialViewport: { zoom: scenario.zoom, pan: { x: 2000, y: 2100 } },
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    const scale = scenario.textureLimit === 4_096 ? .5 : 1;
    expect(forceCommits()).toEqual([expect.objectContaining({ simulationGravity: .25,
      simulationRepulsion: scale ** 2, simulationLinkSpring: 1, simulationLinkDistance: 10 * scale,
      simulationLinkDistRandomVariationRange: [1, 1] })]);
    const radii = graph.setPointCollisionRadii.mock.lastCall![0] as Float32Array;
    expect(radii).toHaveLength(10_000);
    expect(radii.every(value => Number.isFinite(value) && value > 0)).toBe(true);
  });

  it.each([
    [WORKSPACE_MAP_DEFAULT_BUDGET, 2_001, 2],
    [WORKSPACE_MAP_DEFAULT_BUDGET, 3, 6_001],
    [WORKSPACE_MAP_PC_10K_BUDGET, 10_001, 2],
    [WORKSPACE_MAP_PC_10K_BUDGET, 3, 100_001],
  ])("keeps the current scene unchanged when replacement exceeds budget %o at %i/%i", async (budget, count, edgeCount) => {
    const adapter = await mount(columns(), true, vi.fn(), false, vi.fn(), budget);
    const positions = graph.getPointPositions(), snapshot = adapter.captureCoordinates()!;
    vi.clearAllMocks();
    expect(() => adapter.replaceColumns(sizedColumns(count, edgeCount))).toThrow("exceeds its requested budget");
    expect(graph.getPointPositions()).toEqual(positions);
    expect(adapter.captureCoordinates()!.columns.identity).toEqual(snapshot.columns.identity);
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(graph.setLinks).not.toHaveBeenCalled();
    expect(graph.setPinnedPoints).not.toHaveBeenCalled();
    expect(community.compute).not.toHaveBeenCalled();
  });

  it.each(["pc-to-default", "legacy-to-pc", "edges", "nodes"])("rejects %s snapshot provenance without reusing coordinates or collision basis", async (change) => {
    const input = columns(), positions = Float32Array.from([333, 444, 555, 666, 777, 888]);
    const snapshotColumns = change === "edges" ? sizedColumns(3, 6_001)
      : change === "nodes" ? sizedColumns(2_001) : input;
    const budget = change === "legacy-to-pc" ? WORKSPACE_MAP_PC_10K_BUDGET : WORKSPACE_MAP_DEFAULT_BUDGET;
    const snapshotBudget = change === "pc-to-default" ? WORKSPACE_MAP_PC_10K_BUDGET : undefined;
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, { budget, forcedColors: false,
      reducedMotion: true, initialCoordinates: { budget: snapshotBudget, columns: snapshotColumns,
        positions, collisionBasis: { radius: 9999, padding: 9999 }, forceBasis: { radius: 8888, padding: 8888 } },
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    expect(graph.getPointPositions()).not.toEqual(positions);
    const glyphs = buildCosmosSceneBuffers(input, readGraphTokens(document.body, "light"));
    const freshBasis = budget === WORKSPACE_MAP_DEFAULT_BUDGET
      ? buildWorkspaceRelationCollisionBasis(workspaceDirectoryStartPositions(workspaceContainerIndices(input), input.nodeKeys), glyphs.pointSizes, 734, 608)
      : freshCollisionBasis(input);
    expect(adapter.captureCoordinates()!.collisionBasis).toEqual(freshBasis);
    expect(adapter.captureCoordinates()!.forceBasis).toEqual(freshBasis);
  });

  it("captures copied actual GPU coordinates and immutable visible ownership, not the initial worker seed", async () => {
    const input = columns(), adapter = await mount(input);
    adapter.pin("a", true, { x: 333, y: 444 });
    const actual = graph.getPointPositions(), snapshot = adapter.captureCoordinates();
    expect(snapshot).not.toBeNull();
    expect(snapshot!.positions).toEqual(actual);
    expect(snapshot!.positions).not.toBe(actual);
    expect(snapshot!.columns).not.toBe(input);
    expect(snapshot!.columns.identity).not.toBe(input.identity);
    expect(snapshot!.columns.nodeKeys).not.toBe(input.nodeKeys);
    expect(snapshot!.columns.edgeWeights).not.toBe(input.edgeWeights);
    input.identity.actorId = "another-actor"; input.nodeKeys[0] = "another-owner"; input.edgeWeights[0] = 99;
    expect(snapshot!.columns.identity.actorId).toBe("actor-a");
    expect(snapshot!.columns.nodeKeys[0]).toBe("context:a");
    expect(snapshot!.columns.edgeWeights[0]).toBe(1);
    snapshot!.positions[0] = 999;
    expect(graph.getPointPositions()).toEqual(actual);
  });

  it.each(["nonfinite", "length", "throw", "destroy"])("does not capture an invalid %s GPU snapshot", async (failure) => {
    const adapter = await mount();
    if (failure === "nonfinite") graph.getPointPositions.mockReturnValueOnce(Float32Array.from([NaN, 1, 2, 3, 4, 5]));
    if (failure === "length") graph.getPointPositions.mockReturnValueOnce(Float32Array.from([1, 2]));
    if (failure === "throw") graph.getPointPositions.mockImplementationOnce(() => { throw new Error("lost GPU"); });
    if (failure === "destroy") adapter.destroy();
    expect(adapter.captureCoordinates()).toBeNull();
  });

  it("demonstrates the installed SDK's global interruption contract and its non-rendering create path", () => {
    const ended = vi.fn();
    const transition = new InstalledTransition({ transitionDuration: 1_500, onTransitionEnd: ended });
    transition.queue("positions"); transition.start();
    expect(transition.isActiveFor("positions")).toBe(true);
    transition.queue("linkColors"); transition.setDurationOverride(0); transition.start();
    expect(transition.isActiveFor("positions")).toBe(false);
    expect(ended).toHaveBeenCalledWith(true);
    const source = sdkSource("../src/index.ts");
    const create = source.slice(source.indexOf("public create ()"), source.indexOf("public flatten ("));
    expect(create).toContain("this.lines.updateColor()");
    expect(create).toContain("this.requestRender()");
    expect(create).not.toContain("this.transition.start()");
    expect(create).not.toContain("this.graph.update()");
    const data = sdkSource("../src/modules/GraphData/index.ts");
    expect(data).toContain("this.linkColors = this.inputLinkColors");
  });

  it("observes the installed SDK waking physics on false-to-true permission changes without start()", async () => {
    const sdk = installedSdkLifecycle();
    await mount(columns(), true);
    expect(sdk.running()).toBe(false);
    graph.start.mockClear();
    graph.setConfigPartial({ enableSimulation: true });
    expect(graph.start).not.toHaveBeenCalled();
    expect(sdk.running()).toBe(true);
    expect(sdk.alpha()).toBe(1);
    graph.pause();
    expect(sdk.running()).toBe(false);
  });

  it("keeps explicit pause across reduced-motion on/off and a data permission change until resume", async () => {
    const sdk = installedSdkLifecycle();
    const adapter = await mount(columns(), false);
    adapter.stopLayout();
    adapter.setReducedMotion(true);
    adapter.setReducedMotion(false);
    expect(sdk.running()).toBe(false);
    expect(sdk.simulationEnabled()).toBe(false);
    const changed = columns();
    changed.identity = { ...changed.identity, visibleFingerprint: "different-visible-input" };
    adapter.replaceColumns(changed);
    await new Promise(done => setTimeout(done, 0));
    expect(sdk.running()).toBe(false);
    expect(sdk.simulationEnabled()).toBe(false);
    adapter.resumeLayout();
    expect(sdk.simulationEnabled()).toBe(true);
    expect(sdk.running()).toBe(true);
    adapter.setReducedMotion(true);
    adapter.resumeLayout();
    expect(sdk.running()).toBe(false);
    expect(sdk.simulationEnabled()).toBe(false);
  });

  it("uploads reordered GPU relation rows even when their semantic identity and projection IDs are unchanged", async () => {
    const initial = columns();
    const adapter = await mount(initial, true);
    vi.clearAllMocks();
    const reordered = { ...initial,
      edgeSources: initial.edgeSources.slice().reverse(), edgeTargets: initial.edgeTargets.slice().reverse(),
      edgeKinds: initial.edgeKinds.slice().reverse(), edgeCounts: initial.edgeCounts.slice().reverse(),
      edgeWeights: initial.edgeWeights.slice().reverse(), edgeCrossFolder: initial.edgeCrossFolder.slice().reverse(),
    };
    adapter.replaceColumns(reordered);
    expect(graph.setLinks).toHaveBeenCalledWith(Float32Array.from([1, 2, 0, 1]));
    expect(graph.render).toHaveBeenCalledWith(undefined, 0);
    adapter.select("ab");
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({ focusedLinkIndex: 0 }));
  });

  it("retains only the applied pin's retired owner through repeated ID-only metadata updates and accepts its removal", async () => {
    const initial = columns();
    const adapter = await mount(initial, true);
    adapter.pin("a", true, { x: 77, y: 88 });
    vi.clearAllMocks();
    for (let index = 1; index <= 30; index += 1) {
      adapter.replaceColumns({ ...initial, nodeIds: [`projection:${index}`, "b", "c"] });
      if (index > 1) {
        adapter.pin(`projection:${index - 1}`, false);
        expect(graph.setPinnedPoints).not.toHaveBeenCalled();
      }
    }
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(graph.render).not.toHaveBeenCalled();
    expect(adapter.pin("a", true)).toBeNull(); // retired IDs cannot take ownership again
    adapter.pin("a", false); // WorkspaceMapCanvas removes its old applied ID
    expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([]);
    graph.setPinnedPoints.mockClear();
    adapter.pin("a", false);
    expect(graph.setPinnedPoints).not.toHaveBeenCalled();
  });

  it("hands retired pin ownership to the current ID and clears every alias on a real scope change", async () => {
    const initial = columns();
    const adapter = await mount(initial, true);
    adapter.pin("a", true, { x: 77, y: 88 });
    adapter.replaceColumns({ ...initial, nodeIds: ["projection:a", "b", "c"] });
    adapter.pin("projection:a", true);
    graph.setPinnedPoints.mockClear();
    adapter.pin("a", false);
    expect(graph.setPinnedPoints).not.toHaveBeenCalled();
    adapter.replaceColumns({ ...initial, nodeIds: ["projection:again", "b", "c"] });
    adapter.pin("projection:a", false);
    expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([]);
    adapter.pin("projection:again", true);
    adapter.replaceColumns({ ...initial, nodeIds: ["projection:last", "b", "c"] });
    adapter.replaceColumns({ ...initial, identity: { ...initial.identity, actorId: "actor-b" } });
    graph.setPinnedPoints.mockClear();
    adapter.pin("projection:again", false);
    expect(graph.setPinnedPoints).not.toHaveBeenCalled();
  });

  it("keeps responsive native canvas below passive labels and cleans context listeners", async () => {
    const container = document.createElement("div"), native = document.createElement("canvas");
    Object.assign(native.style, { width: "100%", height: "100%", touchAction: "none" });
    createGraph.mockImplementationOnce(function () { container.append(native); return graph; });
    document.body.append(container);
    const adapter = createWorkspaceCosmosAdapter(container, columns(), {
      forcedColors: false, reducedMotion: true, callbacks: { onHover: vi.fn(), onSelect: vi.fn() },
    });
    adapters.push(adapter); await adapter.whenSettled();
    const labels = container.querySelector<HTMLElement>("[data-workspace-map-labels]")!;
    expect(native.style).toMatchObject({ position: "absolute", inset: "0", zIndex: "1",
      width: "100%", height: "100%", touchAction: "none", pointerEvents: "" });
    expect(labels.style).toMatchObject({ zIndex: "2", pointerEvents: "none" });
    adapter.resize(); expect(container.querySelectorAll("canvas")).toHaveLength(1);
    const cleanup = vi.spyOn(native, "removeEventListener"); adapter.destroy();
    expect(container.querySelector("[data-workspace-map-labels]")).toBeNull();
    expect(cleanup).toHaveBeenCalledWith("webglcontextlost", expect.any(Function));
    expect(cleanup).toHaveBeenCalledWith("webglcontextrestored", expect.any(Function));
  });

  it("never reheats or pins a context-lost engine through stale native callbacks", async () => {
    const sdk = installedSdkLifecycle(), create = createGraph.getMockImplementation()!;
    createGraph.mockImplementation(function (container, config) {
      container.appendChild(document.createElement("canvas")); return create(container, config);
    });
    const onPin = vi.fn(), adapter = await mount(columns(), false, vi.fn(), false, onPin);
    const config = createGraph.mock.calls[0][1];
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    const drag = installedSdkDrag(config, 0); drag.start(); drag.move(24, 35);
    document.querySelector("canvas")!.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    graph.start.mockClear(); adapter.resumeLayout(); adapter.setReducedMotion(false); drag.end();
    config.onSimulationTick?.(.001);
    expect(sdk.running()).toBe(false); expect(graph.start).not.toHaveBeenCalled(); expect(onPin).not.toHaveBeenCalled();
  });

  it("rebinds node/edge IDs and label metadata DURING native motion without geometry, camera or worker reset", async () => {
    const sdk = installedSdkLifecycle(), initial = columns(), onSelect = vi.fn();
    const adapter = await mount(initial, true, onSelect), config = createGraph.mock.calls[0][1];
    const target = Float32Array.from([500, 600, 700, 800, 900, 1000]);
    graph.setConfigPartial({ transitionDuration: 1500 }); graph.setPointPositions(target); graph.render(undefined, 1500);
    sdk.advance(400); expect(sdk.activePositions()).toBe(true);
    const metadata = { ...initial, nodeIds: initial.nodeIds.map(id => "projection:" + id),
      edgeIds: initial.edgeIds.map(id => "projection:" + id), nodeLabels: initial.nodeLabels.map(label => label + " updated") };
    vi.clearAllMocks(); adapter.replaceColumns(metadata);
    config.onPointClick?.(0, [0, 0], new MouseEvent("click"));
    expect(onSelect).toHaveBeenLastCalledWith("projection:a");
    adapter.select("projection:b");
    expect([...document.querySelectorAll<SVGSVGElement>("[data-workspace-map-selection-ring]")].at(-1)!.dataset.selectedPoint).toBe("1");
    adapter.focus("projection:a"); adapter.setSearchMatches(new Set(["projection:a"])); adapter.resize();
    expect(graph.setConfigPartial).toHaveBeenCalledWith(expect.objectContaining({ highlightedPointIndices: expect.arrayContaining([0]) }));
    config.onLinkClick?.(0, new MouseEvent("click", { clientX: 100, clientY: 100 }));
    expect(onSelect).toHaveBeenLastCalledWith("projection:ab");
    adapter.select("projection:ab"); adapter.focus("projection:ab");
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({ focusedLinkIndex: 0, highlightedLinkIndices: [0] }));
    adapter.setSearchMatches(null); adapter.select("ab"); adapter.focus("ab");
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({ focusedLinkIndex: undefined, highlightedLinkIndices: undefined }));
    for (const method of [graph.setPointPositions, graph.render, graph.fitView,
      graph.setZoomTransformByPointPositions, graph.setPinnedPoints, community.compute, community.cancel]) expect(method).not.toHaveBeenCalled();
    expect(sdk.activePositions()).toBe(true); expect(sdk.interruptions).not.toContain(true);
    sdk.advance(1100); expect(graph.getPointPositions()).toEqual(target);
  });

  it("queues a post-ready relation community without changing paused coordinates/camera and resumes it exactly once", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValue(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input, false); adapter.stopLayout();
    adapter.pin("a", true, { x: 333, y: 444 });
    const before = graph.getPointPositions();
    vi.clearAllMocks();
    const target = Float32Array.from([500, 600, 700, 800, 900, 1000]);
    resolve({ kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId, input.identity.actorId,
      input.identity.surface, input.identity.selectionKey, input.identity.visibleFingerprint]),
      initialPositions: target, communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 });
    await new Promise(done => setTimeout(done, 0));
    expect(graph.getPointPositions()).toEqual(before); expect(sdk.running()).toBe(false);
    const container = document.body.lastElementChild as HTMLDivElement;
    expect(container.dataset.mapCommunityCount).toBe("1");
    expect(container.dataset.mapCommunityLayoutStatus).toBe("queued");
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(graph.fitView).not.toHaveBeenCalled(); expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    adapter.resumeLayout(); await new Promise(done => setTimeout(done, 0));
    const withPin = target.slice(); withPin[0] = 333; withPin[1] = 444;
    expect(graph.getPointPositions()).toEqual(withPin); expect(sdk.running()).toBe(true);
    expect(container.dataset.mapCommunityLayoutStatus).toBe("applied");
    expect(graph.setPointPositions).toHaveBeenCalledTimes(1);
    expect(forceCommits()).toEqual([]);
    for (const method of [graph.setLinkStrength, graph.setPointClusters, graph.setClusterPositions]) {
      expect(method).not.toHaveBeenCalled();
    }
    vi.clearAllMocks(); adapter.resumeLayout(); await new Promise(done => setTimeout(done, 0));
    expect(graph.getPointPositions()).toEqual(withPin);
    for (const method of [graph.setPointPositions, graph.setZoomTransformByPointPositions,
      graph.fitView, graph.setLinkStrength, graph.setPointClusters, graph.setClusterPositions,
      community.compute, community.cancel]) expect(method).not.toHaveBeenCalled();
    expect(forceCommits()).toEqual([]);
  });

  it("keeps a late worker queued through initial pause and reduced-motion preference switches", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValue(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input, true, vi.fn(), true);
    const before = graph.getPointPositions();
    const target = Float32Array.from([500, 600, 700, 800, 900, 1000]);
    resolve({ kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId, input.identity.actorId,
      input.identity.surface, input.identity.selectionKey, input.identity.visibleFingerprint]),
      initialPositions: target, communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 });
    await new Promise(done => setTimeout(done, 0));
    adapter.setReducedMotion(false);
    expect(graph.getPointPositions()).toEqual(before); expect(sdk.running()).toBe(false);
    adapter.resumeLayout();
    expect(graph.getPointPositions()).toEqual(target); expect(sdk.running()).toBe(true);
  });

  it("drains explicitly resumed queued static coordinates once without enabling reduced-motion physics", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValue(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input, true, vi.fn(), true);
    const before = graph.getPointPositions();
    const target = Float32Array.from([500, 600, 700, 800, 900, 1000]);
    resolve({ kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId, input.identity.actorId,
      input.identity.surface, input.identity.selectionKey, input.identity.visibleFingerprint]),
      initialPositions: target, communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 });
    await new Promise(done => setTimeout(done, 0));
    expect(graph.getPointPositions()).toEqual(before); vi.clearAllMocks();
    adapter.resumeLayout();
    expect(graph.getPointPositions()).toEqual(target); expect(graph.setPointPositions).toHaveBeenCalledTimes(1);
    expect(sdk.running()).toBe(false); expect(sdk.simulationEnabled()).toBe(false); expect(graph.start).not.toHaveBeenCalled();
    vi.clearAllMocks(); adapter.resumeLayout();
    expect(graph.setPointPositions).not.toHaveBeenCalled(); expect(graph.start).not.toHaveBeenCalled();
  });

  it("initializes pre-ready paused/reduced coordinates without installing persistent community forces", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    const target = Float32Array.from([500, 600, 700, 800, 900, 1000]);
    community.compute.mockResolvedValue({ kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId, input.identity.actorId,
      input.identity.surface, input.identity.selectionKey, input.identity.visibleFingerprint]),
      initialPositions: target, communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 });
    const adapter = await mount(input, true, vi.fn(), true);
    expect(graph.getPointPositions()).toEqual(target); expect(sdk.running()).toBe(false);
    expect(graph.setPointClusters).not.toHaveBeenCalled();
    expect(graph.setClusterPositions).not.toHaveBeenCalled();
    expect(graph.setLinkStrength).toHaveBeenCalledTimes(1);
    expect([...graph.setLinkStrength.mock.lastCall![0]]).toEqual([1, 1]);
    expect(forceCommits()).toEqual([expectedPhysicsCommit(input)]);
    vi.clearAllMocks(); adapter.setReducedMotion(false);
    expect(graph.getPointPositions()).toEqual(target); expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(sdk.running()).toBe(false);
    adapter.resumeLayout();
    expect(graph.getPointPositions()).toEqual(target); expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(graph.setPointClusters).not.toHaveBeenCalled();
    expect(graph.setClusterPositions).not.toHaveBeenCalled();
    expect(graph.setLinkStrength).not.toHaveBeenCalled();
    expect(forceCommits()).toEqual([]);
    expect(sdk.running()).toBe(true);
  });

  it("discards a queued paused layout when the visible actor identity changes", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValueOnce(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input, false); adapter.stopLayout();
    const target = Float32Array.from([500, 600, 700, 800, 900, 1000]);
    resolve({ kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId, input.identity.actorId,
      input.identity.surface, input.identity.selectionKey, input.identity.visibleFingerprint]),
      initialPositions: target, communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 });
    await new Promise(done => setTimeout(done, 0));
    const changed = { ...input, identity: { ...input.identity, actorId: "different-viewer" } };
    adapter.replaceColumns(changed);
    const before = graph.getPointPositions(); vi.clearAllMocks(); adapter.resumeLayout();
    expect(graph.getPointPositions()).toEqual(before); expect(graph.getPointPositions()).not.toEqual(target);
    expect(graph.setPointPositions).not.toHaveBeenCalled(); expect(sdk.running()).toBe(false);
    await new Promise(done => setTimeout(done, 0));
    expect(graph.getPointPositions()).toEqual(before); expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(sdk.running()).toBe(true);
  });

  it("reveals only after late community coordinates and restored pins have reached the native scene", async () => {
    const input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValueOnce(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input);
    adapter.select("a"); adapter.revealAfterLayout("a", new AbortController().signal, () => true);
    graph.setZoomTransformByPointPositions.mockClear();
    await new Promise(done => setTimeout(done, 0));
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    adapter.pin("a", true, { x: 888, y: 999 });
    resolve(communityResult(input));
    await new Promise(done => setTimeout(done, 10));
    expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(Float32Array.from([888, 999]), 0, 2, 0, false);
    const reveals = graph.setZoomTransformByPointPositions.mock.calls.filter(([positions]) => positions.length === 2);
    expect(reveals).toHaveLength(1);
    adapter.resumeLayout(); adapter.replaceColumns({ ...input, nodeLabels: ["Renamed", "Beta", "Gamma"] });
    await new Promise(done => setTimeout(done, 10));
    expect(graph.setZoomTransformByPointPositions.mock.calls.filter(([positions]) => positions.length === 2)).toHaveLength(1);
  });

  it.each(["abort", "owner", "pointerdown", "wheel", "center", "fit", "zoom", "geometry", "destroy"] as const)(
    "retires navigation before a queued reveal frame for newer %s intent", async (intent) => {
      const input = columns(); community.compute.mockResolvedValueOnce(communityResult(input));
      const adapter = await mount(input);
      const frames: FrameRequestCallback[] = [];
      vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.push(callback); return frames.length; });
      const controller = new AbortController(); let current = true;
      adapter.revealAfterLayout("a", controller.signal, () => current);
      expect(frames).toHaveLength(1);
      if (intent === "abort") controller.abort();
      else if (intent === "owner") current = false;
      else if (intent === "center") adapter.centerOn("b");
      else if (intent === "fit") adapter.fit();
      else if (intent === "zoom") adapter.zoomBy(1.2);
      else if (intent === "geometry") adapter.replaceColumns({ ...input, nodeSizes: Float32Array.from([9, 3, 3]) });
      else if (intent === "destroy") adapter.destroy();
      else document.body.lastElementChild!.dispatchEvent(new Event(intent));
      graph.setZoomTransformByPointPositions.mockClear();
      frames.shift()!(performance.now());
      expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    },
  );

  it.each(["error", "timeout"] as const)("reveals in terminal %s fallback without waiting forever or moving geometry", async (kind) => {
    const input = columns();
    community.compute.mockResolvedValueOnce({ kind, identityKey: communityResult(input).identityKey });
    const adapter = await mount(input), before = graph.getPointPositions();
    graph.setZoomTransformByPointPositions.mockClear();
    adapter.revealAfterLayout("b", new AbortController().signal, () => true);
    await new Promise(done => setTimeout(done, 10));
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledOnce();
    expect(graph.getPointPositions()).toEqual(before);
  });

  it("defers navigation during passive pause, then reveals once without replaying session coordinates", async () => {
    const input = columns(); community.compute.mockResolvedValueOnce(communityResult(input));
    const adapter = await mount(input), before = graph.getPointPositions();
    adapter.stopLayout(); graph.setZoomTransformByPointPositions.mockClear();
    adapter.revealAfterLayout("a", new AbortController().signal, () => true);
    await new Promise(done => setTimeout(done, 10));
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    adapter.resumeLayout();
    await new Promise(done => setTimeout(done, 10));
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledOnce();
    expect(graph.getPointPositions()).toEqual(before);
    adapter.stopLayout(); adapter.resumeLayout();
    await new Promise(done => setTimeout(done, 10));
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledOnce();
  });

  it("defers an already-scheduled community camera fit if Pause happens before its RAF", async () => {
    const input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValue(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input, false);
    const callbacks: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callbacks.push(callback); return callbacks.length; });
    resolve({ kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId, input.identity.actorId,
      input.identity.surface, input.identity.selectionKey, input.identity.visibleFingerprint]),
      initialPositions: Float32Array.from([500, 600, 700, 800, 900, 1000]),
      communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 });
    await new Promise(done => setTimeout(done, 0));
    expect(callbacks).toHaveLength(1);
    adapter.stopLayout(); const before = graph.getPointPositions(); vi.clearAllMocks();
    callbacks.shift()!(performance.now());
    expect(graph.getPointPositions()).toEqual(before);
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    adapter.resumeLayout(); adapter.resumeLayout();
    expect(callbacks).toHaveLength(1);
    callbacks.shift()!(performance.now());
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledTimes(1);
  });

  it.each(["pointerdown", "wheel", "center", "zoom", "fit"] as const)(
    "cancels an already-scheduled community camera fit after explicit %s intent",
    async (intent) => {
      const input = columns();
      let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
      community.compute.mockReturnValue(new Promise(complete => { resolve = complete; }));
      const adapter = await mount(input, false);
      const callbacks: FrameRequestCallback[] = [];
      vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
        callbacks.push(callback); return callbacks.length;
      });
      resolve({ kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId,
        input.identity.actorId, input.identity.surface, input.identity.selectionKey,
        input.identity.visibleFingerprint]),
      initialPositions: Float32Array.from([500, 600, 700, 800, 900, 1000]),
      communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 });
      await new Promise(done => setTimeout(done, 0));
      expect(callbacks).toHaveLength(1);
      // User intent arrives after the worker accepted its current scene but
      // before the queued camera RAF. Coordinates may initialize; the stale
      // automatic fit must not supersede the user's newer camera action.
      if (intent === "center") adapter.centerOn("a");
      else if (intent === "zoom") adapter.zoomBy(1.2);
      else if (intent === "fit") adapter.fit();
      else document.body.lastElementChild!.dispatchEvent(new Event(intent));
      const positions = graph.getPointPositions();
      vi.clearAllMocks();
      callbacks.shift()!(performance.now());
      expect(graph.getPointPositions()).toEqual(positions);
      expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
      expect(graph.fitView).not.toHaveBeenCalled();
      adapter.stopLayout(); adapter.resumeLayout();
      expect(callbacks).toHaveLength(0);
      expect(graph.setPointPositions).not.toHaveBeenCalled();
      expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    },
  );

  it("fits a worker arriving during initial fallback fit before readiness, never on a later Resume", async () => {
    const input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValue(new Promise(complete => { resolve = complete; }));
    const target = Float32Array.from([500, 600, 700, 800, 900, 1000]);
    graph.fitView.mockImplementationOnce(() => resolve({ kind: "complete", identityKey: JSON.stringify([
      input.identity.workspaceId, input.identity.actorId, input.identity.surface, input.identity.selectionKey,
      input.identity.visibleFingerprint]), initialPositions: target, communityIds: Uint32Array.from([0, 0, 0]),
      communityCount: 1, computeMs: 1 }));
    const adapter = await mount(input, false);
    expect(graph.getPointPositions()).toEqual(target);
    expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(target, 0, undefined, .08, false);
    vi.clearAllMocks(); adapter.stopLayout(); adapter.resumeLayout();
    await new Promise(done => setTimeout(done, 0));
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    expect(graph.setPointPositions).not.toHaveBeenCalled();
  });

  it.each(["saved", "explicit"] as const)("preserves a %s camera over a worker arriving during initial fitting", async (kind) => {
    const input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValue(new Promise(complete => { resolve = complete; }));
    const container = document.createElement("div"); document.body.appendChild(container);
    const viewport = { zoom: .3, pan: { x: 888, y: 999 } };
    graph.fitView.mockImplementationOnce(() => {
      if (kind === "explicit") { container.dispatchEvent(new Event("pointerdown")); adapter.setViewport(viewport); }
      resolve({ kind: "complete", identityKey: JSON.stringify([input.identity.workspaceId, input.identity.actorId,
        input.identity.surface, input.identity.selectionKey, input.identity.visibleFingerprint]),
        initialPositions: Float32Array.from([500, 600, 700, 800, 900, 1000]),
        communityIds: Uint32Array.from([0, 0, 0]), communityCount: 1, computeMs: 1 });
    });
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      initialViewport: kind === "saved" ? viewport : null, callbacks: { onHover: vi.fn(), onSelect: vi.fn() } });
    adapters.push(adapter); await adapter.whenSettled();
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledTimes(1);
    expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(Float32Array.from([888, 999]), 0, .3, 0, false);
  });

  it("keeps authored cross-directory links visible for default selection/search/hover and Hide relations", async () => {
    const input = columns(); input.edgeCrossFolder.fill(1);
    const adapter = await mount(input, false), config = createGraph.mock.calls[0][1];
    // SDK shaders emit straight RGB plus faded alpha. Native WebGL canvases
    // use premultiplied alpha, so disabling source-over blending can turn a
    // low-alpha grey link into white even when the scene background is opaque.
    expect(config.linkBlending).toBe(true); expect(config.backgroundColor).toBe("#ffffff");
    const colors = graph.setLinkColors.mock.lastCall![0].slice();
    expect(colors[3]).toBeGreaterThan(0); expect(colors[7]).toBeGreaterThan(0);
    adapter.select("a"); adapter.setSearchMatches(new Set(["a"]));
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    expect(graph.setLinkColors.mock.lastCall![0]).toEqual(colors);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      linkOpacity: 2.1, linkVisibilityMinTransparency: 1, linkGreyoutOpacity: .1,
    }));
    config.onPointMouseOut?.(undefined);
    adapter.select(null);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({ linkOpacity: 1 }));
    adapter.setEdgesVisible(false); expect(graph.setConfigPartial).toHaveBeenLastCalledWith({ renderLinks: false });
  });

  it("preserves source-over link blending when indexed geometry is replaced", async () => {
    const input = columns(), adapter = await mount(input, false);
    vi.clearAllMocks();
    adapter.replaceColumns({ ...input, edgeTargets: Uint32Array.from([2, 1]) });
    expect(graph.setConfigPartial).toHaveBeenCalledWith(expect.objectContaining({ linkBlending: true }));
  });

});

describe("Cosmos Map interaction contracts", () => {
  it("pins the real SDK XY-subject drag by its captured public hovered node, not a nonexistent subject index", async () => {
    const onPin = vi.fn();
    await mount(columns(), false, vi.fn(), false, onPin);
    const config = createGraph.mock.calls[0][1];
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    const drag = installedSdkDrag(config, 0);
    expect(drag.event.subject).toEqual({ x: 20, y: 30 });
    drag.start(); drag.move(24, 35); config.onPointMouseOut?.(undefined); drag.end();
    expect(onPin).toHaveBeenCalledWith("a", expect.objectContaining({ x: expect.any(Number), y: expect.any(Number) }));
    expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([0]);
  });

  it("keeps native drag ownership through presentation focus and semantic ID rebinding, but drops it on actor change", async () => {
    const input = columns(), onPin = vi.fn();
    const adapter = await mount(input, false, vi.fn(), false, onPin);
    const config = createGraph.mock.calls[0][1];
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    adapter.focus("b"); // presentation hover clears; SDK's native hovered point does not
    const drag = installedSdkDrag(config, 0); drag.start();
    adapter.replaceColumns({ ...input, nodeIds: ["projection:a", "b", "c"] });
    drag.move(24, 35); drag.end();
    expect(onPin).toHaveBeenLastCalledWith("projection:a", expect.any(Object));
    onPin.mockClear();
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    const stale = installedSdkDrag(config, 0); stale.start(); stale.move(24, 35);
    adapter.replaceColumns({ ...input, identity: { ...input.identity, actorId: "another-actor" } });
    stale.end(); expect(onPin).not.toHaveBeenCalled();
  });

  it("does not pin or reheat a stationary native touch tap", async () => {
    const sdk = installedSdkLifecycle(), onPin = vi.fn();
    await mount(columns(), false, vi.fn(), false, onPin);
    const config = createGraph.mock.calls[0][1];
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    graph.stop();
    const tap = installedSdkDrag(config, 0); tap.start("touchstart"); tap.end("touchend");
    expect(onPin).not.toHaveBeenCalled(); expect(sdk.running()).toBe(false);
  });

  it("does not pin a moved but cancelled native touch gesture", async () => {
    const onPin = vi.fn();
    await mount(columns(), false, vi.fn(), false, onPin);
    const config = createGraph.mock.calls[0][1];
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    const cancelled = installedSdkDrag(config, 0); cancelled.start("touchstart"); cancelled.move(24, 35); cancelled.end("touchcancel");
    expect(onPin).not.toHaveBeenCalled();
  });

  it("pins a real moved native gesture on the same SDK-hovered node after Reset without a fresh MouseOver", async () => {
    const onPin = vi.fn();
    const adapter = await mount(columns(), false, vi.fn(), false, onPin);
    const config = createGraph.mock.calls[0][1];
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    adapter.reset();
    const drag = installedSdkDrag(config, 0); drag.start("touchstart"); drag.move(21, 30); drag.end("touchend");
    expect(onPin).toHaveBeenCalledWith("a", expect.any(Object));
  });

  it("starts remounted native physics with preserved user pause before constructor or async ready", async () => {
    const sdk = installedSdkLifecycle();
    const adapter = await mount(columns(), false, vi.fn(), true);
    expect(createGraph.mock.calls[0][1].enableSimulation).toBe(false);
    expect(sdk.running()).toBe(false); expect(graph.start).not.toHaveBeenCalled();
    const config = createGraph.mock.calls[0][1];
    config.onSimulationTick?.(.001);
    config.onDragStart?.({} as Parameters<NonNullable<GraphConfig["onDragStart"]>>[0]);
    expect(sdk.running()).toBe(false);
    adapter.resumeLayout();
    expect(sdk.running()).toBe(true); expect(sdk.simulationEnabled()).toBe(true);
  });
  it("qualifies 2K native physics, keeps workspace seeding and sends authored weights to GPU springs", async () => {
    const input = columns();
    input.nodeIds = Array.from({ length: 2_000 }, (_, index) => `node-${index}`);
    input.nodeKeys = [...input.nodeIds]; input.nodeLabels = [...input.nodeIds];
    input.nodeFolderKeys = input.nodeIds.map(() => null);
    input.nodeKinds = new Uint8Array(2_000); input.nodeSizes = new Float32Array(2_000).fill(3);
    input.edgeWeights[1] = 64;
    await mount(input, false);
    expect(createGraph.mock.calls[0][1]).toMatchObject({ enableSimulation: false, randomSeed: input.identity.workspaceId, simulationCluster: 0 });
    expect(graph.setConfigPartial).toHaveBeenCalledWith({ enableSimulation: true });
    expect([...graph.setLinkStrength.mock.lastCall![0]]).toEqual([1, 7]);
    const collision = graph.setConfigPartial.mock.calls.find(([config]) => config.simulationCollision === 1)![0];
    const buffers = buildCosmosSceneBuffers(input, readGraphTokens(document.createElement("div")));
    expect(graph.setPointSizes.mock.lastCall![0]).toEqual(buffers.drawPointSizes);
    const expected = buildWorkspaceRelationCollisionBasis(graph.setPointPositions.mock.calls[0][0], buffers.pointSizes, 734, 608)!;
    expect(collision.simulationCollisionRadius).toBeCloseTo(expected.radius);
    expect(collision.simulationCollisionPadding).toBe(expected.padding);
  });

  it("keeps original 2K bridge strengths through community seeding and metadata-only updates", async () => {
    const input = columns();
    input.nodeIds = Array.from({ length: 2_000 }, (_, index) => `node-${index}`);
    input.nodeKeys = [...input.nodeIds]; input.nodeLabels = [...input.nodeIds];
    input.nodeFolderKeys = input.nodeIds.map(() => "same-folder");
    input.nodeKinds = new Uint8Array(2_000); input.nodeSizes = new Float32Array(2_000).fill(3);
    input.edgeWeights[1] = 64;
    const ids = new Uint32Array(2_000); ids[2] = 1;
    community.compute.mockResolvedValueOnce({ kind: "complete", identityKey: JSON.stringify([
      input.identity.workspaceId, input.identity.actorId, input.identity.surface,
      input.identity.selectionKey, input.identity.visibleFingerprint]),
      initialPositions: new Float32Array(4_000).fill(2048), communityIds: ids, communityCount: 2, computeMs: 1 });
    const adapter = await mount(input, false);
    expect(graph.setPointClusters).not.toHaveBeenCalled();
    expect(graph.setClusterPositions).not.toHaveBeenCalled();
    expect(graph.setConfigPartial.mock.calls.some(([config]) => config.simulationCluster > 0)).toBe(false);
    expect(graph.setLinkStrength).toHaveBeenCalledTimes(1);
    expect([...graph.setLinkStrength.mock.lastCall![0]]).toEqual([1, 7]);
    expect(forceCommits()).toEqual([expectedPhysicsCommit(input)]);
    expect(graph.setLinks.mock.lastCall![0]).toHaveLength(input.edgeIds.length * 2);
    const before = graph.setLinkStrength.mock.lastCall![0].slice();
    vi.clearAllMocks();
    adapter.replaceColumns({ ...input, nodeFolderKeys: input.nodeIds.map((_, index) => `folder-${index}`), edgeCrossFolder: new Uint8Array(2).fill(1) });
    adapter.select("node-0");
    expect(graph.setLinkColors.mock.lastCall![0][3]).toBeCloseTo(.95);
    expect(graph.setLinkColors.mock.lastCall![0][7]).toBeGreaterThan(0);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({ pointGreyoutOpacity: .3, linkGreyoutOpacity: .08 }));
    adapter.select(null);
    expect(graph.setLinkColors.mock.lastCall![0][3]).toBeCloseTo(.35);
    expect(graph.setLinkColors.mock.lastCall![0][7]).toBeCloseTo(.18);
    expect(before[1]).toBe(7);
    expect(forceCommits()).toEqual([]);
    for (const method of [graph.setLinkStrength, graph.setLinks, graph.setPointPositions, graph.start,
      graph.stop, graph.setPointClusters, graph.setClusterPositions, community.compute]) expect(method).not.toHaveBeenCalled();
  });

  it.each([false, true])("preserves search and selection during node and edge hover (forced colors: %s)", async (forcedColors) => {
    setTrackedRows([[0, [0, 0]], [1, [100, 100]], [2, [200, 200]]]);
    const input = sizedColumns(2_000, 3);
    // A's two neighbors also link to one another. Searching B/C highlights
    // that third relation; hovering A must retain the same search presentation.
    input.edgeIds = ["ab", "ac", "bc"];
    input.edgeSources = Uint32Array.from([0, 0, 1]);
    input.edgeTargets = Uint32Array.from([1, 2, 2]);
    const container = document.createElement("div"); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, input, {
      forcedColors, reducedMotion: true, relationLabel: (kind) => kind === 1 ? "Citation" : "Link",
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() },
    });
    Object.defineProperties(container, { clientWidth: { value: 734 }, clientHeight: { value: 608 } });
    adapters.push(adapter); await adapter.whenSettled();
    const config = createGraph.mock.calls[0][1];
    adapter.setSearchMatches(new Set(["node:1", "node:2"]));
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: [1, 2], highlightedLinkIndices: [2], linkGreyoutOpacity: .08,
    }));
    const positions = graph.getPointPositions(), viewport = adapter.getViewport();
    expect(positions).toHaveLength(4_000);
    expect(graph.setLinks.mock.lastCall![0]).toEqual(Float32Array.from([0, 1, 0, 2, 1, 2]));
    const strength = graph.setLinkStrength.mock.lastCall![0], savedStrength = strength.slice();
    vi.clearAllMocks();

    const labels = container.querySelector("[data-workspace-map-labels]")!.innerHTML;
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    expect(graph.setConfigPartial).not.toHaveBeenCalled();
    expect(container.querySelector("[data-workspace-map-labels]")!.innerHTML).toBe(labels);
    config.onPointMouseOut?.(undefined);
    config.onLinkMouseOver?.(2);
    const chip = container.querySelector<HTMLElement>(".graph-hover-chip")!;
    expect(chip.hidden).toBe(false);
    expect(chip.textContent).toBe("Link");
    expect(chip.dataset.hoverEdgeId).toBe("bc");
    config.onLinkMouseOut?.(undefined);
    expect(graph.setConfigPartial).not.toHaveBeenCalled();
    expect(graph.setLinkColors).not.toHaveBeenCalled();
    expect(graph.getPointPositions()).toEqual(positions);
    expect(adapter.getViewport()).toEqual(viewport);

    config.onPointMouseOut?.(undefined);
    expect(graph.setConfigPartial).not.toHaveBeenCalled();
    expect(graph.getPointPositions()).toEqual(positions);
    expect(adapter.getViewport()).toEqual(viewport);
    expect(input.edgeWeights).toEqual(Float64Array.from([1, 1, 1]));
    expect(strength).toEqual(savedStrength);
    expect(forceCommits()).toEqual([]);
    for (const method of [graph.setLinks, graph.setPointPositions, graph.setLinkStrength,
      graph.setPointColors, graph.setPointShapes, graph.setPointSizes, graph.setPinnedPoints,
      graph.setPointClusters, graph.setClusterPositions, graph.fitView, graph.setZoomLevel,
      graph.setZoomTransformByPointPositions, graph.start, graph.stop, graph.pause,
      graph.unpause, community.compute]) expect(method).not.toHaveBeenCalled();
    adapter.select("node:0");
    const selection = graph.setConfigPartial.mock.lastCall;
    graph.setConfigPartial.mockClear();
    config.onPointMouseOver?.(2, [0, 0], undefined, false, false);
    expect(graph.setConfigPartial).not.toHaveBeenCalled();
    expect(selection?.[0]).toMatchObject({ highlightedPointIndices: [1, 2, 0], highlightedLinkIndices: [2, 0, 1] });
  });

  it.each(["light", "dark"])("draws a contrasting %s selection ring that follows the glyph, not the native circle", async (mode) => {
    document.documentElement.setAttribute("data-theme", mode);
    try {
      const input = columns();
      input.nodeKinds[1] = 1; // "b" is a Library document: a square glyph.
      const adapter = await mount(input);
      // No native focused point: its ring would be an opaque circle under the overlay.
      expect(createGraph.mock.calls[0][1]).toMatchObject({ renderHoveredPointRing: false });
      const ring = [...document.querySelectorAll<SVGSVGElement>("[data-workspace-map-selection-ring]")].at(-1)!;
      expect(ring.style.display).toBe("none");
      const config = createGraph.mock.calls[0][1];
      // A native pick publishes the point's position without waiting for a GPU readback.
      config.onPointMouseOver?.(1, [0, 0], undefined, false, false);
      adapter.select("b");
      expect(graph.setConfigPartial.mock.calls.some(([next]) => next.focusedPointIndex !== undefined)).toBe(false);
      const [gap, band] = ring.querySelectorAll("path");
      expect(band).toHaveAttribute("stroke", mode === "dark" ? "#f8fafc" : "#0f172a");
      expect(gap).toHaveAttribute("stroke", mode === "dark" ? "#0f172a" : "#ffffff");
      expect(ring.style.display).toBe("block");
      expect(band.getAttribute("d")!.split("L")).toHaveLength(4);
      // Zooming in grows the native glyph (pointSizeScale); the ring grows with it.
      const width = () => {
        const xs = band.getAttribute("d")!.match(/-?\d+(?:\.\d+)?/g)!.map(Number).filter((_, index) => index % 2 === 0);
        return Math.max(...xs) - Math.min(...xs);
      };
      const fitWidth = width();
      config.onZoom?.({ transform: { k: graph.getZoomLevel() * 16 } } as never, true);
      expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({ pointSizeScale: 4 }));
      // The glyph is 4x larger; the ring keeps its fixed 2px gap + 2px band outside it.
      const outside = 2 * (2 + 2 / 2);
      expect(width() - outside).toBeCloseTo((fitWidth - outside) * 4, 5);
    } finally { document.documentElement.removeAttribute("data-theme"); }
  });

  it("commits 2K highlight colors without resuming paused native physics or replaying coordinates", async () => {
    const sdk = installedSdkLifecycle(), input = columns();
    input.nodeIds = Array.from({ length: 2_000 }, (_, index) => `node-${index}`);
    input.nodeKeys = [...input.nodeIds]; input.nodeLabels = [...input.nodeIds];
    input.nodeFolderKeys = input.nodeIds.map(() => null);
    input.nodeKinds = new Uint8Array(2_000); input.nodeSizes = new Float32Array(2_000).fill(3);
    const adapter = await mount(input, false);
    adapter.stopLayout(); const coordinates = graph.getPointPositions();
    const palette = sdk.colors();
    graph.setPointPositions.mockClear(); graph.start.mockClear(); community.compute.mockClear();
    adapter.select("node-0");
    expect(sdk.colors()[3]).toBeCloseTo(.95); expect(sdk.colors()[7]).toBeCloseTo(.35);
    expect(sdk.running()).toBe(false); expect(sdk.activePositions()).toBe(false);
    expect(graph.getPointPositions()).toEqual(coordinates);
    adapter.select(null); expect(sdk.colors()).toEqual(palette);
    expect(sdk.running()).toBe(false);
    for (const method of [graph.setPointPositions, graph.start, community.compute]) expect(method).not.toHaveBeenCalled();
  });

  it("reheats cooled native physics without timers, but never overrides pause, reduced motion or destroy", async () => {
    const sdk = installedSdkLifecycle();
    const adapter = await mount(columns(), false);
    const config = createGraph.mock.calls[0][1];
    graph.stop();
    expect(sdk.running()).toBe(false);
    config.onSimulationTick?.(.001);
    expect(sdk.running()).toBe(true); expect(sdk.alpha()).toBe(.04);
    graph.stop();
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    const drag = installedSdkDrag(config, 0); drag.start();
    expect(sdk.running()).toBe(false); drag.move(24, 35);
    expect(sdk.running()).toBe(true); expect(sdk.alpha()).toBe(.15);
    adapter.stopLayout();
    config.onSimulationTick?.(.001);
    const pausedDrag = installedSdkDrag(config, 0); pausedDrag.start(); pausedDrag.move(24, 35); pausedDrag.end();
    expect(sdk.running()).toBe(false);
    adapter.resumeLayout(); expect(sdk.running()).toBe(true);
    adapter.setReducedMotion(true);
    config.onSimulationTick?.(.001);
    const reducedDrag = installedSdkDrag(config, 0); reducedDrag.start(); reducedDrag.move(24, 35);
    expect(sdk.running()).toBe(false); expect(sdk.simulationEnabled()).toBe(false);
    adapter.setReducedMotion(false); expect(sdk.running()).toBe(true);
    adapter.destroy();
    const startsBeforeStaleCallback = graph.start.mock.calls.length;
    config.onSimulationTick?.(.001);
    const destroyedDrag = installedSdkDrag(config, 0); destroyedDrag.start(); destroyedDrag.move(24, 35);
    expect(graph.start.mock.calls).toHaveLength(startsBeforeStaleCallback);
  });

  describe("PC native natural-cooling lifecycle", () => {
    describe("released pin wake permission", () => {
      const releasePin = (adapter: WorkspaceCosmosAdapter, action: "unpin" | "reset") => {
        if (action === "reset") adapter.reset(); else adapter.pin("node:0", false);
      };

      it.each([
        { count: 2_001, action: "unpin" as const },
        { count: 2_001, action: "reset" as const },
        { count: 10_000, action: "unpin" as const },
        { count: 10_000, action: "reset" as const },
      ])("wakes a cooled $count-node scene once when $action releases a real pin", async ({ count, action }) => {
        const sdk = installedSdkLifecycle();
        const adapter = await mount(sizedColumns(count), false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
        adapter.pin("node:0", true, { x: 700, y: 800 });
        graph.start(.0009); sdk.end();
        expect(graph.isSimulationRunning).toBe(false);
        graph.start.mockClear(); graph.setPointPositions.mockClear();
        releasePin(adapter, action);
        expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([]);
        expect(graph.start).toHaveBeenCalledExactlyOnceWith(.15);
        expect(sdk.running()).toBe(true); expect(sdk.alpha()).toBe(.15);
        expect(graph.setPointPositions).not.toHaveBeenCalled();
        // Repeating the same UI echo is not a new release, even after cooling.
        graph.start(.0009); sdk.end(); graph.start.mockClear();
        releasePin(adapter, action);
        expect(graph.start).not.toHaveBeenCalled(); expect(sdk.running()).toBe(false);
      });

      it.each(["unpin", "reset"] as const)("does not wake a cooled scene for a no-pin %s", async action => {
        const sdk = installedSdkLifecycle();
        const adapter = await mount(sizedColumns(2_001), false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
        graph.start(.0009); sdk.end(); graph.start.mockClear();
        releasePin(adapter, action);
        expect(graph.start).not.toHaveBeenCalled(); expect(sdk.running()).toBe(false);
      });

      it.each(["unpin", "reset"] as const)("does not change a live scene's alpha when %s releases a pin", async action => {
        const sdk = installedSdkLifecycle();
        const adapter = await mount(sizedColumns(2_001), false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
        adapter.pin("node:0", true); graph.start(.019); graph.start.mockClear();
        releasePin(adapter, action);
        expect(graph.start).not.toHaveBeenCalled(); expect(sdk.running()).toBe(true); expect(sdk.alpha()).toBe(.019);
      });

      it("does not wake a cooled scene when pins or their projection metadata are restored", async () => {
        const sdk = installedSdkLifecycle(), input = sizedColumns(2_001);
        const adapter = await mount(input, false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
        graph.start(.0009); sdk.end(); graph.start.mockClear();
        adapter.pin("node:0", true, { x: 700, y: 800 });
        adapter.replaceColumns({ ...input, nodeIds: input.nodeIds.map(id => `projection:${id}`) });
        adapter.pin("projection:node:0", true, { x: 700, y: 800 });
        // A retired alias cannot release the pin after its new ID took ownership.
        adapter.pin("node:0", false);
        expect(graph.start).not.toHaveBeenCalled(); expect(sdk.running()).toBe(false);
        expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([0]);
      });

      it("keeps a cooled 10K scene asleep when host reconciliation retires the old pin ID before restoring its new ID", async () => {
        const sdk = installedSdkLifecycle(), input = sizedColumns(10_000);
        const adapter = await mount(input, false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
        adapter.pin("node:0", true, { x: 700, y: 800 });
        graph.start(.0009); sdk.end(); graph.start.mockClear(); graph.setPinnedPoints.mockClear();
        adapter.replaceColumns({ ...input, nodeIds: input.nodeIds.map(id => `projection:${id}`) });
        // WorkspaceMapCanvas removes old applied IDs before replaying new IDs.
        adapter.pin("node:0", false);
        expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([]);
        expect(graph.start).not.toHaveBeenCalled(); expect(sdk.running()).toBe(false);
        adapter.pin("projection:node:0", true, { x: 700, y: 800 });
        expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([0]);
        expect(graph.start).not.toHaveBeenCalled(); expect(sdk.running()).toBe(false);
        expect([...graph.getPointPositions().slice(0, 2)]).toEqual([700, 800]);
        // A subsequent explicit release through the current ID is a real edit.
        adapter.pin("projection:node:0", false);
        expect(graph.start).toHaveBeenCalledExactlyOnceWith(.15);
        expect(sdk.running()).toBe(true);
      });

      it.each((["unpin", "reset"] as const).flatMap(action =>
        (["paused", "reduced", "context-lost", "actor", "destroyed"] as const).map(fence => ({ action, fence }))))(
        "does not wake a $fence engine when $action releases pins", async ({ action, fence }) => {
          const sdk = installedSdkLifecycle(), input = sizedColumns(2_001);
          const adapter = await mount(input, false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
          adapter.pin("node:0", true); graph.start(.0009); sdk.end();
          if (fence === "paused") adapter.stopLayout();
          else if (fence === "reduced") adapter.setReducedMotion(true);
          else if (fence === "context-lost") document.querySelector("canvas")!
            .dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
          else if (fence === "actor") adapter.replaceColumns({ ...input,
            identity: { ...input.identity, actorId: "another-actor" } });
          else adapter.destroy();
          graph.start.mockClear();
          releasePin(adapter, action);
          expect(graph.start).not.toHaveBeenCalled(); expect(sdk.running()).toBe(false);
        },
      );

      it.each(["unpin", "reset"] as const)("preserves the 2K CPU handoff when %s releases pins", async action => {
        const sdk = installedSdkLifecycle();
        const adapter = await mount(sizedColumns(2_000), false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_DEFAULT_BUDGET);
        adapter.pin("node:0", true); const worker = cpu.instances[0];
        worker.setPositions.mockClear(); graph.start.mockClear();
        releasePin(adapter, action);
        expect(worker.setPositions).toHaveBeenCalledExactlyOnceWith(expect.any(Float32Array), []);
        expect(worker.isRunning()).toBe(true); expect(sdk.running()).toBe(false);
        expect(graph.start).not.toHaveBeenCalled();
      });
    });

    it.each([2_001, 10_000])("does not reset the cooling alpha of an actual %i-node default-native scene", async count => {
      const sdk = installedSdkLifecycle();
      await mount(sizedColumns(count), false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
      const config = createGraph.mock.calls[0][1];
      graph.start(.019);
      graph.start.mockClear();
      config.onSimulationTick?.(.019);
      expect(graph.start).not.toHaveBeenCalled();
      expect(sdk.running()).toBe(true);
      expect(sdk.alpha()).toBe(.019);
    });

    it.each([2_001, 10_000])("does not rearm an ended actual %i-node default-native scene from a late cooling tick", async count => {
      const sdk = installedSdkLifecycle();
      await mount(sizedColumns(count), false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
      const config = createGraph.mock.calls[0][1], positions = graph.getPointPositions();
      graph.stop();
      graph.start.mockClear(); graph.setConfigPartial.mockClear();
      graph.fitView.mockClear(); graph.setZoomTransformByPointPositions.mockClear();
      for (const alpha of [.019, .001, 0]) config.onSimulationTick?.(alpha);
      expect(graph.start).not.toHaveBeenCalled();
      expect(graph.setConfigPartial).not.toHaveBeenCalled();
      expect(sdk.running()).toBe(false);
      expect(sdk.alpha()).toBe(0);
      expect(sdk.simulationEnabled()).toBe(true);
      expect(graph.getPointPositions()).toEqual(positions);
      expect(graph.fitView).not.toHaveBeenCalled();
      expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    });

    it("retains the legacy alpha floor for a 2K native scene admitted with the PC budget", async () => {
      const sdk = installedSdkLifecycle();
      await mount(sizedColumns(2_000), false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
      const config = createGraph.mock.calls[0][1];
      graph.stop(); graph.start.mockClear();
      config.onSimulationTick?.(.019);
      expect(graph.start).toHaveBeenCalledExactlyOnceWith(.04);
      expect(sdk.running()).toBe(true);
      expect(sdk.alpha()).toBe(.04);
    });

    it("leaves the accepted default 2K CPU Worker independent of native alpha", async () => {
      const sdk = installedSdkLifecycle();
      await mount(sizedColumns(2_000), false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_DEFAULT_BUDGET);
      const config = createGraph.mock.calls[0][1], worker = cpu.instances[0];
      expect(worker.isRunning()).toBe(true);
      worker.start.mockClear(); graph.start.mockClear();
      config.onSimulationTick?.(.001);
      expect(graph.start).not.toHaveBeenCalled();
      expect(sdk.running()).toBe(false);
      expect(sdk.simulationEnabled()).toBe(false);
      expect(worker.isRunning()).toBe(true);
      expect(worker.start).not.toHaveBeenCalled();
    });

    it("keeps explicit Resume and moved drag-to-pin wake paths after native 10K cooling, but not a stationary tap", async () => {
      const sdk = installedSdkLifecycle(), onPin = vi.fn(), input = sizedColumns(10_000);
      const adapter = await mount(input, false, vi.fn(), false, onPin, WORKSPACE_MAP_PC_10K_BUDGET);
      const config = createGraph.mock.calls[0][1];
      graph.stop(); graph.start.mockClear();
      adapter.resumeLayout();
      expect(graph.start).toHaveBeenCalledExactlyOnceWith(.15);
      expect(sdk.running()).toBe(true);
      graph.stop(); graph.start.mockClear();
      config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
      const tap = installedSdkDrag(config, 0); tap.start(); tap.end();
      expect(graph.start).not.toHaveBeenCalled();
      expect(onPin).not.toHaveBeenCalled();
      expect(sdk.running()).toBe(false);
      const drag = installedSdkDrag(config, 0); drag.start(); drag.move(24, 35);
      expect(graph.start).toHaveBeenCalledExactlyOnceWith(.15);
      graph.stop(); graph.start.mockClear();
      drag.end();
      expect(graph.start).toHaveBeenCalledExactlyOnceWith(.15);
      expect(sdk.running()).toBe(true);
      expect(onPin).toHaveBeenCalledExactlyOnceWith(input.nodeIds[0], expect.any(Object));
      expect(graph.setPinnedPoints).toHaveBeenLastCalledWith([0]);
    });

    it("does not let 10K cooling or drag wake bypass pause, reduced motion, actor replacement or destruction", async () => {
      const sdk = installedSdkLifecycle(), onPin = vi.fn(), input = sizedColumns(10_000);
      const adapter = await mount(input, false, vi.fn(), false, onPin, WORKSPACE_MAP_PC_10K_BUDGET);
      const config = createGraph.mock.calls[0][1];
      for (const fence of [() => adapter.stopLayout(), () => adapter.setReducedMotion(true)]) {
        adapter.resumeLayout(); fence(); graph.start.mockClear();
        config.onSimulationTick?.(.001);
        config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
        const drag = installedSdkDrag(config, 0); drag.start(); drag.move(24, 35); drag.end();
        expect(graph.start).not.toHaveBeenCalled();
        expect(sdk.running()).toBe(false);
      }
      adapter.setReducedMotion(false); adapter.resumeLayout();
      config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
      const stale = installedSdkDrag(config, 0); stale.start();
      adapter.replaceColumns({ ...input, identity: { ...input.identity, actorId: "another-actor" } });
      onPin.mockClear(); graph.start.mockClear();
      stale.move(24, 35); stale.end(); config.onSimulationTick?.(.001);
      expect(onPin).not.toHaveBeenCalled();
      expect(graph.start).not.toHaveBeenCalled();
      adapter.destroy(); graph.start.mockClear();
      config.onSimulationTick?.(.001);
      const destroyed = installedSdkDrag(config, 0); destroyed.start(); destroyed.move(24, 35); destroyed.end();
      expect(graph.start).not.toHaveBeenCalled();
      expect(sdk.running()).toBe(false);
    });
  });

  it("does not upload geometry or change camera for directory-only metadata updates", async () => {
    const input = columns();
    const adapter = await mount(input, false);
    const before = graph.getPointPositions();
    vi.clearAllMocks();
    adapter.replaceColumns({ ...input, nodeFolderKeys: ["one", "two", "one"], edgeCrossFolder: new Uint8Array([1, 1]) });
    expect(graph.getPointPositions()).toEqual(before);
    expect(graph.setPointPositions).not.toHaveBeenCalled(); expect(graph.render).not.toHaveBeenCalled();
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled(); expect(community.compute).not.toHaveBeenCalled();
  });

  it("zooms out below 0.2 relative to a small overview, never in the opposite direction", async () => {
    graph.getZoomLevel.mockReturnValue(0.08);
    const adapter = await mount();
    adapter.zoomBy(1 / 1.2);
    expect(graph.setZoomLevel).toHaveBeenLastCalledWith(0.08 / 1.2, 0, false);
    graph.getZoomLevel.mockReturnValue(0.005);
    adapter.zoomBy(1 / 1.2);
    expect(graph.setZoomLevel.mock.lastCall![0]).toBeLessThanOrEqual(0.005);
  });

  it("keeps dots visible below fit zoom and grows them up to four without uploading geometry", async () => {
    graph.getZoomLevel.mockReturnValue(0.08);
    await mount();
    const config = createGraph.mock.calls[0][1];
    // Every line is the legend's one width; no scene-size scale.
    expect(config).toMatchObject({ linkDefaultWidth: 1.5, linkVisibilityMinTransparency: .65,
      hoveredLinkWidthIncrease: .6, focusedLinkWidthIncrease: .6 });
    graph.setConfigPartial.mockClear();
    config.onZoom?.({ transform: { k: 0.02 } } as Parameters<NonNullable<GraphConfig["onZoom"]>>[0], false);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({ pointSizeScale: 1 }));
    config.onZoom?.({ transform: { k: 0.32 } } as Parameters<NonNullable<GraphConfig["onZoom"]>>[0], false);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({ pointSizeScale: 2 }));
    config.onZoom?.({ transform: { k: 8 } } as Parameters<NonNullable<GraphConfig["onZoom"]>>[0], false);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({ pointSizeScale: 4 }));
    expect(graph.setPointSizes).toHaveBeenCalledTimes(1);
  });

  it("restarts a stopped layout while preserving reduced-motion and default-budget admission guards", async () => {
    const adapter = await mount(columns(), false);
    graph.start.mockClear();
    adapter.stopLayout();
    adapter.resumeLayout();
    expect(graph.start).toHaveBeenCalledWith(expect.any(Number));
    adapter.setReducedMotion(true);
    graph.start.mockClear();
    adapter.resumeLayout();
    expect(graph.start).not.toHaveBeenCalled();
    graph.start.mockClear();
    await expect(mount(sizedColumns(2_001), false, vi.fn(), false, vi.fn(), WORKSPACE_MAP_DEFAULT_BUDGET))
      .rejects.toThrow("exceeds its requested budget");
    expect(graph.start).not.toHaveBeenCalled();
  });

  it("selects the nearest visible node inside a 12px screen radius, otherwise clears on background", async () => {
    const onSelect = vi.fn();
    const adapter = await mount(columns(), true, onSelect);
    const config = createGraph.mock.calls[0][1];
    graph.spaceToScreenPosition.mockReturnValue([20, 20]);
    graph.spaceToScreenPosition.mockReturnValueOnce([20, 20]).mockReturnValueOnce([24, 20]);
    config.onBackgroundClick?.(new MouseEvent("click", { clientX: 25, clientY: 20 }));
    expect(onSelect).toHaveBeenLastCalledWith("b");
    config.onBackgroundClick?.(new MouseEvent("click", { clientX: 100, clientY: 100 }));
    expect(onSelect).toHaveBeenLastCalledWith(null);
    graph.spaceToScreenPosition.mockReturnValue([20, 20]);
    config.onBackgroundClick?.(new MouseEvent("click", { clientX: 32, clientY: 20 }));
    expect(onSelect).toHaveBeenLastCalledWith("a");
    config.onBackgroundClick?.(new MouseEvent("click", { clientX: 33, clientY: 20 }));
    expect(onSelect).toHaveBeenLastCalledWith(null);
    config.onLinkClick?.(0, new MouseEvent("click", { clientX: 30, clientY: 20 }));
    expect(onSelect).toHaveBeenLastCalledWith("a");
    config.onLinkClick?.(0, new MouseEvent("click", { clientX: 100, clientY: 100 }));
    expect(onSelect).toHaveBeenLastCalledWith("ab");
    adapter.destroy();
    onSelect.mockClear();
    config.onBackgroundClick?.(new MouseEvent("click", { clientX: 100, clientY: 100 }));
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("uses a real ellipsis for overview titles and preserves the full focused title", async () => {
    const input = columns();
    input.nodeLabels[0] = "A long document title that must not lose its ending";
    const adapter = await mount(input);
    const label = document.querySelector("[data-workspace-map-labels] span")!;
    expect(label.textContent).toBe(`${input.nodeLabels[0].slice(0, 24)}…`);
    adapter.focus("a");
    expect(label.textContent).toBe(input.nodeLabels[0]);
    adapter.focus(null);
    expect(label.textContent).toBe(`${input.nodeLabels[0].slice(0, 24)}…`);
    Object.defineProperties(createGraph.mock.calls[0][0], { clientWidth: { value: 734 }, clientHeight: { value: 608 } });
    createGraph.mock.calls[0][1].onPointMouseOver?.(0, [0, 0], new MouseEvent("mousemove"), false, false);
    expect(label.textContent).toBe(`${input.nodeLabels[0].slice(0, 24)}…`);
    expect(document.querySelector<HTMLElement>(".graph-hover-chip")!.hidden).toBe(false);
    expect(document.querySelector(".graph-hover-chip")!.textContent).toBe(input.nodeLabels[0]);
  });

  it.each([
    { name: "24 ASCII code points", title: "A".repeat(24), visible: "A".repeat(24) },
    { name: "25 ASCII code points", title: "A".repeat(25), visible: `${"A".repeat(24)}…` },
    { name: "astral character at the 24th code point", title: `${"A".repeat(23)}😀`, visible: `${"A".repeat(23)}😀` },
    { name: "astral character before an omitted 25th code point", title: `${"A".repeat(23)}😀Z`, visible: `${"A".repeat(23)}😀…` },
    { name: "13 astral code points", title: "😀".repeat(13), visible: "😀".repeat(13) },
  ])("truncates overview titles by Unicode code point: $name", async ({ title, visible }) => {
    const input = columns();
    input.nodeLabels[0] = title;
    await mount(input);
    const label = document.querySelector('[data-workspace-map-labels] span[data-node-id="a"]')!;
    expect(label.textContent).toBe(visible);
    expect(label.getAttribute("title")).toBe(title);
    expect(label.getAttribute("data-full-title")).toBe("false");
  });

  it("keeps complete Unicode titles for selection and focus and a separate hover chip", async () => {
    const input = columns();
    input.nodeLabels[0] = `${"A".repeat(23)}😀Z`;
    const adapter = await mount(input);
    const label = document.querySelector('[data-workspace-map-labels] span[data-node-id="a"]')!;
    const config = createGraph.mock.calls[0][1];
    const shortened = `${"A".repeat(23)}😀…`;
    expect(label.textContent).toBe(shortened);
    for (const activate of [
      () => adapter.select("a"),
      () => adapter.focus("a"),
    ]) {
      activate();
      expect(label.textContent).toBe(input.nodeLabels[0]);
      expect(label.getAttribute("title")).toBe(input.nodeLabels[0]);
      expect(label.getAttribute("data-full-title")).toBe("true");
      adapter.select(null);
      adapter.focus(null);
      config.onPointMouseOut?.(new MouseEvent("mouseout"));
      expect(label.textContent).toBe(shortened);
      expect(label.getAttribute("title")).toBe(input.nodeLabels[0]);
      expect(label.getAttribute("data-full-title")).toBe("false");
    }
    Object.defineProperties(createGraph.mock.calls[0][0], { clientWidth: { value: 734 }, clientHeight: { value: 608 } });
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    expect(label.textContent).toBe(shortened);
    expect(document.querySelector<HTMLElement>(".graph-hover-chip")!.hidden).toBe(false);
    expect(document.querySelector(".graph-hover-chip")!.textContent).toBe(input.nodeLabels[0]);
  });

  it("emphasizes an edge's endpoints independently and restores the selected node star on blur", async () => {
    const adapter = await mount();
    adapter.select("a");
    graph.setConfigPartial.mockClear();
    graph.setPointPositions.mockClear();
    graph.setLinks.mockClear();
    graph.setZoomLevel.mockClear();
    graph.setZoomTransformByPointPositions.mockClear();
    const ring = [...document.querySelectorAll<SVGSVGElement>("[data-workspace-map-selection-ring]")].at(-1)!;
    adapter.focus("bc");
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: [1, 2],
      highlightedLinkIndices: [1],
    }));
    expect(ring.dataset.selectedPoint).toBe("0");
    adapter.focus(null);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: [0, 1],
      highlightedLinkIndices: [0],
    }));
    expect(ring.dataset.selectedPoint).toBe("0");
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(graph.setLinks).not.toHaveBeenCalled();
    expect(graph.setZoomLevel).not.toHaveBeenCalled();
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
  });

  it("keeps focused node star precedence over selected edges and unions focused edges with search matches", async () => {
    const adapter = await mount();
    adapter.select("bc");
    adapter.focus("a");
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      focusedLinkIndex: 1,
      highlightedPointIndices: [0, 1],
      highlightedLinkIndices: [0],
    }));
    adapter.setSearchMatches(new Set(["a"]));
    adapter.focus("bc");
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: [0, 1, 2], highlightedLinkIndices: [1],
    }));
  });

  it.each(["c", "bc"])("preserves keyboard focus during stationary-pointer hover %s", async (id) => {
    const onHover = vi.fn();
    const container = document.createElement("div");
    document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, columns(), {
      forcedColors: false, reducedMotion: true,
      callbacks: { onHover, onSelect: vi.fn() },
    });
    adapters.push(adapter);
    await adapter.whenSettled();
    const config = createGraph.mock.calls[0][1];
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    expect(onHover).toHaveBeenLastCalledWith("a");
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: undefined, highlightedLinkIndices: undefined,
    }));
    graph.setPointPositions.mockClear();
    graph.setLinks.mockClear();
    graph.fitView.mockClear();
    graph.setZoomLevel.mockClear();
    graph.setZoomTransformByPointPositions.mockClear();
    adapter.focus(id);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: id === "c" ? [2, 1] : [1, 2],
      highlightedLinkIndices: [1],
    }));
    expect(onHover).toHaveBeenLastCalledWith("a");
    // Pointer motion changes only the chip; blur clears keyboard emphasis.
    config.onPointMouseOver?.(0, [0, 0], undefined, false, false);
    adapter.focus(null);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: undefined, highlightedLinkIndices: undefined,
    }));
    expect(onHover).toHaveBeenLastCalledWith("a");
    config.onPointMouseOut?.(undefined);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: undefined, highlightedLinkIndices: undefined,
    }));
    for (const method of [graph.setPointPositions, graph.setLinks, graph.fitView,
      graph.setZoomLevel, graph.setZoomTransformByPointPositions]) expect(method).not.toHaveBeenCalled();
    adapter.destroy();
    onHover.mockClear();
    adapter.focus(id);
    expect(onHover).not.toHaveBeenCalled();
  });

  it("suppresses focus callbacks while context is lost and replays focus only on a fresh restored engine", async () => {
    const container = document.createElement("div");
    const canvas = document.createElement("canvas");
    container.appendChild(canvas);
    document.body.appendChild(container);
    const onHover = vi.fn();
    const onContextStatus = vi.fn();
    const adapter = createWorkspaceCosmosAdapter(container, columns(), {
      forcedColors: false, reducedMotion: true,
      callbacks: { onHover, onContextStatus, onSelect: vi.fn() },
    });
    adapters.push(adapter);
    await adapter.whenSettled();
    createGraph.mock.calls[0][1].onPointMouseOver?.(0, [0, 0], undefined, false, false);
    canvas.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    expect(onContextStatus).toHaveBeenLastCalledWith("lost");
    onHover.mockClear();
    graph.setConfigPartial.mockClear();
    adapter.focus("bc");
    expect(onHover).not.toHaveBeenCalled();
    expect(graph.setConfigPartial).not.toHaveBeenCalled();
    canvas.dispatchEvent(new Event("webglcontextrestored"));
    expect(onContextStatus).toHaveBeenLastCalledWith("restored");
    adapter.focus("c");
    // Restoration asks the host to recreate GPU resources, not reuse this
    // lost engine. WorkspaceMapCanvas replays its current focus on the new one.
    expect(onHover).not.toHaveBeenCalled();
    expect(graph.setConfigPartial).not.toHaveBeenCalled();
    adapter.destroy();
    const restored = await mount();
    restored.focus("c");
    expect(createGraph).toHaveBeenCalledTimes(2);
    expect(graph.setConfigPartial).toHaveBeenLastCalledWith(expect.objectContaining({
      highlightedPointIndices: [2, 1], highlightedLinkIndices: [1],
    }));
  });

});

describe("bounded initial Map camera ownership", () => {
  const cameraEvent = { transform: { k: .4 } } as Parameters<NonNullable<GraphConfig["onZoom"]>>[0];
  const paintFrame = () => new Promise<void>((resolve) => window.setTimeout(resolve, 5));

  async function frameFixture(count = 2_000, bindHost?: (adapter: WorkspaceCosmosAdapter) => void) {
    const input = sizedColumns(count);
    const container = document.createElement("div"); document.body.appendChild(container);
    const onViewport = vi.fn();
    const adapter = createWorkspaceCosmosAdapter(container, input, { forcedColors: false, reducedMotion: false,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn(), onViewport } });
    adapters.push(adapter);
    // The canvas binds its carried selection, focus and search right after creation, before ready.
    bindHost?.(adapter);
    await adapter.whenSettled();
    const config = createGraph.mock.calls.at(-1)![1];
    config.onZoomEnd?.(cameraEvent, false);
    let now = 1_000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const current = Float32Array.from(graph.getPointPositions());
    graph.getPointPositions.mockClear(); graph.setPointPositions.mockClear();
    graph.setZoomTransformByPointPositions.mockClear(); graph.setZoomLevel.mockClear();
    return { adapter, config, input, current, onViewport, advance: (ms: number) => { now += ms; } };
  }

  it("frames current original GPU positions once per 200ms without recalibrating physics or uploading geometry", async () => {
    const { config, current, advance } = await frameFixture();
    const physicsBefore = forceCommits();
    config.onSimulationTick?.(.3); config.onSimulationTick?.(.3);
    await paintFrame();
    expect(graph.getPointPositions).toHaveBeenCalledTimes(1);
    expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(current, 0, undefined, .08, true);
    config.onZoom?.(cameraEvent, false); config.onZoomEnd?.(cameraEvent, false);
    config.onSimulationTick?.(.3); await paintFrame();
    expect(graph.getPointPositions).toHaveBeenCalledTimes(1);
    advance(200); config.onSimulationTick?.(.3); await paintFrame();
    expect(graph.getPointPositions).toHaveBeenCalledTimes(2);
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(forceCommits()).toEqual(physicsBefore);
  });

  it("frames admitted PC 10K from current GPU positions within one finite initialization window", async () => {
    const { config, current, advance } = await frameFixture(10_000);
    const physicsBefore = forceCommits();
    config.onSimulationTick?.(.3); await paintFrame();
    expect(graph.getPointPositions).toHaveBeenCalledTimes(1);
    expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(current, 0, undefined, .08, true);
    config.onZoom?.(cameraEvent, false); config.onZoomEnd?.(cameraEvent, false);
    advance(200); config.onSimulationTick?.(.3); await paintFrame();
    expect(graph.getPointPositions).toHaveBeenCalledTimes(2);
    advance(3_800); config.onSimulationTick?.(.3); await paintFrame();
    expect(graph.getPointPositions).toHaveBeenCalledTimes(2);
    expect(graph.setPointPositions).not.toHaveBeenCalled();
    expect(forceCommits()).toEqual(physicsBefore);
  });

  const intents = ["pointer", "wheel", "touch", "select", "focus", "pin", "search", "fit", "zoom", "pause", "reduce", "viewport", "metadata", "destroy", "lost"] as const;
  function perform(intent: typeof intents[number], adapter: WorkspaceCosmosAdapter, input: WorkspaceMapColumns) {
    const container = document.querySelector("canvas")!.parentElement!;
    if (intent === "pointer" || intent === "wheel" || intent === "touch") {
      container.dispatchEvent(new Event(intent === "pointer" ? "pointerdown" : intent === "wheel" ? "wheel" : "touchstart", { bubbles: true }));
    } else if (intent === "select") adapter.select("node:0");
    else if (intent === "focus") adapter.focus("node:0");
    else if (intent === "pin") adapter.pin("node:0", true);
    else if (intent === "search") adapter.setSearchMatches(new Set());
    else if (intent === "fit") adapter.fit();
    else if (intent === "zoom") adapter.zoomBy(1.2);
    else if (intent === "pause") adapter.stopLayout();
    else if (intent === "reduce") adapter.setReducedMotion(true);
    else if (intent === "viewport") adapter.setViewport({ zoom: .8, pan: { x: 10, y: 20 } });
    else if (intent === "metadata") adapter.replaceColumns({ ...input, nodeLabels: input.nodeLabels.map((value) => `${value}!`) });
    else if (intent === "lost") document.querySelector("canvas")!.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    else adapter.destroy();
  }

  it.each(intents.flatMap(intent => [2_000, 10_000].map(count => [intent, count] as const)))(
    "consumes initial framing before its RAF on %s at %i nodes, and Resume never rearms", async (intent, count) => {
    const { adapter, config, input, advance } = await frameFixture(count);
    config.onSimulationTick?.(.3);
    perform(intent, adapter, input);
    const cameraCalls = graph.setZoomTransformByPointPositions.mock.calls.length;
    await paintFrame();
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledTimes(cameraCalls);
    adapter.resumeLayout(); advance(200); config.onSimulationTick?.(.3); await paintFrame();
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledTimes(cameraCalls);
  });

  it.each(["pointer", "wheel", "touch", "select", "focus", "pin", "search", "pause", "reduce"] as const)(
    "interrupts a submitted native automatic Fit on %s before D3 applies it, without viewport echoes", async (intent) => {
      const { adapter, config, input, onViewport } = await frameFixture();
      let pending: (() => void) | null = null;
      const applied = vi.fn();
      graph.setZoomTransformByPointPositions.mockImplementation(() => { pending = applied; });
      graph.setZoomLevel.mockImplementation(() => {
        pending = null;
        config.onZoom?.(cameraEvent, false); config.onZoomEnd?.(cameraEvent, false);
      });
      config.onSimulationTick?.(.3); await paintFrame();
      expect(pending).not.toBeNull();
      onViewport.mockClear();
      perform(intent, adapter, input);
      expect(graph.setZoomLevel).toHaveBeenCalled();
      const flush = pending as (() => void) | null;
      flush?.();
      expect(applied).not.toHaveBeenCalled();
      expect(onViewport).not.toHaveBeenCalled();
    },
  );

  it("never applies a camera RAF delayed beyond its absolute four-second window", async () => {
    const { config, advance } = await frameFixture();
    config.onSimulationTick?.(.3); await paintFrame();
    config.onZoomEnd?.(cameraEvent, false);
    advance(3_999); config.onSimulationTick?.(.3);
    advance(1); await paintFrame();
    expect(graph.setZoomTransformByPointPositions).toHaveBeenCalledTimes(1);
  });

  it.each([2_000, 10_000])("does not read or frame hidden %i nodes and does not resurrect on visibility return", async count => {
    const { config, advance } = await frameFixture(count);
    config.onSimulationTick?.(.3);
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange")); await paintFrame();
    expect(graph.getPointPositions).not.toHaveBeenCalled();
    visibility.mockReturnValue("visible"); advance(200); config.onSimulationTick?.(.3); await paintFrame();
    expect(graph.getPointPositions).not.toHaveBeenCalled();
  });

  it.each(["initialPaused", "reducedMotion", "initialViewport", "initialCoordinates"] as const)(
    "never arms progressive camera framing for an initial %s scene", async (guard) => {
      const input = sizedColumns(2_000);
      const positions = buildCosmosSceneBuffers(input, readGraphTokens(document.body, "light")).positions;
      const container = document.createElement("div");
      document.body.appendChild(container);
      const adapter = createWorkspaceCosmosAdapter(container, input, {
        forcedColors: false,
        reducedMotion: guard === "reducedMotion",
        initialPaused: guard === "initialPaused",
        initialViewport: guard === "initialViewport" ? { zoom: .3, pan: { x: 888, y: 999 } } : null,
        initialCoordinates: guard === "initialCoordinates" ? { columns: input, positions } : null,
        callbacks: { onHover: vi.fn(), onSelect: vi.fn() },
      });
      adapters.push(adapter);
      await adapter.whenSettled();
      const config = createGraph.mock.calls.at(-1)![1];
      graph.getPointPositions.mockClear(); graph.setZoomTransformByPointPositions.mockClear();
      graph.fitView.mockClear();
      config.onSimulationTick?.(.3);
      await paintFrame();
      expect(graph.getPointPositions).not.toHaveBeenCalled();
      expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
      expect(graph.fitView).not.toHaveBeenCalled();

      // Returning from the initially saved motion state is not a fresh scene
      // and cannot grant the original initialization another camera window.
      if (guard === "initialPaused") adapter.resumeLayout();
      else if (guard === "reducedMotion") adapter.setReducedMotion(false);
      graph.getPointPositions.mockClear(); graph.setZoomTransformByPointPositions.mockClear();
      config.onSimulationTick?.(.3);
      await paintFrame();
      expect(graph.getPointPositions).not.toHaveBeenCalled();
      expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
      expect(graph.fitView).not.toHaveBeenCalled();
    },
  );

  it.each([[0, 608], [734, 0], [0, 0]])("does not read or progressively frame native CSS size %s by %s", async (width, height) => {
    const container = document.createElement("div"), canvas = document.createElement("canvas");
    Object.defineProperties(canvas, {
      clientWidth: { configurable: true, value: width },
      clientHeight: { configurable: true, value: height },
    });
    container.appendChild(canvas); document.body.appendChild(container);
    const adapter = createWorkspaceCosmosAdapter(container, sizedColumns(2_000), {
      forcedColors: false, reducedMotion: false,
      callbacks: { onHover: vi.fn(), onSelect: vi.fn() },
    });
    adapters.push(adapter);
    await adapter.whenSettled();
    const config = createGraph.mock.calls.at(-1)![1];
    graph.getPointPositions.mockClear(); graph.setZoomTransformByPointPositions.mockClear();
    graph.fitView.mockClear();
    config.onSimulationTick?.(.3);
    await paintFrame();
    expect(graph.getPointPositions).not.toHaveBeenCalled();
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    expect(graph.fitView).not.toHaveBeenCalled();
  });

  it("retires a scheduled canonical worker Fit when hidden and never replays it on visible return", async () => {
    const input = columns();
    let resolve!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValueOnce(new Promise(complete => { resolve = complete; }));
    const adapter = await mount(input, false);
    const config = createGraph.mock.calls.at(-1)![1];
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      frames.push(callback); return frames.length;
    });
    resolve(communityResult(input));
    await paintFrame();
    expect(frames).toHaveLength(1);
    const accepted = graph.getPointPositions();
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    graph.setZoomTransformByPointPositions.mockClear(); graph.fitView.mockClear();
    frames.shift()!(performance.now());
    expect(graph.getPointPositions()).toEqual(accepted);
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    expect(graph.fitView).not.toHaveBeenCalled();

    visibility.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    config.onSimulationTick?.(.3);
    adapter.stopLayout(); adapter.resumeLayout();
    expect(frames).toHaveLength(0);
    expect(graph.setZoomTransformByPointPositions).not.toHaveBeenCalled();
    expect(graph.fitView).not.toHaveBeenCalled();
    expect(graph.getPointPositions()).toEqual(accepted);
  });

  describe("one terminal Fit for a cooled PC native layout", () => {
    // The admitted native alpha starts at .35 and halves around 500 ticks.
    const COOLED = .17;
    const zoomCalls = () => graph.setZoomTransformByPointPositions.mock.calls.length;
    const wheel = () => document.querySelector("canvas")!.parentElement!.dispatchEvent(new Event("wheel", { bubbles: true }));

    /** The finite progressive window as the SDK drives it: one tick and its Zoom events per 200 ms. */
    async function progressiveWindow(config: GraphConfig, advance: (ms: number) => void) {
      for (let elapsed = 0; elapsed < 4_000; elapsed += 200) {
        config.onSimulationTick?.(.3); await paintFrame();
        config.onZoom?.(cameraEvent, false); config.onZoomEnd?.(cameraEvent, false);
        advance(200);
      }
    }
    async function cooledTick(config: GraphConfig, alpha = COOLED) {
      config.onSimulationTick?.(alpha); await paintFrame();
      config.onZoom?.(cameraEvent, false); config.onZoomEnd?.(cameraEvent, false);
    }

    it.each([[2_000, 0], [2_001, 1], [10_000, 1]] as const)(
      "a cooled %i-node native layout gets %i terminal Fit(s) after the progressive window, from current GPU positions", async (count, terminal) => {
        const { config, current, advance } = await frameFixture(count);
        await progressiveWindow(config, advance);
        const progressive = zoomCalls();
        advance(5_000);
        await cooledTick(config, .3); // still hot
        expect(zoomCalls()).toBe(progressive);
        await cooledTick(config);
        expect(zoomCalls()).toBe(progressive + terminal);
        if (terminal) expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(current, 0, undefined, .08, false);
        for (const alpha of [.16, .019, .001]) { advance(500); await cooledTick(config, alpha); }
        expect(zoomCalls()).toBe(progressive + terminal);
        expect(graph.setPointPositions).not.toHaveBeenCalled();
      },
    );

    it.each(["pointer", "fit", "pause", "reduce"] as const)(
      "never fits a cooled layout once %s took the camera, not even after Resume", async intent => {
        const { adapter, config, input, advance } = await frameFixture(10_000);
        await progressiveWindow(config, advance);
        advance(5_000);
        perform(intent, adapter, input);
        const fits = zoomCalls(), reads = graph.getPointPositions.mock.calls.length;
        await cooledTick(config);
        // Returning motion is permission for physics, never a fresh camera.
        adapter.resumeLayout(); adapter.setReducedMotion(false); advance(500); await cooledTick(config, .1);
        expect(zoomCalls()).toBe(fits);
        expect(graph.getPointPositions).toHaveBeenCalledTimes(reads);
      },
    );

    it("never frames a cooled layout while the tab is hidden, and does not resurrect it on return", async () => {
      const { config, advance } = await frameFixture(10_000);
      await progressiveWindow(config, advance);
      advance(5_000);
      const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
      document.dispatchEvent(new Event("visibilitychange"));
      const fits = zoomCalls(), reads = graph.getPointPositions.mock.calls.length;
      await cooledTick(config);
      visibility.mockReturnValue("visible"); advance(500); await cooledTick(config, .1);
      expect(zoomCalls()).toBe(fits);
      expect(graph.getPointPositions).toHaveBeenCalledTimes(reads);
    });

    it("gives a new visible scene a fresh camera that frames it once when it cools, even after the user owned the last one; the same identity gets none", async () => {
      const { adapter, config, input, advance } = await frameFixture(10_000);
      await progressiveWindow(config, advance);
      wheel(); // the user owns the first scene's camera
      // A different layout under the same visible identity is not a new scene.
      adapter.replaceColumns({ ...sizedColumns(10_000, 3), identity: input.identity });
      await paintFrame();
      advance(5_000);
      const owned = zoomCalls();
      await cooledTick(config);
      expect(zoomCalls()).toBe(owned);
      // Showing unlinked documents is a new visible scene on the same renderer.
      adapter.replaceColumns({ ...sizedColumns(10_000), identity: { ...input.identity, visibleFingerprint: "visible-with-unlinked" } });
      await paintFrame();
      config.onZoom?.(cameraEvent, false); config.onZoomEnd?.(cameraEvent, false);
      advance(250);
      await progressiveWindow(config, advance);
      advance(5_000);
      const before = zoomCalls();
      await cooledTick(config);
      expect(zoomCalls()).toBe(before + 1);
      expect(graph.setZoomTransformByPointPositions).toHaveBeenLastCalledWith(expect.any(Float32Array), 0, undefined, .08, false);
      for (const alpha of [.1, .001]) { advance(500); await cooledTick(config, alpha); }
      expect(zoomCalls()).toBe(before + 1);
    });

    it("lets the user own the rearmed camera of the new scene for good", async () => {
      const { adapter, config, input, advance } = await frameFixture(10_000);
      await progressiveWindow(config, advance);
      adapter.replaceColumns({ ...sizedColumns(10_000), identity: { ...input.identity, visibleFingerprint: "visible-then-wheel" } });
      await paintFrame();
      config.onZoom?.(cameraEvent, false); config.onZoomEnd?.(cameraEvent, false);
      wheel();
      advance(5_000);
      const before = zoomCalls();
      await cooledTick(config);
      advance(500); await cooledTick(config, .1);
      expect(zoomCalls()).toBe(before);
    });

    describe("viewport reports while an automatic camera may still change", () => {
      // The host parks every reported camera and restores it as a user-owned one,
      // which ends automatic framing. A camera that a later Fit will replace is not
      // worth parking.
      it("withholds the automatic Fits of a PC scene until the terminal Fit, then reports that one", async () => {
        const { adapter, config, onViewport, advance } = await frameFixture(10_000);
        await progressiveWindow(config, advance);
        expect(onViewport).not.toHaveBeenCalled();
        advance(5_000);
        await cooledTick(config);
        expect(onViewport).toHaveBeenLastCalledWith(adapter.getViewport());
      });

      it("reports a user-driven zoom end and the camera on screen when the user takes it, though a Fit could still follow", async () => {
        const { adapter, config, onViewport, advance } = await frameFixture(10_000);
        await progressiveWindow(config, advance);
        config.onZoomEnd?.(cameraEvent, true); // the gesture's start never reached the camera
        expect(onViewport).toHaveBeenCalledTimes(1);
        document.querySelector("canvas")!.parentElement!.dispatchEvent(new Event("pointerdown", { bubbles: true }));
        expect(onViewport).toHaveBeenCalledTimes(2);
        expect(onViewport).toHaveBeenLastCalledWith(adapter.getViewport());
      });
    });

    describe("intent that the host carries into a fresh renderer", () => {
      // The canvas replays the selection, focus and search it already holds right
      // after creating the adapter. That restores state; it does not take the camera.
      const carry = (adapter: WorkspaceCosmosAdapter) => {
        adapter.select("node:0"); adapter.focus("node:0"); adapter.setSearchMatches(new Set(["node:0"]));
      };

      it("frames a PC 10K scene progressively and once more when it cools, with a carried selection, focus and search", async () => {
        const { config, advance } = await frameFixture(10_000, carry);
        await progressiveWindow(config, advance);
        const progressive = zoomCalls();
        advance(5_000);
        await cooledTick(config);
        expect(zoomCalls()).toBe(progressive + 1);
      });

      it("lets a genuinely new selection after the first fit take the camera for good", async () => {
        const { adapter, config, advance } = await frameFixture(10_000, carry);
        await progressiveWindow(config, advance);
        adapter.select("node:1");
        advance(5_000);
        const fits = zoomCalls();
        await cooledTick(config);
        expect(zoomCalls()).toBe(fits);
      });
    });
  });
});

describe("tracked label rows across a coordinate upload", () => {
  const WIDTH = 7, HEIGHT = 6, INDICES = Array.from({ length: 40 }, (_, index) => index);
  const seeded = (input: WorkspaceMapColumns): Extract<WorkspaceMapCommunityOutcome, { kind: "complete" }> => ({
    kind: "complete",
    identityKey: JSON.stringify([input.identity.workspaceId, input.identity.actorId, input.identity.surface,
      input.identity.selectionKey, input.identity.visibleFingerprint]),
    initialPositions: Float32Array.from({ length: input.nodeIds.length * 2 }, (_, index) => 1_000 + index),
    communityIds: new Uint32Array(input.nodeIds.length), communityCount: 1, computeMs: 1,
  });

  /** The bounded readback surface of a WebGL2 context. The "texture" is the current native position
   * upload, snapshotted when readPixels is issued and delivered only after its fence is signaled. */
  function readbackGl() {
    let snapshot = new Float32Array(WIDTH * HEIGHT * 4), signaled = false;
    const handlers: Record<string, unknown> = {
      isContextLost: () => false,
      createBuffer: () => ({}),
      fenceSync: () => ({}),
      readPixels: () => {
        const native = graph.getPointPositions();
        snapshot = new Float32Array(WIDTH * HEIGHT * 4);
        INDICES.forEach((index, slot) => {
          snapshot.set([native[index * 2], native[index * 2 + 1], 1, 1], slot * 4);
        });
        signaled = false;
      },
      clientWaitSync: () => (signaled ? 2 : 1),
      getBufferSubData: (_target: number, _offset: number, output: Float32Array) => output.set(snapshot),
    };
    const constants: Record<string, number> = { TIMEOUT_EXPIRED: 1, ALREADY_SIGNALED: 2, CONDITION_SATISFIED: 3 };
    const gl = new Proxy({}, {
      get: (_, property) => typeof property !== "string" ? undefined
        : property in handlers ? handlers[property]
          : /^[A-Z][A-Z0-9_]*$/.test(property) ? constants[property] ?? 100 + property.length : () => null,
    });
    return { gl: gl as unknown as WebGL2RenderingContext, signal: () => { signaled = true; } };
  }

  const settleFrames = async () => { for (let frame = 0; frame < 4; frame += 1) await new Promise<void>(resolve => window.setTimeout(resolve, 5)); };

  async function labelScene() {
    vi.mocked(nativeTracking.createWorkspaceTrackedPositions).mockRestore();
    let now = 5_000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const input = sizedColumns(2_001);
    let resolveCommunity!: (outcome: WorkspaceMapCommunityOutcome) => void;
    community.compute.mockReturnValueOnce(new Promise<WorkspaceMapCommunityOutcome>(resolve => { resolveCommunity = resolve; }));
    const readback = readbackGl();
    const frame = { gl: readback.gl, framebuffer: {} as WebGLFramebuffer, width: WIDTH, height: HEIGHT, indices: INDICES };
    graph.getTrackedPointPositionsFrame.mockImplementation(() => frame as never);
    graph.spaceToScreenPosition.mockImplementation(point => point);
    // The first tracked read is issued while mounting and holds the pre-seed texture.
    const adapter = await mount(input, true, vi.fn(), false, vi.fn(), WORKSPACE_MAP_PC_10K_BUDGET);
    const container = createGraph.mock.calls.at(-1)![0];
    const labels = [...container.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span")];
    return { adapter, input, readback, labels, reseed: () => resolveCommunity(seeded(input)),
      advance: (ms: number) => { now += ms; } };
  }

  it("drops a readback issued before a community re-seed instead of publishing pre-seed coordinates", async () => {
    const { adapter, input, readback, labels, reseed, advance } = await labelScene();
    expect(graph.getTrackedPointPositionsFrame).toHaveBeenCalled();
    const preSeed = graph.getPointPositions().slice(0, 2);
    reseed(); await settleFrames();
    expect(graph.getPointPositions().slice(0, 2)).not.toEqual(preSeed); // the upload really replaced the texture
    readback.signal(); await settleFrames();
    // The upload dropped the pre-seed read and requested one of its own rows:
    // a kept pre-seed read would occupy the only slot and publish preSeed here.
    expect(labels[0].dataset).toMatchObject({ nodeId: input.nodeIds[0], pointX: "1000", pointY: "1001" });

    // The next bounded read, issued after the upload, is valid and publishes the new coordinates.
    advance(1_000); adapter.setLabelsVisible(true); await settleFrames();
    readback.signal(); await settleFrames();
    expect(labels[0].dataset).toMatchObject({ nodeId: input.nodeIds[0], pointX: "1000", pointY: "1001" });
  });
});

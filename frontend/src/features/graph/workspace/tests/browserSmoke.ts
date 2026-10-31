import { NativeGraph as Graph } from "../cosmosNative";

import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import { workspaceMapCommunityIdentityKey } from "../../map/communityJob";
import type { GraphViewport } from "../../shared/rendererTypes";
import { createWorkspaceCosmosAdapter } from "../cosmosAdapter";
import { detectWorkspaceMapWebGL } from "../webglSupport";
import type { WorkspaceForceAtlas2Units } from "../forceAtlas2Units";
import { WORKSPACE_MAP_PC_10K_BUDGET } from "../../shared/workspaceMapBudget";
import { workspaceNominalGlyphRadiusCss } from "../forceAtlas2Units";
import { measureNativeGeometry } from "./nativeGeometry";
import { assertPcNativePositions, hasPcNativeMovement, observePcNativeFrame, type PcNativeDiagnostics, type PcNativeFrame, type PcNativeSource } from "./pcNativeObservation";

type InitialCameraObservation = {
  fits: Array<{ at: number; points: number; before: GraphViewport | null }>;
  contactCanonicalFits: Array<{ at: number; points: number }>;
  viewportEvents: number;
  cancellation: { before: GraphViewport | null; after: GraphViewport | null;
    positionsBefore: number[]; positionsAfter: number[]; echoes: number } | null;
};

type NativeCoolingObservation = {
  afterStep: { alpha: number; running: boolean };
  afterEnd: { alpha: number; running: boolean };
  afterCamera: { alpha: number; running: boolean };
  afterResume: { alpha: number; running: boolean };
  endEvents: number;
  manualTicks: number;
  cameraTicks: number;
  nodeCount: number;
  relationCount: number;
  geometryUploads: number;
  topologyUploads: number;
  glError: number;
};

type NativePinReleaseFrame = {
  alpha: number;
  running: boolean;
  nativeTicks: number;
  canonicalPosition: [number, number];
  pinned: number[];
  nodeCount: number;
  relationCount: number;
  topologyMatches: boolean;
  geometryUploads: number;
  topologyUploads: number;
  forceCommits: number;
  collisionCommits: number;
  glError: number;
};

type NativePinReleaseObservation = {
  action: "unpin" | "reset";
  id: string;
  target: [number, number];
  beforePin: NativePinReleaseFrame;
  afterPin: NativePinReleaseFrame;
  afterStep: NativePinReleaseFrame;
  afterEnd: NativePinReleaseFrame;
  afterRelease: NativePinReleaseFrame;
  afterMovement: NativePinReleaseFrame;
  paused: NativePinReleaseFrame;
  frozen: NativePinReleaseFrame;
  manualTicks: number;
  endEvents: number;
};

type NativeFitTransform = { x: number; y: number; k: number };

declare global {
  interface Window {
    __workspaceMapSmoke?: {
      status: "loading" | "ready" | "error";
      error?: string;
      runtimeError?: string;
      nodeCount?: number;
      edgeCount?: number;
      hoveredId?: string | null;
      renderer?: string | null;
      glError?: number;
      canvas?: { width: number; height: number };
      labelCount?: number;
      togglesApplied?: boolean;
      workerStatus?: "pending" | "complete" | "error";
      workerComputeMs?: number;
      workerElapsedMs?: number;
      communityCount?: number;
      minCoordinate?: number;
      maxCoordinate?: number;
      coordinateCount?: number;
      viewport?: { zoom: number; pan: { x: number; y: number } } | null;
    };
    __workspaceMapSearch?: {
      snapshot(): {
        positions: number[];
        viewport: GraphViewport | null;
        geometryUploads: number;
        workerRequests: number;
        nodeCount: number;
        edgeCount: number;
        matchingPointCount: number | null;
        glError: number;
      };
      zoom(): void;
    };
    __workspaceMapInteraction?: {
      snapshot(): {
        positions: number[];
        canonicalPositions: number[];
        nativeSpaceSize: number;
        maxTextureSize: number;
        viewport: GraphViewport | null;
        pointSizeScale: number;
        collisionRadius: number | undefined;
        collisionPadding: number;
        collisionCommits: number;
        forceCommits: number;
        forceConfig: number[];
        selectedId: string | null;
        selections: Array<string | null>;
        dragPins: Array<{ id: string; position: [number, number] }>;
        draggingIndex: number | null;
        geometryUploads: number;
        topologyUploads: number;
        uploadedRelationCount: number;
        uploadedTopologyMatches: boolean;
        collisionFootprint: { radii: number; minRadius: number; maxRadius: number; padding: number;
          legacyUniformAreaFraction: number; perPointAreaFraction: number } | null;
        nativeTicks: number;
        highlightedLinks: number[];
        linkGreyoutOpacity: number;
        running: boolean;
        layoutRunning: boolean;
        layoutTicks: number;
        layoutUnits: WorkspaceForceAtlas2Units | null;
        pinned: number[];
        glError: number;
      };
      calibration(): {
        fallbackPositions: number[];
        workerPositions: number[];
        pointSizes: number[];
        width: number;
        height: number;
      };
      initialCamera(): InitialCameraObservation;
      nativeTickCount(): number;
      nativeCooling(): Promise<NativeCoolingObservation>;
      nativePinRelease(action: "unpin" | "reset"): Promise<NativePinReleaseObservation>;
      fitAndWait(): Promise<{ requested: NativeFitTransform; completed: NativeFitTransform }>;
      boundaryPointFrame(indices: readonly number[]): {
        nodeCount: number;
        targets: Array<{ index: number; screen: [number, number]; otherNodeClearance: number }>;
      };
      pcCheckpoints(before: { positions: number[]; nativeTicks: number }): Promise<{
        geometry: Array<ReturnType<typeof measureNativeGeometry> & { tick: number; phase: string }>;
        paused: PcNativeFrame; frozen: PcNativeFrame; pausedFits: InitialCameraObservation["fits"];
        initialFits: InitialCameraObservation["fits"]; initialMovementObserved: boolean; diagnostics: PcNativeDiagnostics;
      }>;
      pcResume(before: number[]): Promise<{ resumed: PcNativeFrame; diagnostics: PcNativeDiagnostics; renderer: string | null }>;
      geometry(): ReturnType<typeof measureNativeGeometry>;
      point(index: number): [number, number];
      dragTarget(): { index: number; id: string; screen: [number, number]; clearance: number };
      isolate(): [number, number];
      pinAtScreen(index: number, screen: [number, number]): void;
      focus(id: string | null): void;
      fit(): void;
      zoom(factor: number): void;
      stop(): void;
      pause(): void;
      resume(): void;
      reduce(enabled: boolean): void;
      resize(): void;
      destroy(): void;
    };
  }
}

const query = new URLSearchParams(window.location.search);
const smallScene = query.get("geometry") === "40";
const mediumScene = query.get("geometry") === "2000";
const pcScene = query.get("geometry") === "10000";
const linksOnly = query.get("topology") === "links";
const reducedMotion = query.get("motion") === "reduce";
const lightTheme = query.get("theme") === "light";
const count = smallScene ? 40 : mediumScene ? 2_000 : pcScene ? 10_000 : 20_000;
const edgeCount = smallScene ? 40 : mediumScene ? 4_700 : pcScene
  ? query.get("relations") === "100000" ? 100_000 : 30_895 : 47_000;
const stage = document.getElementById("stage") as HTMLDivElement;
if (lightTheme) {
  document.documentElement.setAttribute("data-theme", "light");
  document.documentElement.style.backgroundColor = "#ffffff";
  document.body.style.backgroundColor = "#ffffff";
  stage.style.backgroundColor = "#ffffff";
  stage.style.color = "#111827";
}
if (query.get("size") === "app") {
  stage.style.width = "734px";
  stage.style.height = "600px";
}
window.__workspaceMapSmoke = { status: "loading", workerStatus: "pending" };
const searchFixture = query.get("search") === "stability";
const interactionFixture = query.get("interaction") === "targets";
const boundaryLabelFixture = query.get("labels") === "boundary";
// Opt-in upstream PC-native camera diagnostic only. All ordinary 2K fixtures
// use the product's default official CPU Worker and GPU drawing/picking.
const pcNativeCameraDiagnostic = query.get("camera") === "cancel-focus" && query.get("budget") === "pc10k";
const pcBudget = query.get("budget") === "pc10k";
const observedGraph: { current: Graph | null } = { current: null };
let geometryUploads = 0;
let topologyUploads = 0;
let uploadedRelationCount = 0;
let uploadedTopologyMatches = false;
let workerRequests = 0;
let collisionCommits = 0;
let forceCommits = 0;
let fallbackPositions: Float32Array | null = null;
let workerPositions: Float32Array | null = null;
let pointSizes: Float32Array | null = null;
let collisionRadii: Float32Array | null = null;
let nativeTicks = 0;
let nativePositionReads = 0;
let remountCaptures = 0;
const observedTicks = new WeakSet<Graph>();
let calibrationViewport: { width: number; height: number } | null = null;
const initialCamera: InitialCameraObservation = { fits: [], contactCanonicalFits: [], viewportEvents: 0, cancellation: null };
let observeAutomaticFit: (() => void) | null = null;
if (searchFixture || interactionFixture) {
  if (pcScene) {
    const read = Graph.prototype.getPointPositions;
    Graph.prototype.getPointPositions = function (...args) {
      nativePositionReads++;
      return read.apply(this, args);
    };
  }
  // Fixture-only observation of actual Cosmos GPU geometry, not a production hook.
  const nativeRender = Graph.prototype.render;
  Graph.prototype.render = function (...args) {
    observedGraph.current = this;
    if (!observedTicks.has(this) && this.config.onSimulationTick) {
      observedTicks.add(this);
      const tick = this.config.onSimulationTick;
      this.config.onSimulationTick = (...tickArgs) => { nativeTicks++; tick(...tickArgs); };
    }
    return nativeRender.apply(this, args);
  };
  const nativePositions = Graph.prototype.setPointPositions;
  Graph.prototype.setPointPositions = function (...args) {
    geometryUploads += 1;
    fallbackPositions ??= args[0].slice();
    return nativePositions.apply(this, args);
  };
  const nativeLinks = Graph.prototype.setLinks;
  Graph.prototype.setLinks = function (...args) {
    topologyUploads += 1;
    uploadedRelationCount = args[0].length / 2;
    uploadedTopologyMatches = uploadedRelationCount === edgeCount && args[0].every((value, offset) => {
      const edge = Math.floor(offset / 2);
      return value === (offset % 2 === 0 ? edge % count : linksOnly
        ? (edge % count + 1 + Math.floor(edge / count) * 73) % count : (edge * 13 + 1) % count);
    });
    return nativeLinks.apply(this, args);
  };
  const nativeSizes = Graph.prototype.setPointSizes;
  Graph.prototype.setPointSizes = function (...args) {
    pointSizes = args[0].slice();
    return nativeSizes.apply(this, args);
  };
  const nativeCollisionRadii = Graph.prototype.setPointCollisionRadii;
  Graph.prototype.setPointCollisionRadii = function (radii) {
    collisionRadii = radii?.slice() ?? null;
    return nativeCollisionRadii.call(this, radii);
  };
  const nativeConfig = Graph.prototype.setConfigPartial;
  Graph.prototype.setConfigPartial = function (next) {
    if (next.simulationCollisionRadius !== undefined || next.simulationCollisionPadding !== undefined) {
      collisionCommits += 1;
      const canvas = stage.querySelector("canvas");
      if (canvas) calibrationViewport ??= { width: canvas.clientWidth, height: canvas.clientHeight };
    }
    if (next.simulationGravity !== undefined || next.simulationRepulsion !== undefined
      || next.simulationLinkSpring !== undefined || next.simulationLinkDistance !== undefined) forceCommits += 1;
    return nativeConfig.call(this, next);
  };
  const nativeFit = Graph.prototype.setZoomTransformByPointPositions;
  Graph.prototype.setZoomTransformByPointPositions = function (...args) {
    const result = nativeFit.apply(this, args);
    // Observation after the real SDK submission, before its queued D3 Fit.
    // Only the new progressive initial Fits retain simulation permission.
    if (args[4] === true) observeAutomaticFit?.();
    if (args[4] === false && document.getElementById("stage")?.dataset.mapLayoutPhase === "contact") {
      initialCamera.contactCanonicalFits.push({ at: performance.now(), points: args[0].length / 2 });
    }
    return result;
  };
}

async function main() {
  const support = detectWorkspaceMapWebGL();
  if (!support.supported) throw new Error(`WebGL unavailable: ${support.reason}`);
  const nodeIds = Array.from({ length: count }, (_, index) => `node-${index}`);
  const columns: WorkspaceMapColumns = {
    identity: { workspaceId: "synthetic", actorId: "smoke", surface: "workspace", selectionKey: "combined", visibleFingerprint: `synthetic-${count}-${edgeCount}` },
    nodeIds,
    nodeKeys: nodeIds.map((_, index) => `context:${String(index).padStart(5, "0")}`),
    nodeLabels: nodeIds.map((id, index) => {
      if (boundaryLabelFixture && index === count - 2) return "지식관계분석문서제목";
      if (boundaryLabelFixture && index === count - 1) {
        return "A newly focused document outside the overview top forty must show its complete multiline title, including this final sentence at the bottom right boundary.";
      }
      return interactionFixture && index === 0
        ? "A long document title whose complete ending must remain visible on focus" : id;
    }),
    nodeFolderKeys: nodeIds.map((_, index) => `folder-${index % (smallScene ? 4 : 143)}`),
    nodeKinds: Uint8Array.from({ length: count }, (_, index) => linksOnly ? 0 : index % 11 === 0 ? 1 : 0),
    nodeSizes: Float32Array.from({ length: count }, (_, index) => linksOnly ? 3.75 : 1.5 + index % 6),
    edgeIds: Array.from({ length: edgeCount }, (_, index) => `edge-${index}`),
    edgeSources: Uint32Array.from({ length: edgeCount }, (_, index) => index % count),
    edgeTargets: Uint32Array.from({ length: edgeCount }, (_, index) => linksOnly
      ? (index % count + 1 + Math.floor(index / count) * 73) % count
      : (index * 13 + 1) % count),
    edgeKinds: Uint8Array.from({ length: edgeCount }, (_, index) => linksOnly ? 0 : index % 4 === 0 ? 1 : 0),
    edgeCounts: new Uint32Array(edgeCount).fill(1),
    edgeWeights: new Float64Array(edgeCount).fill(1),
    edgeCrossFolder: new Uint8Array(edgeCount),
  };
  const expectedIdentityKey = workspaceMapCommunityIdentityKey(columns);
  const workerStartedAt = performance.now();
  const NativeWorker = window.Worker;
  // Smoke-only observation of the real worker response; no adapter test hook.
  window.Worker = class ObservedWorker extends NativeWorker {
    constructor(scriptURL: string | URL, options?: WorkerOptions) {
      super(scriptURL, options);
      workerRequests += 1;
      this.addEventListener("message", (event: MessageEvent) => {
        const response = event.data;
        if (!window.__workspaceMapSmoke || response?.identityKey !== expectedIdentityKey) return;
        if (response.type === "error") {
          window.__workspaceMapSmoke.workerStatus = "error";
          return;
        }
        if (response.type !== "complete") return;
        const positions = response.initialPositions as Float32Array;
        workerPositions = positions.slice();
        let min = Infinity;
        let max = -Infinity;
        for (const position of positions) {
          min = Math.min(min, position);
          max = Math.max(max, position);
        }
        window.__workspaceMapSmoke.workerStatus = "complete";
        window.__workspaceMapSmoke.workerComputeMs = response.computeMs;
        window.__workspaceMapSmoke.workerElapsedMs = performance.now() - workerStartedAt;
        window.__workspaceMapSmoke.communityCount = response.communityCount;
        window.__workspaceMapSmoke.minCoordinate = min;
        window.__workspaceMapSmoke.maxCoordinate = max;
        window.__workspaceMapSmoke.coordinateCount = positions.length;
      });
    }
  };
  const selections: Array<string | null> = [];
  const dragPins: Array<{ id: string; position: [number, number] }> = [];
  let selectedId: string | null = null;
  const adapter = createWorkspaceCosmosAdapter(stage, columns, {
    forcedColors: false, reducedMotion,
    ...(pcBudget ? { budget: WORKSPACE_MAP_PC_10K_BUDGET } : {}),
    callbacks: {
      onHover(id) { if (window.__workspaceMapSmoke) window.__workspaceMapSmoke.hoveredId = id; },
      onRuntimeError(error) { if (window.__workspaceMapSmoke) window.__workspaceMapSmoke.runtimeError = String(error); },
      onSelect(id) { selectedId = id; selections.push(id); },
      onPin(id, position) { dragPins.push({ id, position: [position.x, position.y] }); },
      onViewport() { initialCamera.viewportEvents += 1; },
    },
  });
  if (pcScene) {
    const capture = adapter.captureCoordinates.bind(adapter);
    adapter.captureCoordinates = () => { remountCaptures++; return capture(); };
  }
  observeAutomaticFit = () => {
    const graph = observedGraph.current!;
    initialCamera.fits.push({ at: performance.now(), points: columns.nodeIds.length, before: adapter.getViewport() });
    if (!pcNativeCameraDiagnostic || initialCamera.cancellation) return;
    // Freeze native physics only to isolate real D3 camera cancellation. Do
    // not use adapter.stopLayout(), which would itself cancel and mask focus.
    graph.pause();
    const before = adapter.getViewport();
    const positionsBefore = Array.from(graph.getPointPositions());
    const echoesBefore = initialCamera.viewportEvents;
    adapter.focus("node-0");
    graph.pause();
    initialCamera.cancellation = { before, after: adapter.getViewport(), positionsBefore,
      positionsAfter: Array.from(graph.getPointPositions()), echoes: initialCamera.viewportEvents - echoesBefore };
  };
  await adapter.whenSettled();
  await new Promise<void>((resolve, reject) => {
    const startedAt = performance.now();
    function check() {
      const status = window.__workspaceMapSmoke?.workerStatus;
      if (status === "complete") return resolve();
      if (status === "error" || performance.now() - startedAt > 12_000) {
        return reject(new Error(`Community worker did not complete: ${status}`));
      }
      setTimeout(check, 25);
    }
    check();
  });
  if (query.get("fit") === "manual") {
    adapter.fit();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }
  if (query.get("delay") === "5000") {
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const labelLayer = stage.querySelector<HTMLElement>("[data-workspace-map-labels]");
  const labelCount = labelLayer?.querySelectorAll("span").length ?? 0;
  if (!labelLayer || labelCount > 40) throw new Error(`Unbounded Map label layer: ${labelCount}`);
  adapter.setLabelsVisible(false);
  if (labelLayer.style.display !== "none") throw new Error("Map labels did not hide");
  adapter.setLabelsVisible(true);
  adapter.setEdgesVisible(false);
  adapter.setEdgesVisible(true);
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const canvas = stage.querySelector("canvas");
  if (!canvas) throw new Error("Cosmos canvas absent");
  const gl = canvas.getContext("webgl2");
  if (!gl) throw new Error("Cosmos WebGL2 context absent");
  const debug = gl.getExtension("WEBGL_debug_renderer_info");
  window.__workspaceMapSmoke = {
    ...window.__workspaceMapSmoke,
    status: "ready", nodeCount: count, edgeCount,
    renderer: debug ? String(gl.getParameter(debug.UNMASKED_RENDERER_WEBGL)) : null,
    glError: gl.getError(), canvas: { width: canvas.width, height: canvas.height },
    labelCount, togglesApplied: true,
    viewport: adapter.getViewport(),
  };
  if (searchFixture) {
    const search = document.createElement("input");
    search.type = "search";
    search.setAttribute("aria-label", "Find in visible map");
    Object.assign(search.style, { position: "fixed", top: "8px", left: "8px", zIndex: "2" });
    search.addEventListener("input", () => {
      const term = search.value.trim().toLocaleLowerCase();
      adapter.setSearchMatches(term ? new Set(columns.nodeIds.filter((_id, index) =>
        columns.nodeLabels[index].toLocaleLowerCase().includes(term))) : null);
    });
    document.body.appendChild(search);
    window.__workspaceMapSearch = {
      snapshot: () => ({
        positions: Array.from(observedGraph.current?.getPointPositions() ?? []),
        viewport: adapter.getViewport(),
        geometryUploads,
        workerRequests,
        nodeCount: columns.nodeIds.length,
        edgeCount: columns.edgeIds.length,
        matchingPointCount: observedGraph.current?.config.highlightedPointIndices?.length ?? null,
        glError: gl.getError(),
      }),
      zoom: () => adapter.zoomBy(1.5),
    };
  }
  if (interactionFixture) {
    const point = (index: number): [number, number] => {
      const graph = observedGraph.current!;
      const positions = graph.getPointPositions();
      return graph.spaceToScreenPosition([positions[index * 2], positions[index * 2 + 1]]);
    };
    const geometryFromPositions = (positions: ArrayLike<number>) => {
      const graph = observedGraph.current!;
      const zoom = graph.getZoomLevel();
      const sizeScale = graph.config.pointSizeScale * Math.min(5, Math.max(1, zoom * .01));
      const range = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE) as Float32Array;
      const cap = range[1] / graph.config.pixelRatio;
      const radii = Float32Array.from(pointSizes!, (diameter, index) =>
        workspaceNominalGlyphRadiusCss(Math.min(diameter * sizeScale, cap), columns.nodeKinds[index] === 1 ? 1 : 0) / zoom);
      return measureNativeGeometry(positions, radii, graph.config.spaceSize,
        graph.getGpuSimulationFrame()?.pinnedIndices ?? []);
    };
    const pcSource: PcNativeSource = {
      positions: () => {
        const positions = observedGraph.current!.getPointPositions();
        assertPcNativePositions(positions, columns.nodeIds.length);
        return positions;
      },
      counters: () => ({ positionReads: nativePositionReads, remountCaptures }),
      state: () => ({ viewport: adapter.getViewport(), running: observedGraph.current!.isSimulationRunning,
        geometryUploads, topologyUploads, nativeTicks, fits: structuredClone(initialCamera.fits), glError: gl.getError() }),
      geometry: geometryFromPositions,
    };
    const pcDiagnostics = (): PcNativeDiagnostics => ({ frames: 0, positionReads: 0, remountCaptures: 0,
      glErrorCodes: [], checkpoints: [] });
    const nativeFrame = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
    window.__workspaceMapInteraction = {
      snapshot: () => {
        const coordinates = adapter.captureCoordinates();
        const graph = observedGraph.current!;
        let collisionFootprint = null;
        if (collisionRadii && pointSizes) {
          const nominal = workspaceNominalGlyphRadiusCss(pointSizes[0], linksOnly ? 0 : 1);
          const inverseZoom = collisionRadii[0] / nominal;
          const largest = pointSizes.reduce((maximum, size) => Math.max(maximum, size), 0);
          const padding = graph.config.simulationCollisionPadding;
          const area = graph.config.spaceSize ** 2;
          collisionFootprint = { radii: collisionRadii.length,
            minRadius: collisionRadii.reduce((minimum, radius) => Math.min(minimum, radius), Infinity),
            maxRadius: collisionRadii.reduce((maximum, radius) => Math.max(maximum, radius), 0), padding,
            legacyUniformAreaFraction: collisionRadii.length * Math.PI * (largest * inverseZoom / 2 + padding) ** 2 / area,
            perPointAreaFraction: collisionRadii.reduce((sum, radius) => sum + Math.PI * (radius + padding) ** 2, 0) / area };
        }
        return {
          positions: Array.from(observedGraph.current!.getPointPositions()),
          canonicalPositions: Array.from(coordinates?.positions ?? []),
          nativeSpaceSize: observedGraph.current!.config.spaceSize,
          maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE) as number,
          viewport: adapter.getViewport(),
          pointSizeScale: observedGraph.current!.config.pointSizeScale,
          collisionRadius: observedGraph.current!.config.simulationCollisionRadius,
          collisionPadding: observedGraph.current!.config.simulationCollisionPadding,
          collisionCommits,
          forceCommits,
          forceConfig: [observedGraph.current!.config.simulationGravity,
            observedGraph.current!.config.simulationRepulsion,
            observedGraph.current!.config.simulationLinkSpring,
            observedGraph.current!.config.simulationLinkDistance,
            ...observedGraph.current!.config.simulationLinkDistRandomVariationRange],
          selectedId, selections: [...selections],
          dragPins: dragPins.map(({ id, position }) => ({ id, position: [...position] })),
          draggingIndex: observedGraph.current!.getGpuSimulationFrame()?.draggingIndex ?? null,
          geometryUploads,
          topologyUploads,
          uploadedRelationCount,
          uploadedTopologyMatches,
          collisionFootprint,
          nativeTicks,
          highlightedLinks: [...(observedGraph.current!.config.highlightedLinkIndices ?? [])],
          linkGreyoutOpacity: observedGraph.current!.config.linkGreyoutOpacity,
          running: observedGraph.current!.isSimulationRunning,
          layoutRunning: stage.dataset.mapLayoutRunning === "true",
          layoutTicks: Number(stage.dataset.mapLayoutTicks ?? 0),
          layoutUnits: coordinates?.forceAtlas2Units ?? null,
          pinned: [...(observedGraph.current!.getGpuSimulationFrame()?.pinnedIndices ?? [])],
          glError: gl.getError(),
        };
      },
      calibration: () => {
        if (!fallbackPositions || !workerPositions || !pointSizes || !calibrationViewport) {
          throw new Error("Native scene calibration inputs were not observed");
        }
        return { fallbackPositions: Array.from(fallbackPositions), workerPositions: Array.from(workerPositions),
          pointSizes: Array.from(pointSizes), ...calibrationViewport };
      },
      initialCamera: () => structuredClone(initialCamera),
      nativeTickCount: () => nativeTicks,
      pcCheckpoints: async before => {
        if (!pcScene || !pcBudget) throw new Error("Lean native observations require the PC10K fixture");
        const diagnostics = pcDiagnostics(), initialFits = structuredClone(initialCamera.fits);
        let current = observePcNativeFrame(pcSource, diagnostics, true, "ready");
        const geometry = [{ ...current.geometry!, tick: current.nativeTicks, phase: "ready" }];
        for (const delta of [8, 16]) {
          while (nativeTicks < before.nativeTicks + delta) await nativeFrame();
          current = observePcNativeFrame(pcSource, diagnostics, true, `ready+${delta}`);
          geometry.push({ ...current.geometry!, tick: current.nativeTicks, phase: `ready+${delta}` });
        }
        let initialMovementObserved = hasPcNativeMovement(before.positions, current.positions);
        while (!initialMovementObserved) {
          await nativeFrame();
          current = observePcNativeFrame(pcSource, diagnostics);
          initialMovementObserved = hasPcNativeMovement(before.positions, current.positions);
        }
        adapter.stopLayout();
        const paused = observePcNativeFrame(pcSource, diagnostics, false, "paused");
        const pausedFits = paused.fits;
        await nativeFrame(); await nativeFrame(); // The original two-RAF freeze check.
        const frozen = observePcNativeFrame(pcSource, diagnostics, false, "frozen");
        return { geometry, paused, frozen, pausedFits, initialFits, initialMovementObserved, diagnostics };
      },
      pcResume: async before => {
        if (!pcScene || !pcBudget) throw new Error("Lean native observations require the PC10K fixture");
        const diagnostics = pcDiagnostics();
        adapter.focus("node-0"); adapter.resumeLayout();
        let resumed: PcNativeFrame;
        do { await nativeFrame(); resumed = observePcNativeFrame(pcSource, diagnostics, false, "resume-observation"); }
        while (!hasPcNativeMovement(before, resumed.positions));
        return { resumed, diagnostics, renderer: window.__workspaceMapSmoke?.renderer ?? null };
      },
      fitAndWait: () => {
        const graph = observedGraph.current!;
        if (graph.isSimulationRunning || stage.dataset.mapLayoutRunning === "true") {
          throw new Error("Fit completion diagnostic requires a stopped layout");
        }
        // Fixture-only observation of the real SDK's pure Fit calculation and
        // completion, not a delay or a production camera/debug API.
        const native = graph as unknown as { zoomInstance: {
          getTransform(positions: Float32Array, scale: undefined, padding: number): NativeFitTransform;
          eventTransform: NativeFitTransform;
        } };
        const requested = { ...native.zoomInstance.getTransform(Float32Array.from(graph.getPointPositions()), undefined, .08) };
        const matches = (transform: NativeFitTransform) => transform.x === requested.x
          && transform.y === requested.y && transform.k === requested.k;
        return new Promise((resolve, reject) => {
          const previous = graph.config.onZoomEnd;
          let issued = false;
          graph.config.onZoomEnd = (event, userDriven) => {
            previous?.(event, userDriven);
            if (!issued || userDriven || !matches(event.transform) || !matches(native.zoomInstance.eventTransform)) return;
            graph.config.onZoomEnd = previous;
            resolve({ requested, completed: { ...native.zoomInstance.eventTransform } });
          };
          try { adapter.fit(); issued = true; }
          catch (error) { graph.config.onZoomEnd = previous; reject(error); }
        });
      },
      boundaryPointFrame: indices => {
        if (!boundaryLabelFixture || new Set(indices).size !== indices.length
          || indices.some(index => !Number.isInteger(index) || index < 0 || index >= count)) {
          throw new Error("Invalid boundary-label point frame request");
        }
        const graph = observedGraph.current!, positions = graph.getPointPositions();
        const targets = new Set(indices);
        // One GPU position snapshot and one current camera for every center.
        const screens = columns.nodeIds.map((_id, index) => graph.spaceToScreenPosition([
          positions[index * 2], positions[index * 2 + 1],
        ]));
        return { nodeCount: positions.length / 2, targets: indices.map(index => ({ index, screen: screens[index],
          otherNodeClearance: screens.reduce((nearest, screen, other) => targets.has(other) ? nearest
            : Math.min(nearest, Math.hypot(screen[0] - screens[index][0], screen[1] - screens[index][1])), Infinity),
        })) };
      },
      nativeCooling: async () => {
        if (!pcScene || !pcBudget) throw new Error("Natural cooling diagnostic requires the PC10K native fixture");
        const graph = observedGraph.current!;
        // Test-only access to the real SDK store: use public start/step to
        // cross its unchanged alpha threshold, never modify a force or seed.
        const store = (graph as unknown as { store: { alpha: number } }).store;
        const read = () => ({ alpha: store.alpha, running: graph.isSimulationRunning });
        const frames = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
        adapter.focus(columns.nodeIds[0]); // Retire initial camera ownership, not physics.
        adapter.resumeLayout();
        const priorEnd = graph.config.onSimulationEnd;
        let endEvents = 0;
        graph.config.onSimulationEnd = () => { endEvents++; priorEnd?.(); };
        try {
          const beforeTicks = nativeTicks;
          graph.start(.0010005);
          graph.step(); // One real GPU force step and real upstream alpha decay.
          const afterStep = read(), manualTicks = nativeTicks - beforeTicks;
          // The already scheduled native RAF must observe alpha<.001 and end.
          await frames();
          const afterEnd = read(), endedTicks = nativeTicks;
          adapter.fit(); adapter.zoomBy(1.2);
          await frames();
          const afterCamera = read(), cameraTicks = nativeTicks - endedTicks;
          adapter.resumeLayout();
          const afterResume = read();
          adapter.stopLayout();
          const frame = graph.getGpuSimulationFrame();
          if (!frame) throw new Error("Native cooling diagnostic lost its GPU frame");
          return { afterStep, afterEnd, afterCamera, afterResume, endEvents, manualTicks, cameraTicks,
            nodeCount: frame.nodeCount, relationCount: uploadedRelationCount,
            geometryUploads, topologyUploads, glError: frame.gl.getError() };
        } finally { graph.config.onSimulationEnd = priorEnd; }
      },
      nativePinRelease: async action => {
        if (!pcScene || !pcBudget || columns.nodeIds.length !== 10_000 || uploadedRelationCount !== 30_895
          || stage.dataset.mapCommunityLayoutStatus !== "applied"
          || (action !== "unpin" && action !== "reset")) {
          throw new Error("Released-pin diagnostic requires the representative PC10K native fixture");
        }
        const graph = observedGraph.current!;
        const store = (graph as unknown as { store: { alpha: number } }).store;
        const read = (): NativePinReleaseFrame => {
          const frame = graph.getGpuSimulationFrame(), positions = graph.getPointPositions();
          if (!frame || frame.nodeCount !== columns.nodeIds.length) throw new Error("Released-pin diagnostic lost its native scene");
          assertPcNativePositions(positions, columns.nodeIds.length);
          const scale = 4096 / graph.config.spaceSize;
          return { alpha: store.alpha, running: graph.isSimulationRunning, nativeTicks,
            canonicalPosition: [positions[0] * scale, positions[1] * scale], pinned: [...frame.pinnedIndices],
            nodeCount: frame.nodeCount, relationCount: uploadedRelationCount, topologyMatches: uploadedTopologyMatches,
            geometryUploads, topologyUploads, forceCommits, collisionCommits, glError: frame.gl.getError() };
        };
        const id = columns.nodeIds[0];
        adapter.focus(id); // Retire automatic camera work, not forces or user pause.
        adapter.resumeLayout();
        const beforePin = read();
        // One explicit public pin moves this original document, not the scene
        // seed or force model. The opposite quadrant provides a real edit.
        const target: [number, number] = beforePin.canonicalPosition.map(value => value < 2048 ? 3400 : 700) as [number, number];
        adapter.pin(id, true, { x: target[0], y: target[1] });
        const afterPin = read();
        const priorEnd = graph.config.onSimulationEnd;
        let endEvents = 0;
        graph.config.onSimulationEnd = () => { endEvents++; priorEnd?.(); };
        try {
          const beforeTicks = nativeTicks;
          graph.start(.0010005);
          graph.step(); // Same actual upstream threshold crossing as nativeCooling.
          const afterStep = read(), manualTicks = nativeTicks - beforeTicks;
          await nativeFrame(); await nativeFrame();
          const afterEnd = read();
          if (action === "reset") adapter.reset(); else adapter.pin(id, false);
          const afterRelease = read();
          // Fixed actual-tick checkpoint, not retries until movement happens.
          while (nativeTicks < afterRelease.nativeTicks + 4) await nativeFrame();
          const afterMovement = read();
          adapter.stopLayout();
          const paused = read();
          await nativeFrame(); await nativeFrame();
          const frozen = read();
          return { action, id, target, beforePin, afterPin, afterStep, afterEnd, afterRelease,
            afterMovement, paused, frozen, manualTicks, endEvents };
        } finally { graph.config.onSimulationEnd = priorEnd; }
      },
      geometry: () => geometryFromPositions(observedGraph.current!.getPointPositions()),
      point,
      dragTarget: () => {
        // Diagnose the current native scene only: do not pin/reposition a dot
        // or substitute a CPU seed to manufacture a successful drag target.
        const graph = observedGraph.current!;
        const positions = graph.getPointPositions();
        const screens = columns.nodeIds.map((_id, index) => graph.spaceToScreenPosition([
          positions[index * 2], positions[index * 2 + 1],
        ]));
        const pins = new Set(graph.getGpuSimulationFrame()?.pinnedIndices ?? []);
        let best: { index: number; id: string; screen: [number, number]; clearance: number } | null = null;
        screens.forEach((screen, index) => {
          if (pins.has(index) || screen[0] < 48 || screen[1] < 48
            || screen[0] > stage.clientWidth - 96 || screen[1] > stage.clientHeight - 72) return;
          let clearance = Infinity;
          screens.forEach((other, otherIndex) => {
            if (otherIndex !== index) clearance = Math.min(clearance, Math.hypot(screen[0] - other[0], screen[1] - other[1]));
          });
          if (!best || clearance > best.clearance) best = { index, id: columns.nodeIds[index], screen, clearance };
        });
        if (!best) throw new Error("No unpinned native point lies inside the pointer-drag viewport");
        return best;
      },
      isolate: () => {
        const target = observedGraph.current!.screenToSpacePosition([48, 48]);
        const canonicalScale = 4096 / observedGraph.current!.config.spaceSize;
        adapter.pin("node-0", true, { x: target[0] * canonicalScale, y: target[1] * canonicalScale });
        return point(0);
      },
      pinAtScreen: (index, screen) => {
        if (!boundaryLabelFixture || !Number.isInteger(index) || index < 0 || index >= count
          || !screen.every(Number.isFinite)) throw new Error("Invalid boundary-label pin request");
        // An explicit fixture user pin isolates label placement only. It is
        // never a seed, force, density or target-device performance oracle.
        const graph = observedGraph.current!;
        const target = graph.screenToSpacePosition(screen);
        const canonicalScale = 4096 / graph.config.spaceSize;
        adapter.pin(columns.nodeIds[index], true, { x: target[0] * canonicalScale, y: target[1] * canonicalScale });
      },
      focus: (id) => adapter.focus(id),
      fit: () => adapter.fit(),
      zoom: (factor) => adapter.zoomBy(factor),
      // Force the documented stopped/alpha=0 state in this native fixture.
      stop: () => { adapter.stopLayout(); observedGraph.current!.stop(); },
      pause: () => adapter.stopLayout(),
      resume: () => adapter.resumeLayout(),
      reduce: (enabled) => adapter.setReducedMotion(enabled),
      resize: () => adapter.resize(),
      destroy: () => adapter.destroy(),
    };
  }
  window.addEventListener("pagehide", () => adapter.destroy(), { once: true });
}

void main().catch((error) => {
  window.__workspaceMapSmoke = { status: "error", error: String(error?.stack ?? error) };
});

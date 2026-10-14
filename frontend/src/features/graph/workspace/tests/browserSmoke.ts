import { NativeGraph as Graph } from "../cosmosNative";

import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import { workspaceMapCommunityIdentityKey } from "../../map/communityJob";
import type { GraphViewport } from "../../shared/rendererTypes";
import { createWorkspaceCosmosAdapter } from "../cosmosAdapter";
import { detectWorkspaceMapWebGL } from "../webglSupport";
import type { WorkspaceForceAtlas2Units } from "../forceAtlas2Units";
import { WORKSPACE_MAP_PC_10K_BUDGET } from "../../shared/workspaceMapBudget";

type InitialCameraObservation = {
  fits: Array<{ at: number; points: number; before: GraphViewport | null }>;
  contactCanonicalFits: Array<{ at: number; points: number }>;
  viewportEvents: number;
  cancellation: { before: GraphViewport | null; after: GraphViewport | null;
    positionsBefore: number[]; positionsAfter: number[]; echoes: number } | null;
};

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
      point(index: number): [number, number];
      dragTarget(): { index: number; id: string; screen: [number, number]; clearance: number };
      isolate(): [number, number];
      pinAtScreen(index: number, screen: [number, number]): void;
      focus(id: string | null): void;
      fit(): void;
      zoom(factor: number): void;
      stop(): void;
      resume(): void;
      reduce(enabled: boolean): void;
      resize(): void;
    };
  }
}

const query = new URLSearchParams(window.location.search);
const smallScene = query.get("geometry") === "40";
const mediumScene = query.get("geometry") === "2000";
const linksOnly = query.get("topology") === "links";
const reducedMotion = query.get("motion") === "reduce";
const lightTheme = query.get("theme") === "light";
const count = smallScene ? 40 : mediumScene ? 2_000 : 20_000;
const edgeCount = smallScene ? 40 : mediumScene ? 4_700 : 47_000;
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
const observedGraph: { current: Graph | null } = { current: null };
let geometryUploads = 0;
let topologyUploads = 0;
let workerRequests = 0;
let collisionCommits = 0;
let forceCommits = 0;
let fallbackPositions: Float32Array | null = null;
let workerPositions: Float32Array | null = null;
let pointSizes: Float32Array | null = null;
let calibrationViewport: { width: number; height: number } | null = null;
const initialCamera: InitialCameraObservation = { fits: [], contactCanonicalFits: [], viewportEvents: 0, cancellation: null };
let observeAutomaticFit: (() => void) | null = null;
if (searchFixture || interactionFixture) {
  // Fixture-only observation of actual Cosmos GPU geometry, not a production hook.
  const nativeRender = Graph.prototype.render;
  Graph.prototype.render = function (...args) {
    observedGraph.current = this;
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
    return nativeLinks.apply(this, args);
  };
  const nativeSizes = Graph.prototype.setPointSizes;
  Graph.prototype.setPointSizes = function (...args) {
    pointSizes = args[0].slice();
    return nativeSizes.apply(this, args);
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
    ...(pcNativeCameraDiagnostic ? { budget: WORKSPACE_MAP_PC_10K_BUDGET } : {}),
    callbacks: {
      onHover(id) { if (window.__workspaceMapSmoke) window.__workspaceMapSmoke.hoveredId = id; },
      onRuntimeError(error) { if (window.__workspaceMapSmoke) window.__workspaceMapSmoke.runtimeError = String(error); },
      onSelect(id) { selectedId = id; selections.push(id); },
      onPin(id, position) { dragPins.push({ id, position: [position.x, position.y] }); },
      onViewport() { initialCamera.viewportEvents += 1; },
    },
  });
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
    window.__workspaceMapInteraction = {
      snapshot: () => {
        const coordinates = adapter.captureCoordinates();
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
      resume: () => adapter.resumeLayout(),
      reduce: (enabled) => adapter.setReducedMotion(enabled),
      resize: () => adapter.resize(),
    };
  }
  window.addEventListener("pagehide", () => adapter.destroy(), { once: true });
}

void main().catch((error) => {
  window.__workspaceMapSmoke = { status: "error", error: String(error?.stack ?? error) };
});

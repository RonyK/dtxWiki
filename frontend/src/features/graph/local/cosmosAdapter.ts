import {
  createCosmosRenderer, type CosmosCoordinateSnapshot, type CosmosRenderer,
} from "../shared/cosmos/cosmosCore";
import { buildLocalCosmosColumns } from "../shared/cosmos/localScene";
import { readGraphTokens } from "../shared/graphTokens";
import type { GraphPoint, GraphRenderScene, GraphViewport, GraphVisibleIdentity } from "../shared/rendererTypes";
import { createLocalCosmosBinding } from "./cosmosBinding";
import { createLocalCosmosLayoutSolver } from "./cosmosLayout";
import { createLocateTrace } from "./locateTrace";
import { buildLocalScene } from "./sceneInput";

export type LocalGraphAdapterOptions = {
  callbacks: {
    onHover(id: string | null): void;
    onSelect(id: string | null): void;
    onPin(id: string, position: GraphPoint): void;
    onViewport(viewport: GraphViewport): void;
    onContextStatus?(status: "lost" | "restored"): void;
  };
  hoverChip?: HTMLDivElement | null;
  forcedColors: boolean;
  initialViewport?: GraphViewport | null;
  initialCoordinates?: CosmosCoordinateSnapshot | null;
  initialPaused?: boolean;
  /** Stable only for this authority-fenced GraphCanvas owner's lifetime. */
  ownerKey?: string;
  positions: Record<string, GraphPoint>;
  reducedMotion: boolean;
  viewportInsets?: () => { top: number; right: number; bottom: number; left: number };
  showLabels?: boolean;
};

export type LocalSceneUpdate = "metadata" | "topology" | "rebuild";

/** Surface contract independent of the renderer and its coordinate transport. */
export interface LocalGraphAdapter {
  captureCoordinates(): CosmosCoordinateSnapshot | null;
  centerOn(id: string): void;
  destroy(): void;
  fit(): void;
  focus(id: string | null): void;
  locate(id: string): void;
  pin(id: string, pinned: boolean, position?: GraphPoint): GraphPoint | null;
  replaceScene(scene: GraphRenderScene): LocalSceneUpdate | void;
  reset(): void;
  resize(): void;
  resumeLayout(): void;
  setLabelsVisible(enabled: boolean): void;
  select(id: string | null): void;
  setViewport(viewport: GraphViewport): void;
  stopLayout(): void;
  whenSettled(): Promise<void>;
  zoomBy(factor: number): void;
}

let nextOwner = 0;
const finitePoint = (point: GraphPoint | undefined): point is GraphPoint => Boolean(point
  && Number.isFinite(Math.fround(point.x)) && Number.isFinite(Math.fround(point.y)));

/**
 * Context owns query/timeline state; this binding owns only its live renderer.
 * The Context owner remounts on authority changes. The additional per-owner
 * identity prevents even an accidental remount snapshot crossing that fence.
 */
export function createLocalGraphAdapter(
  container: HTMLDivElement,
  initialScene: GraphRenderScene,
  options: LocalGraphAdapterOptions,
): LocalGraphAdapter {
  const owner = options.ownerKey ?? `local-owner-${++nextOwner}`;
  let scene = initialScene;
  let local = buildLocalScene(scene);
  let scope = scene.scopeKey ?? null;
  let destroyed = false;
  let lost = false;
  let ready = false;
  let revision = 0;
  // An ambiguous scene never shares renderer state: each gets its own selection.
  let ambiguousScene = 0;
  let navigation = 0;
  let tracingId: string | null = null;
  let renderer: CosmosRenderer | undefined;
  const positions = new Map(Object.entries(options.positions).filter(([, point]) => finitePoint(point))
    .map(([id, point]) => [id, { ...point }]));
  const pendingPins = new Set<string>();
  const requestedArrivals = new Set<string>();
  const binding = createLocalCosmosBinding(createLocalCosmosLayoutSolver(), container);
  const identity = (): GraphVisibleIdentity => ({
    workspaceId: owner, actorId: owner, surface: "local",
    selectionKey: local.geometryKey === null ? JSON.stringify(["ambiguous", scope, ambiguousScene])
      : scope ?? local.geometryKey,
    visibleFingerprint: local.geometryKey ?? JSON.stringify([
      local.nodes.map(node => [node.key, node.graphId]),
      local.edges.map(edge => [edge.graphId, edge.sourceKey, edge.targetKey]),
    ]),
  });
  const markCanvas = () => {
    const canvas = container.querySelector<HTMLCanvasElement>("canvas:not([data-local-graph-labels]):not([data-local-graph-rings])");
    if (canvas) { canvas.classList.add("local-graph-canvas"); canvas.dataset.localGraphCanvas = ""; }
  };
  const trace = createLocateTrace(container, () => {
    tracingId = null;
    binding.setTracing(null);
  });
  const cancelTrace = () => { navigation++; trace.cancel(); tracingId = null; binding.setTracing(null); };
  // Explicit reader input supersedes queued navigation. An already-playing
  // locate trace continues to follow the node through camera movement.
  const cancelNavigation = () => { navigation++; };
  const renderTrace = () => {
    markCanvas();
    if (!trace.playing || !tracingId || !renderer) return;
    const point = renderer.getNodeDisplayData(tracingId);
    const mode = options.forcedColors ? "forced"
      : document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    trace.place(point ? { x: point.x, y: point.y, radius: point.radius * .88,
      width: point.radius * .24, color: readGraphTokens(container, mode).ink } : null);
  };
  const publishPin = (id: string, point: GraphPoint) => {
    if (destroyed || lost || !finitePoint(point)) return;
    pendingPins.delete(id);
    requestedArrivals.delete(id);
    positions.set(id, { ...point });
    options.callbacks.onPin(id, { ...point });
  };
  const requestArrival = (id: string) => {
    if (!renderer || requestedArrivals.has(id) || !local.keyByGraphId.has(id)) return;
    requestedArrivals.add(id);
    renderer.pinWhenSettled(id);
  };
  const reconcilePins = (restore?: ReadonlySet<string>) => {
    if (!renderer || destroyed || lost) return;
    for (const [id, point] of positions) {
      if (restore?.has(id) && local.keyByGraphId.has(id)) renderer.pin(id, true, point, { reconcile: true });
    }
    for (const id of pendingPins) {
      requestArrival(id);
    }
  };
  try {
    renderer = createCosmosRenderer(container, buildLocalCosmosColumns(scene, identity(), local), {
    forcedColors: options.forcedColors, reducedMotion: options.reducedMotion,
    initialPaused: options.initialPaused, initialViewport: options.initialViewport,
    initialCoordinates: options.initialCoordinates,
    hoverChip: options.hoverChip, showLabels: options.showLabels,
    getSafeInsets: options.viewportInsets,
    callbacks: {
      onHover: id => { if (!destroyed && !lost) options.callbacks.onHover(id); },
      onSelect: id => { if (!destroyed && !lost) options.callbacks.onSelect(id); },
      onPin: publishPin,
      onViewport: viewport => { if (!destroyed && !lost) options.callbacks.onViewport(viewport); },
      onContextStatus: status => {
        if (destroyed) return;
        if (status === "lost") { lost = true; cancelTrace(); }
        options.callbacks.onContextStatus?.(status);
      },
      onRuntimeError: () => {
        if (destroyed || lost) return;
        lost = true; cancelTrace(); options.callbacks.onContextStatus?.("lost");
      },
      onRender: renderTrace,
    },
    }, binding);
  } catch (error) {
    trace.destroy(); binding.destroyOverlay?.(); throw error;
  }
  const hoverChip = options.hoverChip ?? container.querySelector<HTMLDivElement>(".graph-hover-chip");
  const previousChipLayer = hoverChip?.style.zIndex ?? "";
  if (hoverChip) hoverChip.style.zIndex = "4";
  container.addEventListener("pointerdown", cancelNavigation, true);
  container.addEventListener("wheel", cancelNavigation, { capture: true, passive: true });
  container.addEventListener("touchstart", cancelNavigation, { capture: true, passive: true });
  reconcilePins(new Set(positions.keys()));

  const watchReady = () => {
    const current = revision;
    const completion = renderer!.whenSettled().then(() => {
      if (destroyed || lost || current !== revision) return;
      ready = true;
      markCanvas();
      reconcilePins();
    });
    // GraphCanvas also observes whenSettled and owns the visible error. This
    // internal completion merely reconciles pins; never leave a rejection idle.
    void completion.catch(() => undefined);
    return completion;
  };
  let completion = watchReady();
  const active = () => !destroyed && !lost;
  const navigate = (id: string, withTrace: boolean) => {
    if (!active()) return;
    cancelTrace();
    const request = navigation, current = revision;
    void renderer!.whenSettled().then(() => {
      if (!active() || request !== navigation || current !== revision) return;
      renderer!.centerOn(id);
      if (!withTrace || options.reducedMotion || !local.nodes.some(node => node.graphId === id && node.isCurrent)) return;
      tracingId = id; binding.setTracing(id); trace.play(); renderTrace();
    }).catch(() => undefined);
  };
  return {
    captureCoordinates: () => active() ? renderer!.captureCoordinates() : null,
    centerOn(id) { navigate(id, false); },
    destroy() {
      if (destroyed) return;
      destroyed = true; revision++; navigation++; pendingPins.clear(); requestedArrivals.clear(); positions.clear(); trace.destroy(); renderer!.destroy();
      container.removeEventListener("pointerdown", cancelNavigation, true);
      container.removeEventListener("wheel", cancelNavigation, true);
      container.removeEventListener("touchstart", cancelNavigation, true);
      if (hoverChip) hoverChip.style.zIndex = previousChipLayer;
    },
    fit() { if (active()) { cancelTrace(); renderer!.fit(); } },
    focus(id) { if (active()) renderer!.focus(id); },
    locate(id) { navigate(id, true); },
    pin(id, pinned, position) {
      if (!active()) return null;
      if (!pinned) {
        pendingPins.delete(id); requestedArrivals.delete(id); positions.delete(id);
        const point = renderer!.getNodePosition(id);
        renderer!.pin(id, false);
        return point;
      }
      if (position !== undefined && !finitePoint(position)) return null;
      if (pendingPins.has(id)) return null;
      if (!local.keyByGraphId.has(id)) {
        if (finitePoint(position)) positions.set(id, { ...position });
        else if (!positions.has(id)) pendingPins.add(id);
        return null;
      }
      if (!ready && !position && !positions.has(id)) {
        pendingPins.add(id); requestArrival(id); return null;
      }
      const chosen = position === undefined && !positions.has(id);
      const point = renderer!.pin(id, true, position ?? positions.get(id));
      if (point) { positions.set(id, { ...point }); if (chosen) options.callbacks.onPin(id, { ...point }); }
      return point;
    },
    replaceScene(nextScene) {
      if (!active()) return;
      const previousVisible = new Set(local.keyByGraphId.keys());
      scene = nextScene; local = buildLocalScene(nextScene); scope = nextScene.scopeKey ?? null;
      if (local.geometryKey === null) ambiguousScene++;
      // The renderer alone decides, as for the Map, whether this is metadata,
      // a live topology update or a rebuild (also before its GPU or Worker is
      // ready), and it owns the camera. Everything below follows its answer.
      const update: LocalSceneUpdate | void = renderer!.replaceColumns(buildLocalCosmosColumns(scene, identity(), local));
      if (!update) return;
      if (update === "metadata") { renderTrace(); return update; }
      cancelTrace(); ready = false; revision++;
      if (update === "rebuild") requestedArrivals.clear();
      else for (const id of requestedArrivals) if (!local.keyByGraphId.has(id)) requestedArrivals.delete(id);
      // Retained pins belong to the live Worker, including a drag currently
      // in progress. Replaying their last external coordinates would rewind it.
      reconcilePins(new Set([...positions.keys()].filter(id => update === "rebuild" || !previousVisible.has(id))));
      completion = watchReady();
      return update;
    },
    reset() {
      if (!active()) return;
      cancelTrace(); pendingPins.clear(); requestedArrivals.clear(); positions.clear(); renderer!.reset();
    },
    resize() { if (active()) { renderer!.resize(); renderTrace(); } },
    resumeLayout() { if (active()) renderer!.resumeLayout(); },
    setLabelsVisible(enabled) { if (active()) renderer!.setLabelsVisible(enabled); },
    select(id) { if (active()) renderer!.select(id); },
    setViewport(viewport) { if (active()) { cancelNavigation(); renderer!.setViewport(viewport); } },
    stopLayout() { if (active()) renderer!.stopLayout(); },
    async whenSettled() {
      let observed: Promise<void>;
      do { observed = completion; await observed; } while (active() && observed !== completion);
    },
    zoomBy(factor) { if (active()) { cancelNavigation(); renderer!.zoomBy(factor); } },
  };
}

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import type { GraphCanvasHandle, RendererStatus } from "../GraphCanvas";
import type { WorkspaceMapColumns } from "../map/sceneColumns";
import { sameGraphVisibleIdentity, type GraphPoint, type GraphViewport, type GraphVisibleIdentity } from "../shared/rendererTypes";
import { readGraphTokens } from "../shared/graphTokens";
import { sameWorkspaceMapIndexedGeometry } from "../shared/workspaceMapGeometry";
import { assertWorkspaceMapWithinBudget, isWorkspaceMapWithinBudget, WORKSPACE_MAP_DEFAULT_BUDGET, type WorkspaceMapBudget } from "../shared/workspaceMapBudget";
import type { WorkspaceCosmosAdapter, WorkspaceMapCoordinateSnapshot, WorkspaceMapLayoutStatus } from "./cosmosAdapter";
import { detectWorkspaceMapWebGL } from "./webglSupport";

export type WorkspaceMapCanvasProps = {
  active: boolean;
  /** Stable device/explicit-scale policy supplied by the owning Screen. */
  budget?: WorkspaceMapBudget;
  columns: WorkspaceMapColumns;
  focusedId: string | null;
  forcedColors: boolean;
  /** User pause survives native theme/context/fullscreen renderer remounts. */
  layoutPaused?: boolean;
  /** A canonical Map link is explicit navigation, unlike ordinary Back. */
  navigationTargetId?: string | null;
  navigationIsCurrent?(): boolean;
  onNavigationRevealFinished?(): void;
  onHover(id: string | null): void;
  onPin(id: string, position: GraphPoint): void;
  onRendererStatusChange(status: RendererStatus): void;
  onSelect(id: string | null): void;
  onViewport?(viewport: GraphViewport): void;
  pinnedIds: string[];
  pinnedPositions: Record<string, GraphPoint>;
  reducedMotion: boolean;
  /** null means no search; an empty set means no current-scene matches. */
  searchMatchedIds?: ReadonlySet<string> | null;
  selectedId: string | null;
  showEdges?: boolean;
  showLabels?: boolean;
  viewport?: GraphViewport | null;
};

type MapFailure = "webgl2" | "float-render-target" | "context-lost" | "runtime" | null;

function finitePoint(point: GraphPoint | undefined): GraphPoint | null {
  return point && Number.isFinite(point.x) && Number.isFinite(point.y)
    ? { x: point.x, y: point.y } : null;
}

function samePoint(left: GraphPoint | null, right: GraphPoint | null): boolean {
  return left === right || Boolean(left && right && left.x === right.x && left.y === right.y);
}

function preservesAppliedPins(previous: WorkspaceMapColumns | null, next: WorkspaceMapColumns): boolean {
  return previous !== null && sameGraphVisibleIdentity(previous.identity, next.identity)
    && sameWorkspaceMapIndexedGeometry(previous, next);
}

/**
 * The Map's render lifecycle is deliberately separate from Local Graph's
 * Sigma lifecycle. Probe first, then import the much larger Cosmos engine.
 * The caller's document list stays mounted when WebGL is unavailable.
 */
export const WorkspaceMapCanvas = forwardRef<GraphCanvasHandle, WorkspaceMapCanvasProps>(
  function WorkspaceMapCanvas(
    {
      active,
      budget = WORKSPACE_MAP_DEFAULT_BUDGET,
      columns,
      focusedId,
      forcedColors,
      layoutPaused = false,
      navigationTargetId = null,
      navigationIsCurrent,
      onNavigationRevealFinished,
      onHover,
      onPin,
      onRendererStatusChange,
      onSelect,
      onViewport,
      pinnedIds,
      pinnedPositions,
      reducedMotion,
      searchMatchedIds = null,
      selectedId,
      showEdges = true,
      showLabels = true,
      viewport,
    },
    ref,
  ) {
    const { t } = useTranslation("graph");
    const containerRef = useRef<HTMLDivElement | null>(null);
    const adapterRef = useRef<WorkspaceCosmosAdapter | null>(null);
    const columnsRef = useRef(columns);
    const budgetRef = useRef(budget);
    const pinnedIdsRef = useRef(pinnedIds);
    const pinnedPositionsRef = useRef(pinnedPositions);
    const selectedIdRef = useRef(selectedId);
    const focusedIdRef = useRef(focusedId);
    const searchMatchedIdsRef = useRef(searchMatchedIds);
    const activeRef = useRef(active);
    const layoutPausedRef = useRef(layoutPaused);
    const reducedMotionRef = useRef(reducedMotion);
    const showEdgesRef = useRef(showEdges);
    const showLabelsRef = useRef(showLabels);
    const viewportRef = useRef(viewport);
    const callbacksRef = useRef({ navigationIsCurrent, onNavigationRevealFinished, onHover, onPin, onRendererStatusChange, onSelect, onViewport });
    const navigationRequestRef = useRef<{ id: string; controller: AbortController; submitted: boolean;
      finished: boolean; onFinished?: () => void } | null>(null);
    const appliedColumnsRef = useRef<WorkspaceMapColumns | null>(null);
    const pendingColumnsRef = useRef<WorkspaceMapColumns | null>(null);
    const sessionCoordinatesRef = useRef<WorkspaceMapCoordinateSnapshot | null>(null);
    const appliedPinsRef = useRef<Map<string, GraphPoint | null>>(new Map());
    const appliedPauseRef = useRef<boolean | null>(null);
    const nativeReadyRef = useRef(false);
    const generationRef = useRef(0);
    const [status, setStatus] = useState<RendererStatus>("loading");
    const [layoutStatus, setLayoutStatus] = useState<{
      status: WorkspaceMapLayoutStatus | null; identity: GraphVisibleIdentity;
    } | null>(null);
    const [failure, setFailure] = useState<MapFailure>(null);
    const [rendererRevision, setRendererRevision] = useState(0);
    const [themeRevision, setThemeRevision] = useState(0);
    const hasNodes = columns.nodeIds.length > 0;
    const budgetDocuments = budget.documents;
    const budgetRelations = budget.relations;

    columnsRef.current = columns;
    budgetRef.current = budget;
    pinnedIdsRef.current = pinnedIds;
    pinnedPositionsRef.current = pinnedPositions;
    selectedIdRef.current = selectedId;
    focusedIdRef.current = focusedId;
    searchMatchedIdsRef.current = searchMatchedIds;
    activeRef.current = active;
    layoutPausedRef.current = layoutPaused;
    reducedMotionRef.current = reducedMotion;
    showEdgesRef.current = showEdges;
    showLabelsRef.current = showLabels;
    viewportRef.current = viewport;
    callbacksRef.current = { navigationIsCurrent, onNavigationRevealFinished, onHover, onPin, onRendererStatusChange, onSelect, onViewport };

    const finishNavigationReveal = useCallback((request: NonNullable<typeof navigationRequestRef.current>) => {
      if (request.finished) return;
      request.finished = true;
      request.onFinished?.();
    }, []);
    const cancelNavigationReveal = useCallback(() => {
      const request = navigationRequestRef.current;
      if (!request) return;
      request.controller.abort();
      finishNavigationReveal(request);
    }, [finishNavigationReveal]);
    const submitNavigationReveal = useCallback((adapter: WorkspaceCosmosAdapter) => {
      const request = navigationRequestRef.current;
      if (!request || request.submitted || request.controller.signal.aborted
        || !nativeReadyRef.current || !activeRef.current) return;
      request.submitted = true;
      adapter.revealAfterLayout(request.id, request.controller.signal, () =>
        adapterRef.current === adapter && nativeReadyRef.current && activeRef.current
        && navigationRequestRef.current === request && selectedIdRef.current === request.id
        && (callbacksRef.current.navigationIsCurrent?.() ?? true), () => finishNavigationReveal(request));
    }, [finishNavigationReveal]);

    const reconcilePins = (adapter: WorkspaceCosmosAdapter) => {
      if (!nativeReadyRef.current) return;
      const visible = new Set(columnsRef.current.nodeIds);
      const next = new Set(pinnedIdsRef.current.filter((id) => visible.has(id)));
      for (const id of appliedPinsRef.current.keys()) {
        if (!next.has(id)) adapter.pin(id, false);
      }
      const applied = new Map<string, GraphPoint | null>();
      for (const id of next) {
        const position = finitePoint(pinnedPositionsRef.current[id]);
        if (!appliedPinsRef.current.has(id)
          || !samePoint(appliedPinsRef.current.get(id) ?? null, position)) {
          adapter.pin(id, true, position ?? undefined);
        }
        applied.set(id, position);
      }
      appliedPinsRef.current = applied;
    };
    const reconcileLayoutPause = (adapter: WorkspaceCosmosAdapter) => {
      const shouldPause = layoutPausedRef.current || !activeRef.current;
      if (appliedPauseRef.current === shouldPause) return;
      if (shouldPause) adapter.stopLayout();
      else adapter.resumeLayout();
      appliedPauseRef.current = shouldPause;
    };
    const failRuntime = (adapter: WorkspaceCosmosAdapter) => {
      if (adapterRef.current !== adapter) return;
      adapter.destroy();
      adapterRef.current = null;
      setFailure("runtime");
      setStatus("error");
      callbacksRef.current.onRendererStatusChange("error");
    };

    useImperativeHandle(ref, () => ({
      centerOn: (id) => { cancelNavigationReveal(); adapterRef.current?.centerOn(id); },
      fit: () => { cancelNavigationReveal(); adapterRef.current?.fit(); },
      pauseLayout: () => {
        cancelNavigationReveal();
        adapterRef.current?.stopLayout();
        appliedPauseRef.current = true;
      },
      resumeLayout: () => {
        adapterRef.current?.resumeLayout();
        appliedPauseRef.current = false;
      },
      pin: (id, shouldPin) => {
        cancelNavigationReveal();
        const position = adapterRef.current?.pin(id, shouldPin) ?? null;
        if (nativeReadyRef.current) {
          if (shouldPin && position) appliedPinsRef.current.set(id, { ...position });
          else appliedPinsRef.current.delete(id);
        }
        return position;
      },
      reset: () => {
        cancelNavigationReveal();
        adapterRef.current?.reset();
        appliedPinsRef.current.clear();
      },
      setViewport: (nextViewport) => { cancelNavigationReveal(); adapterRef.current?.setViewport(nextViewport); },
      zoomIn: () => { cancelNavigationReveal(); adapterRef.current?.zoomBy(1.2); },
      zoomOut: () => { cancelNavigationReveal(); adapterRef.current?.zoomBy(1 / 1.2); },
    }), [cancelNavigationReveal]);

    useEffect(() => {
      const request = navigationTargetId
        ? { id: navigationTargetId, controller: new AbortController(), submitted: false,
          finished: false, onFinished: callbacksRef.current.onNavigationRevealFinished } : null;
      navigationRequestRef.current = request;
      const cancel = () => {
        if (!request) return;
        request.controller.abort();
        finishNavigationReveal(request);
      };
      const container = containerRef.current;
      // Own the intent before native ready, so a waiting user's interaction
      // cannot be overwritten by a newly armed reveal after asynchronous import.
      container?.addEventListener("pointerdown", cancel);
      container?.addEventListener("wheel", cancel, { passive: true });
      if (adapterRef.current) submitNavigationReveal(adapterRef.current);
      return () => {
        // StrictMode probes effects before asynchronous native construction.
        // Aborting that binding is cleanup, not consumed navigation intent.
        request?.controller.abort();
        if (request?.submitted) finishNavigationReveal(request);
        container?.removeEventListener("pointerdown", cancel);
        container?.removeEventListener("wheel", cancel);
      };
    }, [finishNavigationReveal, navigationTargetId, submitNavigationReveal]);

    useEffect(() => {
      const container = containerRef.current;
      const generation = ++generationRef.current;
      const rendererBudget = { documents: budgetDocuments, relations: budgetRelations };
      nativeReadyRef.current = false;
      setLayoutStatus(null);
      let cancelled = false;
      let contextLost = false;
      let runtimeFailed = false;
      let createdAdapter: WorkspaceCosmosAdapter | null = null;
      const acceptsInteraction = () => !cancelled && !contextLost && !runtimeFailed
        && generation === generationRef.current;
      const publishStatus = (next: RendererStatus) => {
        if (cancelled || generation !== generationRef.current) return;
        setStatus(next);
        callbacksRef.current.onRendererStatusChange(next);
      };
      const publishFailure = (reason: MapFailure) => {
        if (cancelled || generation !== generationRef.current) return;
        setFailure(reason);
        sessionCoordinatesRef.current = null;
        publishStatus("error");
      };

      try {
        assertWorkspaceMapWithinBudget({ nodeCount: columnsRef.current.nodeIds.length,
          edgeCount: columnsRef.current.edgeIds.length }, rendererBudget);
      } catch {
        publishFailure("runtime");
        return;
      }
      if (!container || !columnsRef.current.nodeIds.length) {
        publishStatus("ready");
        return;
      }
      setFailure(null);
      publishStatus("loading");
      let support: ReturnType<typeof detectWorkspaceMapWebGL>;
      try {
        support = detectWorkspaceMapWebGL();
      } catch {
        publishFailure("runtime");
        return;
      }
      if (!support.supported) {
        publishFailure(support.reason);
        return;
      }

      void import("./cosmosAdapter")
        .then(async ({ createWorkspaceCosmosAdapter }) => {
          if (cancelled || generation !== generationRef.current) return;
          const initialPaused = layoutPausedRef.current || !activeRef.current;
          const snapshot = sessionCoordinatesRef.current;
          sessionCoordinatesRef.current = null;
          const snapshotBudget = snapshot?.budget ?? WORKSPACE_MAP_DEFAULT_BUDGET;
          const preservesSnapshot = snapshot && snapshotBudget.documents === rendererBudget.documents
            && snapshotBudget.relations === rendererBudget.relations
            && isWorkspaceMapWithinBudget({ nodeCount: snapshot.columns.nodeIds.length,
              edgeCount: snapshot.columns.edgeIds.length }, rendererBudget)
            && preservesAppliedPins(snapshot.columns, columnsRef.current);
          const adapter = createWorkspaceCosmosAdapter(container, columnsRef.current, {
            budget: rendererBudget,
            forcedColors,
            initialPaused,
            initialCoordinates: preservesSnapshot ? snapshot : null,
            initialViewport: viewportRef.current,
            reducedMotion: reducedMotionRef.current,
            showEdges: showEdgesRef.current,
            showLabels: showLabelsRef.current,
            callbacks: {
              onLayoutStatusChange: (next, identity) => {
                if (!acceptsInteraction() || !sameGraphVisibleIdentity(identity, columnsRef.current.identity)) return;
                setLayoutStatus((previous) => previous?.status === next
                  && sameGraphVisibleIdentity(previous.identity, identity) ? previous
                  : { status: next, identity: { ...identity } });
              },
              onHover: (id) => {
                if (acceptsInteraction()) callbacksRef.current.onHover(id);
              },
              onPin: (id, position) => {
                if (!acceptsInteraction()) return;
                // Cosmos has already pinned the dragged point before it emits
                // this callback. The React prop echo must not upload it again.
                if (nativeReadyRef.current) appliedPinsRef.current.set(id, { ...position });
                callbacksRef.current.onPin(id, position);
              },
              onSelect: (id) => {
                if (!acceptsInteraction()) return;
                cancelNavigationReveal();
                callbacksRef.current.onSelect(id);
              },
              onViewport: (next) => {
                if (acceptsInteraction()) callbacksRef.current.onViewport?.(next);
              },
              onContextStatus: (next) => {
                if (cancelled || generation !== generationRef.current) return;
                if (next === "lost") {
                  contextLost = true;
                  publishFailure("context-lost");
                } else {
                  // GPU textures and framebuffers from the lost context are
                  // stale. Destroy and create a fresh engine on restoration.
                  setRendererRevision((revision) => revision + 1);
                }
              },
              onRuntimeError: () => {
                if (cancelled || runtimeFailed || generation !== generationRef.current) return;
                runtimeFailed = true;
                nativeReadyRef.current = false;
                sessionCoordinatesRef.current = null;
                cancelNavigationReveal();
                publishFailure("runtime");
                const failedAdapter = createdAdapter;
                if (adapterRef.current === failedAdapter) adapterRef.current = null;
                // Let the current native frame finish restoring its borrowed
                // resources before retiring the renderer. Cleanup is idempotent.
                queueMicrotask(() => failedAdapter?.destroy());
              },
            },
          });
          createdAdapter = adapter;
          if (cancelled || generation !== generationRef.current) {
            adapter.destroy();
            return;
          }
          adapterRef.current = adapter;
          appliedColumnsRef.current = columnsRef.current;
          pendingColumnsRef.current = null;
          appliedPinsRef.current = new Map();
          appliedPauseRef.current = initialPaused;
          adapter.select(selectedIdRef.current);
          adapter.focus(focusedIdRef.current);
          adapter.setSearchMatches(searchMatchedIdsRef.current);
          await adapter.whenSettled();
          if (cancelled || contextLost || runtimeFailed || generation !== generationRef.current) return;
          // Cosmos cannot commit GPU pins before its async ready phase.
          nativeReadyRef.current = true;
          reconcileLayoutPause(adapter);
          reconcilePins(adapter);
          submitNavigationReveal(adapter);
          publishStatus("ready");
        })
        .catch(() => {
          if (cancelled || generation !== generationRef.current) return;
          createdAdapter?.destroy();
          if (adapterRef.current === createdAdapter) adapterRef.current = null;
          createdAdapter = null;
          publishFailure("runtime");
        });

      return () => {
        cancelled = true;
        const sameBudget = budgetRef.current.documents === rendererBudget.documents
          && budgetRef.current.relations === rendererBudget.relations;
        sessionCoordinatesRef.current = sameBudget && !contextLost && nativeReadyRef.current
          && adapterRef.current === createdAdapter ? createdAdapter?.captureCoordinates() ?? null : null;
        createdAdapter?.destroy();
        // Submitted navigation is one-shot, including renderer replacement.
        // An unsubmitted intent may still wait for the first healthy renderer.
        if (navigationRequestRef.current?.submitted) cancelNavigationReveal();
        if (adapterRef.current === createdAdapter) adapterRef.current = null;
        appliedColumnsRef.current = null;
        appliedPinsRef.current = new Map();
        appliedPauseRef.current = null;
        nativeReadyRef.current = false;
      };
    }, [budgetDocuments, budgetRelations, cancelNavigationReveal, forcedColors, hasNodes, rendererRevision, submitNavigationReveal, themeRevision]);

    useEffect(() => {
      if (typeof MutationObserver === "undefined") return undefined;
      const container = containerRef.current;
      if (!container) return undefined;
      const paletteKey = () => {
        const mode = forcedColors ? "forced"
          : document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
        return JSON.stringify([mode, readGraphTokens(container, mode)]);
      };
      let previousPalette = paletteKey();
      const observer = new MutationObserver(() => {
        const nextPalette = paletteKey();
        if (nextPalette === previousPalette) return;
        previousPalette = nextPalette;
        setThemeRevision((revision) => revision + 1);
      });
      const config: MutationObserverInit = {
        attributes: true,
        attributeFilter: ["class", "data-theme", "style"],
      };
      // The SDK writes attribution/error colors on html when a transparent
      // Folder background is installed; dialogs also change body scroll styles.
      // Only a changed effective graph palette warrants a GPU remount. Include
      // scoped ancestors so genuine inherited/scoped token changes still work.
      for (let element: Element | null = container; element; element = element.parentElement) {
        observer.observe(element, config);
      }
      return () => observer.disconnect();
    }, [forcedColors]);

    useEffect(() => {
      const adapter = adapterRef.current;
      if (!adapter) return;
      try {
        assertWorkspaceMapWithinBudget({ nodeCount: columns.nodeIds.length,
          edgeCount: columns.edgeIds.length }, budgetRef.current);
      } catch {
        pendingColumnsRef.current = null;
        sessionCoordinatesRef.current = null;
        failRuntime(adapter);
        return;
      }
      if (!activeRef.current) {
        if (appliedColumnsRef.current !== columns) pendingColumnsRef.current = columns;
        reconcileLayoutPause(adapter);
        return;
      }
      if (appliedColumnsRef.current === columns) return;
      try {
        const preservePins = preservesAppliedPins(appliedColumnsRef.current, columns);
        adapter.replaceColumns(columns);
        appliedColumnsRef.current = columns;
        pendingColumnsRef.current = null;
        // Metadata rebinding does not replace native coordinates or springs.
        // Keep the applied owners so unchanged React pin echoes stay inert;
        // reconcilePins still retires removed/renamed projection IDs safely.
        if (!preservePins) appliedPinsRef.current = new Map();
        reconcilePins(adapter);
        adapter.select(selectedIdRef.current);
        adapter.focus(focusedIdRef.current);
      } catch {
        failRuntime(adapter);
      }
    }, [columns]);

    useEffect(() => {
      const adapter = adapterRef.current;
      if (!adapter) return;
      if (!active) {
        reconcileLayoutPause(adapter);
        return;
      }
      const pending = pendingColumnsRef.current;
      try {
        if (pending && pending !== appliedColumnsRef.current) {
          const preservePins = preservesAppliedPins(appliedColumnsRef.current, pending);
          adapter.replaceColumns(pending);
          appliedColumnsRef.current = pending;
          pendingColumnsRef.current = null;
          if (!preservePins) appliedPinsRef.current = new Map();
          reconcilePins(adapter);
          adapter.select(selectedIdRef.current);
          adapter.focus(focusedIdRef.current);
        }
        reconcileLayoutPause(adapter);
        adapter.resize();
        submitNavigationReveal(adapter);
      } catch {
        failRuntime(adapter);
      }
    }, [active, submitNavigationReveal]);

    useEffect(() => {
      const adapter = adapterRef.current;
      if (layoutPaused) cancelNavigationReveal();
      if (adapter) reconcileLayoutPause(adapter);
    }, [cancelNavigationReveal, layoutPaused]);

    useEffect(() => {
      if (navigationRequestRef.current && navigationRequestRef.current.id !== selectedId) cancelNavigationReveal();
      adapterRef.current?.select(selectedId);
    }, [cancelNavigationReveal, selectedId]);
    useEffect(() => { adapterRef.current?.focus(focusedId); }, [focusedId]);
    useEffect(() => { adapterRef.current?.setSearchMatches(searchMatchedIds); }, [searchMatchedIds]);
    useEffect(() => { adapterRef.current?.setEdgesVisible(showEdges); }, [showEdges]);
    useEffect(() => { adapterRef.current?.setLabelsVisible(showLabels); }, [showLabels]);
    useEffect(() => { adapterRef.current?.setReducedMotion(reducedMotion); }, [reducedMotion]);
    useEffect(() => {
      if (active) {
        const adapter = adapterRef.current;
        if (adapter) reconcilePins(adapter);
      }
    }, [active, pinnedIds, pinnedPositions]);

    useEffect(() => {
      const container = containerRef.current;
      if (!container || typeof ResizeObserver === "undefined") return undefined;
      const observer = new ResizeObserver(() => {
        if (activeRef.current) adapterRef.current?.resize();
      });
      observer.observe(container);
      return () => observer.disconnect();
    }, []);

    const failureMessage = failure === "webgl2"
      ? t("workspaceGraph.mapWebgl2Unavailable", {
          defaultValue: "Graph View is unavailable because WebGL2 is unavailable in this browser. You can still open documents from the list below.",
        })
      : failure === "float-render-target"
        ? t("workspaceGraph.mapFloatUnavailable", {
            defaultValue: "Graph View is unavailable because required WebGL2 features are unavailable in this browser. You can still open documents from the list below.",
          })
        : failure === "context-lost"
          ? t("workspaceGraph.mapContextLost", {
              defaultValue: "The graphics context was lost. The document list remains available while the Map recovers.",
            })
          : t("workspaceGraph.mapRendererFailed", {
              defaultValue: "The interactive Map could not start. The document list below remains available.",
            });

    const visibleLayoutStatus = status === "ready" && active && hasNodes && layoutStatus
      && sameGraphVisibleIdentity(layoutStatus.identity, columns.identity)
      && layoutStatus.status !== "live" ? layoutStatus.status : null;

    return (
      <div className="graph-canvas-frame" data-renderer-status={status}>
        <div
          aria-hidden="true"
          className="graph-canvas"
          data-map-edge-count={columns.edgeIds.length}
          data-map-node-count={columns.nodeIds.length}
          data-testid="graph-canvas"
          ref={containerRef}
        />
        {status === "loading" ? (
          <p className="graph-canvas-status" role="status">
            {t("workspaceGraph.mapRendererLoading", { defaultValue: "Loading interactive map…" })}
          </p>
        ) : null}
        {visibleLayoutStatus ? (
          <p
            aria-live="polite"
            className="workspace-map-layout-status"
            data-layout-state={visibleLayoutStatus}
            data-testid="workspace-map-layout-status"
            role="status"
          >
            {t(`workspaceGraph.mapLayout.${visibleLayoutStatus}`)}
          </p>
        ) : null}
        {status === "error" ? (
          <div className="graph-canvas-status graph-canvas-error">
            <p role="status">{failureMessage}</p>
            <button
              className="icon-button text-button"
              onClick={() => setRendererRevision((revision) => revision + 1)}
              type="button"
            >
              {t("workspaceGraph.mapRendererRetry", { defaultValue: "Retry interactive map" })}
            </button>
          </div>
        ) : null}
      </div>
    );
  },
);

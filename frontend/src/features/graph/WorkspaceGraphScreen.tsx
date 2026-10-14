import {
  Download,
  Maximize2,
  Pause,
  Play,
  RotateCcw,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import type { TFunction } from "i18next";
import {
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError, apiErrorMessage } from "../../shared/api/client";
import {
  queryWorkspaceGraph,
  type WorkspaceGraphAvailableLens,
  type WorkspaceGraphDataFilters,
  type WorkspaceGraphEdge,
  type WorkspaceGraphNode,
  type WorkspaceGraphOverviewRequest,
  type WorkspaceGraphQuickFilter,
  type WorkspaceGraphScene,
} from "../../shared/api/workspaceGraph";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useAuth } from "../../shared/auth/AuthContext";
import { i18n } from "../../shared/i18n";
import { trapTabFocus } from "../../shared/lib/trapTabFocus";
import { clearWorkspaceGraphStorage, workspaceGraphStorageEpoch } from "../../shared/lib/workspaceGraphStorage";
import { useWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import type { GraphCanvasHandle } from "./GraphCanvas";
import { buildWorkspaceMapColumns } from "./map/sceneColumns";
import { prepareWorkspaceMapFolderScene, type WorkspaceMapPreparedFolderScene } from "./map/folderScene";
import { canAnimateWorkspaceRelationLayout } from "./shared/workspaceLayoutPolicy";
import { assertWorkspaceMapWithinBudget, isWorkspaceMapWithinBudget, readWorkspaceMapDeviceProfile,
  workspaceMapBudget, type WorkspaceMapScale } from "./shared/workspaceMapBudget";
import { WorkspaceMapCanvas } from "./workspace/WorkspaceMapCanvas";
import { WorkspaceGraphFolderFilters } from "./WorkspaceGraphFolderFilters";
import { visibleWorkspaceGraphDocumentCount, WorkspaceGraphToolbar } from "./workspace/WorkspaceGraphToolbar";
import type { GraphRenderScene, GraphViewport } from "./shared/rendererTypes";
import {
  DEFAULT_WORKSPACE_GRAPH_FILTERS,
  countPendingRefineFilters,
  clearWorkspaceGraphState,
  freshWorkspaceGraphState,
  estimateWorkspaceGraphImpact,
  workspaceGraphOrphanIds,
  loadWorkspaceGraphState,
  refineFiltersDiffer,
  saveWorkspaceGraphState,
  workspaceGraphRenderScene,
  workspaceGraphRequest,
  type WorkspaceGraphRestorableState,
} from "./workspaceGraphModel";

type WorkspaceGraphRefineTarget = "folder" | "time" | "overview";
type WorkspaceGraphDocumentNavigation = (event: ReactMouseEvent<HTMLAnchorElement>) => void;
type WorkspaceGraphModal =
  | { kind: "refine"; target: WorkspaceGraphRefineTarget }
  | { kind: "mobileCanvas" }
  | null;
type WorkspaceGraphAuthorityScope = {
  workspaceId: string;
  actorId: string | null;
  role: string | null;
};
type WorkspaceGraphListFocus = {
  id: string;
  scope: WorkspaceGraphAuthorityScope;
  request: WorkspaceGraphOverviewRequest;
  storageEpoch: number;
};
type WorkspaceGraphFolderSelection = {
  id: string;
  scope: WorkspaceGraphAuthorityScope;
  request: WorkspaceGraphOverviewRequest;
  visibleFingerprint: string;
};
type WorkspaceMapFolderContext = Pick<WorkspaceMapPreparedFolderScene,
  "points" | "folders" | "relations" | "folderBundles" | "geometryFingerprint">;

export function WorkspaceGraphScreen() {
  const { t } = useTranslation("graph");
  const { selectedId: workspaceId, selectedWorkspace } = useWorkspaceContext();
  const { user } = useAuth();
  const actorId = user?.user_id ?? null;
  const observedRole = selectedWorkspace?.id === workspaceId
    ? selectedWorkspace.role ?? null : null;
  // Identity, not just equal values: owner -> viewer -> owner must not make
  // callbacks from the first owner scene authoritative again.
  const authorityScope = useMemo<WorkspaceGraphAuthorityScope>(
    () => ({ workspaceId, actorId, role: observedRole }),
    [actorId, observedRole, workspaceId],
  );
  const [deviceProfile] = useState(readWorkspaceMapDeviceProfile);
  const [scaleOwner, setScaleOwner] = useState<{ scope: WorkspaceGraphAuthorityScope; scale: WorkspaceMapScale }>(() =>
    ({ scope: authorityScope, scale: "2k" }));
  const mapScale = scaleOwner.scope === authorityScope ? scaleOwner.scale : "2k";
  const mapBudget = useMemo(() => workspaceMapBudget(deviceProfile, mapScale), [deviceProfile, mapScale]);
  const [searchParams, setSearchParams] = useSearchParams();
  const focusedEntityFromUrl = searchParams.get("focus");
  const [requestedFocus, setRequestedFocus] = useState(() => focusedEntityFromUrl);
  const [consumedNavigationReveal, setConsumedNavigationReveal] = useState<{
    scope: WorkspaceGraphAuthorityScope; focus: string | null;
  } | null>(null);
  const navigationRevealConsumed = consumedNavigationReveal?.scope === authorityScope
    && consumedNavigationReveal.focus === requestedFocus;
  const [focusUrlClearPending, setFocusUrlClearPending] = useState(false);
  const canvasRef = useRef<GraphCanvasHandle | null>(null);
  const screenRef = useRef<HTMLDivElement | null>(null);
  const regionRef = useRef<HTMLElement | null>(null);
  const mobileCanvasButtonRef = useRef<HTMLButtonElement | null>(null);
  const mobileCanvasCloseRef = useRef<HTMLButtonElement | null>(null);
  const modalPanelRef = useRef<HTMLElement | null>(null);
  const modalOpenerRef = useRef<HTMLElement | null>(null);
  const refineCloseRef = useRef<HTMLButtonElement | null>(null);
  const refineFolderControlRef = useRef<HTMLInputElement | null>(null);
  const refineFolderSectionRef = useRef<HTMLFieldSetElement | null>(null);
  const refineTimeControlRef = useRef<HTMLSelectElement | null>(null);
  const refineTimeSectionRef = useRef<HTMLFieldSetElement | null>(null);
  const viewportFrameRef = useRef<number | null>(null);
  const pendingViewportRef = useRef<WorkspaceGraphRestorableState["viewport"]>(null);
  const expansionControllerRef = useRef<AbortController | null>(null);
  const previousScopeRef = useRef(authorityScope);
  const committedScopeRef = useRef(authorityScope);
  useLayoutEffect(() => {
    committedScopeRef.current = authorityScope;
  }, [authorityScope]);
  const authorityIsCurrent = useCallback((scope: WorkspaceGraphAuthorityScope, storageEpoch?: number) =>
    committedScopeRef.current === scope
      && (storageEpoch === undefined || workspaceGraphStorageEpoch() === storageEpoch), []);
  const [stateOwner, setStateOwner] = useState(authorityScope);
  const deniedRetryRef = useRef<{ scope: WorkspaceGraphAuthorityScope; retryRevision: number } | null>(null);
  const restorationRetryRef = useRef<{
    scope: WorkspaceGraphAuthorityScope;
    storageEpoch: number;
    retryRevision: number;
  } | null>(null);
  const [state, setState] = useState<WorkspaceGraphRestorableState>(() =>
    workspaceId && actorId ? loadWorkspaceGraphState(workspaceId, actorId) : freshWorkspaceGraphState(),
  );
  const selectedIdRef = useRef(state.selectedId);
  useEffect(() => { selectedIdRef.current = state.selectedId; }, [state.selectedId]);
  const pendingListCenterRef = useRef<string | null>(null);
  const [draftFilters, setDraftFilters] = useState<WorkspaceGraphDataFilters>(() =>
    cloneFilters(state.dataFilters),
  );
  const [receivedScene, setScene] = useState<WorkspaceGraphScene | null>(null);
  const [sceneOwner, setSceneOwner] = useState<{ scope: WorkspaceGraphAuthorityScope; storageEpoch: number;
    request: WorkspaceGraphOverviewRequest } | null>(null);
  // Keyboard emphasis is transient, not a saved selection or a camera action.
  const [listFocus, setListFocus] = useState<WorkspaceGraphListFocus | null>(null);
  const [visibleSearch, setVisibleSearch] = useState("");
  const deferredVisibleSearch = useDeferredValue(visibleSearch);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryRevision, setRetryRevision] = useState(0);
  const [modal, setModal] = useState<WorkspaceGraphModal>(null);
  const [activeExpansion, setActiveExpansion] = useState<{
    clusterId: string;
    cursor: string | null;
  } | null>(null);
  const [expandableClusterIds, setExpandableClusterIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [expandingClusterId, setExpandingClusterId] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [layoutPaused, setLayoutPaused] = useState(false);
  const [folderGrouping, setFolderGrouping] = useState(false);
  // Optional metadata has its own scoped selection, never a native point ID.
  const [folderSelection, setFolderSelection] = useState<WorkspaceGraphFolderSelection | null>(null);
  const selectedFolderIdRef = useRef<string | null>(null);
  const folderOpenerRef = useRef<HTMLButtonElement | null>(null);
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const forcedColors = useMediaQuery("(forced-colors: active)");
  const mobileLayout = useMediaQuery("(max-width: 640px)");

  useEffect(() => {
    if (focusUrlClearPending) {
      if (focusedEntityFromUrl) {
        const nextParams = new URLSearchParams(searchParams);
        nextParams.delete("focus");
        setSearchParams(nextParams, { replace: true });
      } else {
        setFocusUrlClearPending(false);
      }
      return;
    }
    if (focusedEntityFromUrl !== requestedFocus) {
      setConsumedNavigationReveal(null);
      setRequestedFocus(focusedEntityFromUrl);
    }
  }, [focusUrlClearPending, focusedEntityFromUrl, requestedFocus, searchParams, setSearchParams]);

  useEffect(() => {
    const previous = previousScopeRef.current;
    if (previous === authorityScope) return;
    if (previous.actorId !== actorId) clearWorkspaceGraphStorage();
    else if (previous.workspaceId === workspaceId && previous.role !== observedRole && actorId) {
      clearWorkspaceGraphState(workspaceId, actorId);
    }
    previousScopeRef.current = authorityScope;
    setConsumedNavigationReveal(null);
    expansionControllerRef.current?.abort();
    if (viewportFrameRef.current !== null) cancelAnimationFrame(viewportFrameRef.current);
    viewportFrameRef.current = null;
    pendingViewportRef.current = null;
    pendingListCenterRef.current = null;
    deniedRetryRef.current = null;
    restorationRetryRef.current = null;
    const next = workspaceId && actorId
      ? loadWorkspaceGraphState(workspaceId, actorId)
      : freshWorkspaceGraphState();
    setStateOwner(authorityScope);
    setScaleOwner({ scope: authorityScope, scale: "2k" });
    setState(next);
    setDraftFilters(cloneFilters(next.dataFilters));
    setScene(null);
    setSceneOwner(null);
    setListFocus(null);
    setError(null);
    setVisibleSearch("");
    setModal(null);
    setActiveExpansion(null);
    setExpandableClusterIds(new Set());
    setExpandingClusterId(null);
    setLoading(false);
    setLayoutPaused(false);
    setFolderGrouping(false);
    setFolderSelection(null);
  }, [actorId, authorityScope, observedRole, workspaceId]);

  const request = useMemo(
    () => workspaceGraphRequest({
      lens: state.lens,
      // A Local-to-Workspace deep link must select its published document even
      // when the last saved scene excluded its kind, folder, or time range.
      // Keep saved state unchanged; the UI explicitly discloses this
      // temporary unfiltered published-scene request.
      dataFilters: requestedFocus
        ? DEFAULT_WORKSPACE_GRAPH_FILTERS
        : state.dataFilters,
    }, requestedFocus, mapBudget),
    [mapBudget, requestedFocus, state.dataFilters, state.lens],
  );
  const committedRequestRef = useRef(request);
  useLayoutEffect(() => { committedRequestRef.current = request; }, [request]);
  const scene = receivedScene && sceneOwner?.scope === authorityScope && sceneOwner.request === request
    && isWorkspaceMapWithinBudget({ nodeCount: receivedScene.nodes.length, edgeCount: receivedScene.edges.length }, mapBudget)
    ? receivedScene : null;

  useEffect(() => {
    if (!workspaceId || !actorId || stateOwner !== authorityScope
      || (deniedRetryRef.current?.scope === authorityScope
        && deniedRetryRef.current.retryRevision === retryRevision)) return undefined;
    const restorationRetry = restorationRetryRef.current;
    restorationRetryRef.current = null;
    if (restorationRetry?.retryRevision === retryRevision
      && (restorationRetry.storageEpoch !== workspaceGraphStorageEpoch()
        || restorationRetry.scope !== authorityScope)) {
      // An external purge between cleanup and this effect revokes automatic
      // recovery. A later explicit Retry/Reset may authorize a fresh read.
      setLoading(false);
      return undefined;
    }
    expansionControllerRef.current?.abort();
    setActiveExpansion(null);
    setExpandingClusterId(null);
    const controller = new AbortController();
    const storageEpoch = workspaceGraphStorageEpoch();
    const readIsCurrent = () => !controller.signal.aborted
      && committedRequestRef.current === request
      && authorityIsCurrent(authorityScope, storageEpoch);
    setLoading(true);
    setError(null);
    setScene(null);
    setSceneOwner(null);
    setLayoutPaused(false);
    void queryWorkspaceGraph(workspaceId, request, { signal: controller.signal })
      .then((payload) => {
        if (!readIsCurrent()) return;
        assertWorkspaceMapWithinBudget({ nodeCount: payload.nodes.length, edgeCount: payload.edges.length }, request.budget);
        let invalidOwnerRestoration = false;
        const restored = loadWorkspaceGraphState(
          workspaceId,
          actorId,
          payload.graph_revision,
          () => {
            invalidOwnerRestoration = true;
            restorationRetryRef.current = {
              scope: authorityScope,
              storageEpoch: workspaceGraphStorageEpoch(),
              retryRevision: retryRevision + 1,
            };
          },
        );
        if (invalidOwnerRestoration) {
          // Reject this response after the restoration-only purge. A fresh
          // read owns the new epoch; the old request must never be rebased.
          setRetryRevision((current) => current + 1);
          return;
        }
        if (!readIsCurrent()) return;
        const sameViewer = actorId !== null && restored.viewerId === actorId;
        const visibleIds = new Set(payload.nodes.map((node) => node.id));
        const focusedNode = requestedFocus && payload.focus?.node_id
          ? payload.nodes.find((node) => node.id === payload.focus?.node_id)
          : null;
        const pinnedIds = sameViewer
          ? restored.pinnedIds.filter((id) => visibleIds.has(id))
          : [];
        setState((current) => ({
          ...current,
          selectedId: requestedFocus
            ? focusedNode?.id ?? null
            : (sameViewer && restored.selectedId && visibleIds.has(restored.selectedId)
              ? restored.selectedId
              : null),
          pinnedIds,
          pinnedPositions: Object.fromEntries(
            pinnedIds.flatMap((id) => restored.pinnedPositions[id]
              ? [[id, restored.pinnedPositions[id]]]
              : []),
          ),
          viewport: sameViewer && restored.viewportEngine === "cosmos"
            ? restored.viewport : null,
          viewportEngine: "cosmos",
          viewerId: actorId ?? undefined,
        }));
        setScene(payload);
        setSceneOwner({ scope: authorityScope, storageEpoch, request });
        setExpandableClusterIds(new Set(
          payload.nodes.flatMap((node) => node.grain === "cluster" ? [node.id] : []),
        ));
      })
      .catch((caught: unknown) => {
        if (!readIsCurrent()) return;
        if (caught instanceof ApiError && [401, 403, 404].includes(caught.status)) {
          deniedRetryRef.current = { scope: authorityScope, retryRevision };
          if (caught.status === 401) clearWorkspaceGraphStorage();
          else clearWorkspaceGraphState(workspaceId, actorId);
          const fresh = freshWorkspaceGraphState();
          setState(fresh);
          setDraftFilters(cloneFilters(fresh.dataFilters));
          setVisibleSearch("");
          setModal(null);
          pendingViewportRef.current = null;
          if (viewportFrameRef.current !== null) cancelAnimationFrame(viewportFrameRef.current);
          viewportFrameRef.current = null;
          setLoading(false);
        }
        setError(workspaceGraphErrorMessage(
          caught,
          i18n.t("graph:workspaceGraph.ui.requestFailed"),
        ));
      })
      .finally(() => {
        if (readIsCurrent()) setLoading(false);
      });
    return () => controller.abort();
  }, [actorId, authorityIsCurrent, authorityScope, request, requestedFocus, retryRevision, stateOwner, workspaceId]);

  useEffect(() => {
    if (!workspaceId || !scene || !actorId || state.viewerId !== actorId
      || stateOwner !== authorityScope || sceneOwner?.scope !== authorityScope
      || sceneOwner.storageEpoch !== workspaceGraphStorageEpoch()) return;
    const storageEpoch = sceneOwner.storageEpoch;
    const persist = () => {
      if (committedRequestRef.current !== request || !authorityIsCurrent(authorityScope, storageEpoch)) return;
      saveWorkspaceGraphState(workspaceId, actorId, scene.graph_revision, state);
    };
    const timeout = window.setTimeout(persist, 250);
    // A reload/back navigation can happen before the geometry debounce fires.
    // Flush only the currently owned, successfully loaded scene on page exit.
    window.addEventListener("pagehide", persist);
    return () => {
      window.clearTimeout(timeout);
      window.removeEventListener("pagehide", persist);
    };
  }, [actorId, authorityIsCurrent, authorityScope, request, scene, sceneOwner, state, stateOwner, workspaceId]);

  useEffect(() => () => {
    expansionControllerRef.current?.abort();
    if (viewportFrameRef.current !== null) {
      cancelAnimationFrame(viewportFrameRef.current);
    }
  }, []);

  const orphanIds = useMemo(() => scene ? workspaceGraphOrphanIds(scene) : new Set<string>(), [scene]);
  const showOrphans = state.display.show_orphans || state.dataFilters.quick_filters.includes("orphans");
  const renderScene = useMemo(
    () => {
      if (!scene) return { nodes: [], edges: [] };
      const rendered = workspaceGraphRenderScene(
        scene,
        { ...state.display, show_orphans: showOrphans },
        deferredVisibleSearch,
        state.lens,
      );
      return {
        ...rendered,
        edges: rendered.edges.map((edge) => ({
          ...edge,
          label: workspaceGraphEnumLabel(edge.edgeKind ?? edge.label, t),
        })),
      };
    },
    [deferredVisibleSearch, scene, showOrphans, state.display, state.lens, t],
  );
  const mapColumns = useMemo(() => scene && workspaceId && actorId
    && state.viewerId === actorId
    && sceneOwner?.scope === authorityScope
    ? buildWorkspaceMapColumns(scene, {
      workspaceId,
      actorId,
      selectionKey: JSON.stringify(request),
      excludedNodeIds: showOrphans ? undefined : orphanIds,
    })
    : null,
  [actorId, authorityScope, orphanIds, request, scene, sceneOwner, showOrphans, state.viewerId, workspaceId]);
  const folderPreparation = useMemo(() => scene
    ? prepareWorkspaceMapFolderScene(scene, { budgets: { points: 25_000, relations: 100_000 } }) : null,
  [scene]);
  const folderContext = useMemo(() => folderPreparation?.supported && mapColumns
    ? visibleWorkspaceMapFolderContext(folderPreparation, new Set(mapColumns.nodeIds)) : null,
  [folderPreparation, mapColumns]);
  const folderScene = folderGrouping ? folderContext : null;
  const effectiveFolderGrouping = folderScene !== null;
  const folderSelectionFingerprint = mapColumns && folderContext
    ? `${mapColumns.identity.visibleFingerprint}:${folderContext.geometryFingerprint}` : null;
  const selectedFolderId = folderSelection && folderSelection.scope === authorityScope
    && folderSelection.request === request
    && folderSelection.visibleFingerprint === folderSelectionFingerprint
    ? folderSelection.id : null;
  const selectedFolder = folderScene?.folders.find((folder) => folder.id === selectedFolderId) ?? null;
  useLayoutEffect(() => { selectedFolderIdRef.current = selectedFolder?.id ?? null; }, [selectedFolder?.id]);
  const restoreFolderFocus = useCallback((sourceId: string | null) => {
    const modalPanel = modalPanelRef.current;
    const panel = modalPanel?.dataset.workspaceGraphModal === "mobile-canvas" ? modalPanel : regionRef.current;
    if (!panel) return;
    const usable = (element: HTMLElement | null | undefined): element is HTMLElement => {
      const closed = element?.closest("details:not([open])");
      return Boolean(element && panel.contains(element)
        && (!closed || closed.querySelector("summary")?.contains(element))
        && isUsableModalOpener(element));
    };
    const originalOpener = folderOpenerRef.current;
    const folderButton = Array.from(panel.querySelectorAll<HTMLButtonElement>("[data-map-folder-id]"))
      .find((button) => button.dataset.mapFolderId === selectedFolderIdRef.current);
    const sourceButton = sourceId === null ? null
      : Array.from(panel.querySelectorAll<HTMLElement>("[data-graph-node-id]"))
        .find((row) => row.dataset.graphNodeId === sourceId)?.querySelector<HTMLButtonElement>(".workspace-graph-list-select");
    const mapControl = sourceId === null ? null
      : panel.querySelector<HTMLButtonElement>(".graph-canvas-toolbar button:not(:disabled)");
    const folderToggle = panel.querySelector<HTMLButtonElement>(".workspace-graph-folder-toggle");
    const viewSummary = panel.querySelector<HTMLElement>(".workspace-graph-view-options > summary");
    const candidates = sourceId === null ? [originalOpener, folderButton, folderToggle, viewSummary]
      : [mapControl, sourceButton, originalOpener, folderButton, folderToggle, viewSummary];
    const target = candidates.find(usable);
    const scope = committedScopeRef.current;
    const storageEpoch = workspaceGraphStorageEpoch();
    requestAnimationFrame(() => {
      if (!authorityIsCurrent(scope, storageEpoch) || !panel.isConnected || !usable(target)) return;
      target.focus({ preventScroll: true });
    });
  }, [authorityIsCurrent]);
  const closeFolderDetails = useCallback(() => {
    restoreFolderFocus(null);
    setFolderSelection(null);
  }, [restoreFolderFocus]);
  const selectFolder = (id: string) => {
    if (!folderScene?.folders.some((folder) => folder.id === id)
      || !actorId || !workspaceId || !folderSelectionFingerprint) return;
    setFolderSelection({ id, scope: authorityScope, request,
      visibleFingerprint: folderSelectionFingerprint });
  };
  useEffect(() => {
    setFolderSelection(null);
    folderOpenerRef.current = null;
  }, [authorityScope, request, folderSelectionFingerprint]);
  const visibleNodeIds = useMemo(
    () => new Set(renderScene.nodes.map((node) => node.id)),
    [renderScene.nodes],
  );
  const visibleEdgeIds = useMemo(
    () => new Set(renderScene.edges.map((edge) => edge.id)),
    [renderScene.edges],
  );
  const focusedId = listFocus && mapColumns
    && listFocus.scope === authorityScope
    && listFocus.request === request
    && listFocus.storageEpoch === sceneOwner?.storageEpoch
    && listFocus.storageEpoch === workspaceGraphStorageEpoch()
    && (visibleNodeIds.has(listFocus.id) || visibleEdgeIds.has(listFocus.id))
    ? listFocus.id : null;
  useEffect(() => {
    // A removed/filtered row must not regain emphasis when it becomes visible
    // again without receiving a new DOM focus event.
    if (listFocus && focusedId === null) setListFocus(null);
  }, [focusedId, listFocus]);
  const handleListFocus = (id: string) => {
    if (!workspaceId || !actorId || !mapColumns
      || sceneOwner?.scope !== authorityScope
      || sceneOwner.storageEpoch !== workspaceGraphStorageEpoch()
      || (!visibleNodeIds.has(id) && !visibleEdgeIds.has(id))) return;
    setListFocus({ id, scope: authorityScope, request, storageEpoch: sceneOwner.storageEpoch });
  };
  const handleListBlur = (id: string) => {
    setListFocus((current) => current?.id === id
      && current.scope === authorityScope
      && current.request === request ? null : current);
  };
  const selectedNode = scene?.nodes.find((node) =>
    node.id === state.selectedId && mapColumns?.nodeIds.includes(node.id),
  ) ?? null;
  const selectedEdge = scene?.edges.find((edge) =>
    edge.id === state.selectedId && mapColumns?.edgeIds.includes(edge.id),
  ) ?? null;
  const focusStatus = requestedFocus && scene
    ? scene.focus?.status ?? "unavailable"
    : null;
  const focusUnavailable = focusStatus === "unavailable";

  useEffect(() => {
    const region = regionRef.current;
    if (!region) return undefined;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (state.selectedId || selectedFolderId) {
        event.preventDefault();
        event.stopPropagation();
        if (selectedFolderId) {
          closeFolderDetails();
          return;
        }
        const clearButtonFocused = document.activeElement?.classList.contains("workspace-graph-selection-clear");
        pendingListCenterRef.current = null;
        setState((current) => ({ ...current, selectedId: null }));
        setListFocus(null);
        setFolderSelection(null);
        if (clearButtonFocused) requestAnimationFrame(() => {
          region.querySelector<HTMLInputElement>("#workspace-graph-visible-search")?.focus({ preventScroll: true });
        });
      }
    };
    region.addEventListener("keydown", handleEscape);
    return () => region.removeEventListener("keydown", handleEscape);
  }, [closeFolderDetails, state.selectedId, selectedFolderId]);

  useEffect(() => {
    if (!modal) return undefined;
    const panel = modalPanelRef.current;
    const region = regionRef.current;
    if (!panel || !region) return undefined;

    const underlay = region.closest<HTMLElement>(".app-shell")
      ?? screenRef.current
      ?? region;
    const previousAriaHidden = underlay.getAttribute("aria-hidden");
    const previousInertAttribute = underlay.getAttribute("inert");
    const supportsInert = "inert" in underlay;
    const inertUnderlay = underlay as HTMLElement & { inert?: boolean };
    const previousInert = supportsInert ? inertUnderlay.inert : undefined;
    const previousOverflow = region.style.overflow;
    const previousPaddingRight = region.style.paddingRight;
    const previousScrollTop = region.scrollTop;
    const previousScrollLock = region.getAttribute("data-modal-scroll-lock");
    const regionStyle = region.ownerDocument.defaultView?.getComputedStyle(region);
    const borderWidth = (Number.parseFloat(regionStyle?.borderLeftWidth ?? "0") || 0)
      + (Number.parseFloat(regionStyle?.borderRightWidth ?? "0") || 0);
    const scrollbarWidth = Math.max(
      0,
      region.offsetWidth - region.clientWidth - borderWidth,
    );
    const computedPaddingRight = Number.parseFloat(regionStyle?.paddingRight ?? "0") || 0;

    underlay.setAttribute("aria-hidden", "true");
    underlay.setAttribute("inert", "");
    if (supportsInert) inertUnderlay.inert = true;
    if (scrollbarWidth > 0) {
      region.style.paddingRight = `${computedPaddingRight + scrollbarWidth}px`;
    }
    region.style.overflow = "hidden";
    region.setAttribute("data-modal-scroll-lock", "true");
    const preserveRegionScroll = () => {
      if (region.scrollTop !== previousScrollTop) region.scrollTop = previousScrollTop;
    };
    region.addEventListener("scroll", preserveRegionScroll, { passive: true });

    const initialTarget = () => {
      if (modal.kind === "mobileCanvas") return mobileCanvasCloseRef.current;
      if (modal.target === "folder") {
        return refineFolderControlRef.current ?? refineFolderSectionRef.current;
      }
      if (modal.target === "time") {
        return refineTimeControlRef.current ?? refineTimeSectionRef.current;
      }
      return refineCloseRef.current;
    };

    const focusFrame = requestAnimationFrame(() => {
      const target = initialTarget() ?? panel;
      target.scrollIntoView?.({ block: "nearest" });
      target.focus({ preventScroll: true });
      preserveRegionScroll();
    });
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (modal.kind === "mobileCanvas" && selectedFolderIdRef.current) {
          closeFolderDetails();
          return;
        }
        if (modal.kind === "mobileCanvas" && selectedIdRef.current) {
          const clearButtonFocused = document.activeElement?.classList.contains("workspace-graph-selection-clear");
          pendingListCenterRef.current = null;
          setState((current) => ({ ...current, selectedId: null }));
          if (clearButtonFocused) requestAnimationFrame(() => mobileCanvasCloseRef.current?.focus({ preventScroll: true }));
          return;
        }
        if (modal.kind === "refine") setDraftFilters(cloneFilters(state.dataFilters));
        setModal(null);
        return;
      }
      if (event.key === "Tab") trapTabFocus(event, panel);
    };
    const containFocus = (event: FocusEvent) => {
      if (event.target instanceof Node && !panel.contains(event.target)) {
        (initialTarget() ?? panel).focus({ preventScroll: true });
        preserveRegionScroll();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    document.addEventListener("focusin", containFocus, true);

    return () => {
      cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", handleKeyDown, true);
      document.removeEventListener("focusin", containFocus, true);
      region.removeEventListener("scroll", preserveRegionScroll);
      restoreAttribute(underlay, "aria-hidden", previousAriaHidden);
      restoreAttribute(underlay, "inert", previousInertAttribute);
      if (supportsInert) inertUnderlay.inert = previousInert ?? false;
      region.style.overflow = previousOverflow;
      region.style.paddingRight = previousPaddingRight;
      region.scrollTop = previousScrollTop;
      restoreAttribute(region, "data-modal-scroll-lock", previousScrollLock);

      const opener = modalOpenerRef.current;
      modalOpenerRef.current = null;
      requestAnimationFrame(() => {
        if (opener && isUsableModalOpener(opener)) {
          opener.focus({ preventScroll: true });
        } else if (region.isConnected) {
          region.focus({ preventScroll: true });
        }
        preserveRegionScroll();
      });
    };
  }, [closeFolderDetails, modal, state.dataFilters]);

  useEffect(() => {
    if (modal?.kind !== "mobileCanvas") return;
    if (!mobileLayout || !scene || !mapColumns?.nodeIds.length) {
      modalOpenerRef.current = null;
      setModal(null);
    }
  }, [mapColumns?.nodeIds.length, mobileLayout, modal, scene]);

  const clearFocusNavigation = () => {
    if (!requestedFocus) return;
    // Clear the local override in the same React update as the filter/lens
    // change. URL cleanup follows after that commit, so it cannot issue an
    // intermediate request with the old lens and saved filters.
    setRequestedFocus(null);
    setFocusUrlClearPending(true);
  };

  const applyFilters = () => {
    if (!draftFilters.document_kinds.length || invalidWorkspaceGraphTimeWindow(draftFilters)
      || !refineFiltersDiffer(draftFilters, state.dataFilters)) return;
    pendingListCenterRef.current = null;
    clearFocusNavigation();
    setState((current) => ({
      ...current,
      dataFilters: cloneFilters(draftFilters),
      selectedId: null,
    }));
    setModal(null);
  };

  const closeRefine = () => {
    setDraftFilters(cloneFilters(state.dataFilters));
    setModal(null);
  };

  const openRefine = (target: WorkspaceGraphRefineTarget, trigger: HTMLButtonElement) => {
    setDraftFilters(cloneFilters(state.dataFilters));
    modalOpenerRef.current = trigger;
    setModal({ kind: "refine", target });
  };

  const resetWorkspaceGraph = () => {
    pendingListCenterRef.current = null;
    if (workspaceId && actorId) clearWorkspaceGraphState(workspaceId, actorId);
    const fresh = freshWorkspaceGraphState();
    setScaleOwner({ scope: authorityScope, scale: "2k" });
    setState(fresh);
    setDraftFilters(cloneFilters(fresh.dataFilters));
    setVisibleSearch("");
    setScene(null);
    setActiveExpansion(null);
    setExpandableClusterIds(new Set());
    setRetryRevision((value) => value + 1);
  };

  const expandCluster = (clusterId: string, cursor: string | null = null) => {
    if (!workspaceId || !scene || !actorId || committedRequestRef.current !== request
      || sceneOwner?.scope !== authorityScope
      || !authorityIsCurrent(authorityScope, sceneOwner.storageEpoch)) return;
    const pagingActiveCluster = activeExpansion?.clusterId === clusterId;
    if (!pagingActiveCluster && !expandableClusterIds.has(clusterId)) return;
    const previousDocumentIds = new Set(
      scene.nodes.flatMap((node) => node.grain === "document" ? [node.id] : []),
    );
    expansionControllerRef.current?.abort();
    const controller = new AbortController();
    const storageEpoch = sceneOwner.storageEpoch;
    const readIsCurrent = () => !controller.signal.aborted
      && committedRequestRef.current === request
      && authorityIsCurrent(authorityScope, storageEpoch);
    expansionControllerRef.current = controller;
    setExpandingClusterId(clusterId);
    setError(null);
    void queryWorkspaceGraph(
      workspaceId,
      { ...request, expansion: { cluster_id: clusterId, cursor } },
      { signal: controller.signal },
    )
      .then((payload) => {
        if (!readIsCurrent()) return;
        assertWorkspaceMapWithinBudget({ nodeCount: payload.nodes.length, edgeCount: payload.edges.length }, request.budget);
        const firstExpanded = payload.nodes.find((node) =>
          node.grain === "document" && !previousDocumentIds.has(node.id),
        ) ?? null;
        const visibleIds = new Set(payload.nodes.map((node) => node.id));
        setState((current) => ({
          ...current,
          selectedId: firstExpanded?.id
            ?? (current.selectedId && visibleIds.has(current.selectedId)
              ? current.selectedId
              : null),
          pinnedIds: current.pinnedIds.filter((id) => visibleIds.has(id)),
          pinnedPositions: Object.fromEntries(
            current.pinnedIds
              .filter((id) => visibleIds.has(id) && current.pinnedPositions[id])
              .map((id) => [id, current.pinnedPositions[id]]),
          ),
        }));
        setScene(payload);
        setActiveExpansion({ clusterId, cursor: payload.expansion_cursor });
        setAnnouncement(
          payload.expansion_cursor
            ? t("workspaceGraph.expansion.more")
            : t("workspaceGraph.expansion.complete"),
        );
        requestAnimationFrame(() => {
          if (readIsCurrent()) document.getElementById("workspace-graph-list-title")?.focus();
        });
      })
      .catch((caught: unknown) => {
        if (!readIsCurrent()) return;
        if (caught instanceof ApiError && [401, 403, 404].includes(caught.status) && actorId) {
          deniedRetryRef.current = { scope: authorityScope, retryRevision };
          if (caught.status === 401) clearWorkspaceGraphStorage();
          else clearWorkspaceGraphState(workspaceId, actorId);
          const fresh = freshWorkspaceGraphState();
          setState(fresh);
          setDraftFilters(cloneFilters(fresh.dataFilters));
          setVisibleSearch("");
          setScene(null);
          setSceneOwner(null);
          setModal(null);
          setActiveExpansion(null);
          pendingViewportRef.current = null;
          if (viewportFrameRef.current !== null) cancelAnimationFrame(viewportFrameRef.current);
          viewportFrameRef.current = null;
          setExpandingClusterId(null);
          setLoading(false);
        }
        setError(workspaceGraphErrorMessage(caught, t("workspaceGraph.expansion.failed")));
      })
      .finally(() => {
        if (readIsCurrent()) setExpandingClusterId(null);
      });
  };

  const handleMapViewport = useCallback((viewport: GraphViewport) => {
    if (sceneOwner?.scope !== authorityScope || sceneOwner.request !== request
      || committedRequestRef.current !== request || !authorityIsCurrent(authorityScope, sceneOwner.storageEpoch)) return;
    pendingViewportRef.current = viewport;
    if (viewportFrameRef.current !== null) return;
    viewportFrameRef.current = requestAnimationFrame(() => {
      viewportFrameRef.current = null;
      const nextViewport = pendingViewportRef.current;
      pendingViewportRef.current = null;
      if (!nextViewport || committedRequestRef.current !== request || !authorityIsCurrent(authorityScope, sceneOwner.storageEpoch)) return;
      setState((current) => ({
        ...current,
        viewport: nextViewport,
        viewportEngine: "cosmos",
        viewerId: actorId ?? undefined,
      }));
    });
  }, [actorId, authorityIsCurrent, authorityScope, request, sceneOwner]);

  const sceneIsCurrent = () => sceneOwner?.scope === authorityScope
    && sceneOwner.request === request && committedRequestRef.current === request
    && authorityIsCurrent(authorityScope, sceneOwner.storageEpoch);
  const flushOwnedMapState = () => {
    if (!workspaceId || !actorId || !scene || !sceneIsCurrent()
      || stateOwner !== authorityScope || state.viewerId !== actorId) return;
    // SPA links do not emit pagehide. Save explicit intent before either
    // navigation or a budget change can cancel debounce/viewport delivery.
    const pendingViewport = viewportFrameRef.current !== null ? pendingViewportRef.current : null;
    const snapshot = pendingViewport ? {
      ...state, viewport: pendingViewport, viewportEngine: "cosmos" as const, viewerId: actorId,
    } : state;
    saveWorkspaceGraphState(workspaceId, actorId, scene.graph_revision, snapshot);
    if (viewportFrameRef.current !== null) cancelAnimationFrame(viewportFrameRef.current);
    viewportFrameRef.current = null;
    pendingViewportRef.current = null;
    if (pendingViewport) setState(snapshot);
  };
  const handleDocumentNavigation: WorkspaceGraphDocumentNavigation = (event) => {
    const target = event.currentTarget.target;
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
      || event.shiftKey || event.altKey || (target && target !== "_self")) return;
    flushOwnedMapState();
  };
  const handleMapExport = () => {
    if (!scene || !sceneIsCurrent()) return;
    exportWorkspaceGraph(scene, renderScene);
  };

  const staticLayout = !canAnimateWorkspaceRelationLayout(mapColumns?.nodeIds.length ?? 0, mapBudget);
  const handleMapRendererStatus = (status: "loading" | "ready" | "error") => {
    if (!sceneIsCurrent()) return;
    const pending = pendingListCenterRef.current;
    if (status === "ready" && pending) {
      if (pending === state.selectedId && mapColumns?.nodeIds.includes(pending)) {
        canvasRef.current?.centerOn(pending);
      }
      pendingListCenterRef.current = null;
    }
  };
  const handleFolderMemberSelect = (sourceId: string) => {
    if (!mapColumns?.nodeIds.includes(sourceId)) return;
    restoreFolderFocus(sourceId);
    setListFocus(null);
    setFolderSelection(null);
    setState((current) => ({ ...current, selectedId: sourceId }));
    // Aggregate memberships describe a source node, not new native points.
    if (canvasRef.current) canvasRef.current.centerOn(sourceId);
    else pendingListCenterRef.current = sourceId;
  };
  const handleMapSelect = (id: string | null) => {
    if (!sceneIsCurrent()) return;
    setListFocus(null);
    if (id === null) pendingListCenterRef.current = null;
    setFolderSelection(null);
    setState((current) => ({ ...current, selectedId: id }));
  };
  const handleMapDisplay = (display: WorkspaceGraphRestorableState["display"]) => {
    const hideOrphans = !display.show_orphans && !state.dataFilters.quick_filters.includes("orphans");
    if (hideOrphans && state.selectedId && orphanIds.has(state.selectedId)) pendingListCenterRef.current = null;
    setState((current) => ({
      ...current,
      display,
      selectedId: hideOrphans && current.selectedId && orphanIds.has(current.selectedId)
        ? null : current.selectedId,
    }));
  };
  const clearSelection = () => {
    const selectedRow = Array.from(regionRef.current?.querySelectorAll<HTMLElement>(
      "[data-graph-node-id], [data-graph-edge-id]",
    ) ?? []).find((row) => row.dataset.graphNodeId === state.selectedId
      || row.dataset.graphEdgeId === state.selectedId)?.querySelector<HTMLButtonElement>("button");
    setState((current) => ({ ...current, selectedId: null }));
    setFolderSelection(null);
    pendingListCenterRef.current = null;
    requestAnimationFrame(() => {
      const target = modal?.kind === "mobileCanvas" ? mobileCanvasCloseRef.current
        : selectedRow?.isConnected ? selectedRow
          : regionRef.current?.querySelector<HTMLInputElement>("#workspace-graph-visible-search");
      target?.focus({ preventScroll: true });
    });
  };
  const toggleFolderGrouping = () => {
    setFolderGrouping((enabled) => !enabled);
    setFolderSelection(null);
    folderOpenerRef.current = null;
  };
  const renderFolderIndex = () => folderScene ? <details className="workspace-graph-folder-index">
    <summary>{t("workspaceGraph.folders.index", { count: folderScene.folders.length })}</summary>
    <ul>{folderScene.folders.map((folder) => <li key={folder.id}>
      <button aria-pressed={selectedFolderId === folder.id} className="workspace-map-folder-action"
        data-map-folder-id={folder.id} onClick={(event) => {
          folderOpenerRef.current = event.currentTarget;
          selectFolder(folder.id);
        }} type="button">
        {folder.label} · {folder.direct_member_count}
      </button>
    </li>)}</ul>
  </details> : null;
  const selectionNote = state.selectedId ? (
    <div className="workspace-graph-selection-note" role="status">
      <span>{t("workspaceGraph.ui.selectionFocus")}</span>
      <button className="workspace-graph-selection-clear" onClick={clearSelection} type="button">
        {t("workspaceGraph.ui.clearSelection")}
      </button>
    </div>
  ) : null;
  const renderCanvasPanel = (
    canvasScene: WorkspaceGraphScene,
    mobileDialog: boolean,
  ) => (
    <section
      aria-labelledby={mobileDialog ? "workspace-graph-mobile-canvas-title" : undefined}
      aria-modal={mobileDialog ? true : undefined}
      className={`workspace-graph-canvas-panel${mobileDialog ? " is-mobile-open" : ""}`}
      data-workspace-graph-modal={mobileDialog ? "mobile-canvas" : undefined}
      ref={mobileDialog ? modalPanelRef : undefined}
      role={mobileDialog ? "dialog" : undefined}
      tabIndex={mobileDialog ? -1 : undefined}
    >
      {mobileDialog ? (
        <>
          <h2 className="visually-hidden" id="workspace-graph-mobile-canvas-title">
            {t("workspaceGraph.ui.canvasDialogTitle")}
          </h2>
          <button
            aria-label={t("workspaceGraph.ui.closeCanvasLabel")}
            className="workspace-graph-mobile-canvas-close"
            onClick={() => setModal(null)}
            ref={mobileCanvasCloseRef}
            type="button"
          >
            <X aria-hidden="true" size={18} /> {t("workspaceGraph.ui.closeCanvas")}
          </button>
        </>
      ) : null}
      <div className="workspace-graph-canvas-controls">
        {mobileDialog ? <button
          aria-pressed={effectiveFolderGrouping}
          className="workspace-graph-folder-toggle"
          disabled={!folderContext}
          onClick={toggleFolderGrouping} type="button"
        >{t("workspaceGraph.folders.groupByFolder")}</button> : null}
        <WorkspaceGraphCanvasToolbar
          canvasRef={canvasRef}
          exportEnabled={canvasScene.capabilities.export}
          layoutPaused={layoutPaused}
          onExport={handleMapExport}
          onReset={() => {
            if (!sceneIsCurrent()) return;
            pendingListCenterRef.current = null;
            setListFocus(null);
            setFolderSelection(null);
            canvasRef.current?.reset();
            setLayoutPaused(false);
            setState((current) => ({
              ...current,
              pinnedIds: [],
              pinnedPositions: {},
              selectedId: null,
              viewport: null,
            }));
          }}
          onToggleLayout={() => {
            if (!sceneIsCurrent()) return;
            setLayoutPaused((paused) => !paused);
          }}
          reducedMotion={reducedMotion}
          staticLayout={staticLayout}
        />
        {staticLayout ? <p className="workspace-graph-layout-hint">{t("workspaceGraph.canvas.fixedLayoutHint")}</p> : null}
        {selectionNote}
        {mobileDialog ? renderFolderIndex() : null}
      </div>
      {mapColumns ? <WorkspaceMapCanvas
        ref={canvasRef}
        active
        columns={mapColumns}
        budget={mapBudget}
        focusedId={focusedId}
        forcedColors={forcedColors}
        layoutPaused={layoutPaused}
        navigationTargetId={requestedFocus && !navigationRevealConsumed
          ? scene?.focus?.node_id ?? null : null}
        navigationIsCurrent={sceneIsCurrent}
        onNavigationRevealFinished={() => {
          if (sceneIsCurrent()) setConsumedNavigationReveal({ scope: authorityScope, focus: requestedFocus });
        }}
        onHover={() => {}}
        onPin={(id, position) => {
          if (!sceneIsCurrent()) return;
          setState((current) => ({
            ...current,
            pinnedIds: current.pinnedIds.includes(id)
              ? current.pinnedIds
              : [...current.pinnedIds, id],
            pinnedPositions: { ...current.pinnedPositions, [id]: position },
          }));
        }}
        onRendererStatusChange={handleMapRendererStatus}
        onSelect={handleMapSelect}
        onViewport={handleMapViewport}
        pinnedIds={state.pinnedIds}
        pinnedPositions={state.pinnedPositions}
        reducedMotion={reducedMotion}
        searchMatchedIds={deferredVisibleSearch.trim() ? visibleNodeIds : null}
        selectedId={state.selectedId}
        showEdges={state.display.show_edges}
        showLabels={state.display.show_labels}
        viewport={state.viewportEngine === "cosmos" && state.viewerId === actorId
          ? state.viewport : null}
      /> : (
        <StatusBanner tone="warning">
          {t("workspaceGraph.ui.requestFailed")}
        </StatusBanner>
      )}
      {mobileDialog && selectedFolder && folderScene ? <WorkspaceMapFolderDetails
        folderId={selectedFolder.id}
        mobileLayout
        memberLimit={mapBudget.documents}
        onClose={closeFolderDetails}
        onFolder={selectFolder}
        onNavigate={handleDocumentNavigation}
        onSelect={handleFolderMemberSelect}
        prepared={folderScene}
        scene={canvasScene}
      /> : null}
    </section>
  );

  // Actor, workspace and observed role can change without a route remount.
  // Do not show the previous authority's controls/documents for even one frame.
  if (stateOwner !== authorityScope
    || (scene && (state.viewerId !== actorId
    || sceneOwner?.scope !== authorityScope))) {
    return (
      <div className="workspace-screen single-column workspace-graph-screen">
        <StatusBanner tone="info">{t("workspaceGraph.ui.building")}</StatusBanner>
      </div>
    );
  }

  return (
    <>
    <div className="workspace-screen single-column workspace-graph-screen" ref={screenRef}>
      <section
        aria-labelledby="workspace-graph-title"
        className="feature-main workspace-graph"
        ref={regionRef}
        tabIndex={-1}
      >
        <header className="feature-header workspace-graph-header">
          <div>
            <h1 id="workspace-graph-title">{t("workspaceGraph.ui.title")}</h1>
            <p className="visually-hidden">
              {t("workspaceGraph.ui.subtitle")}
            </p>
          </div>
          {scene ? (
            <span className="workspace-graph-coverage" aria-live="polite">
              {t("workspaceGraph.ui.visibleCoverage", {
                visible: visibleWorkspaceGraphDocumentCount(scene, mapColumns?.nodeIds ?? []),
                total: scene.counts.candidate_documents,
                representation: workspaceGraphEnumLabel(scene.representation, t),
              })}
            </span>
          ) : null}
        </header>

        <WorkspaceGraphToolbar
          activeFilterCount={countActiveRefineFilters(state.dataFilters)}
          display={state.display}
          folderAvailable={Boolean(folderContext)}
          folderEnabled={effectiveFolderGrouping}
          lens={state.lens}
          mobileLayout={mobileLayout}
          deviceProfile={deviceProfile}
          mapScale={mapScale}
          onMapScale={(scale) => {
            if (deviceProfile !== "desktop" || (scale !== "2k" && scale !== "10k")
              || !authorityIsCurrent(authorityScope)) return;
            flushOwnedMapState();
            setConsumedNavigationReveal({ scope: authorityScope, focus: requestedFocus });
            pendingListCenterRef.current = null;
            expansionControllerRef.current?.abort();
            setScaleOwner({ scope: authorityScope, scale });
          }}
          onDisplay={handleMapDisplay}
          onLens={(lens) => {
            pendingListCenterRef.current = null;
            clearFocusNavigation();
            setState((current) => ({ ...current, lens, selectedId: null }));
          }}
          onOpenRefine={(trigger) => openRefine("overview", trigger)}
          onSearch={setVisibleSearch}
          onToggleFolder={toggleFolderGrouping}
          orphanCount={state.dataFilters.quick_filters.includes("orphans") ? 0 : orphanIds.size}
          pendingChanges={refineFiltersDiffer(draftFilters, state.dataFilters)}
          ready={Boolean(scene)}
          search={visibleSearch}
          showOrphans={showOrphans}
        />
        {state.lens === "directory" || state.lens === "compare" ? (
          <StatusBanner tone="info">
            {t(`workspaceGraph.lensNotice.${state.lens}`)}
          </StatusBanner>
        ) : null}
        {requestedFocus ? (
          <StatusBanner tone={focusUnavailable ? "warning" : "info"}>
            {t(focusUnavailable
              ? "workspaceGraph.ui.focusUnavailable"
              : focusStatus === "cluster"
                ? "workspaceGraph.ui.focusInCluster"
                : "workspaceGraph.ui.focusOverride")}
          </StatusBanner>
        ) : null}

        {loading ? (
          <StatusBanner tone="info">
            <strong>{t("workspaceGraph.ui.building")}</strong>
            <span> {t("workspaceGraph.ui.buildingBody")}</span>
          </StatusBanner>
        ) : null}
        {error ? (
          <StatusBanner
            action={(
              <div className="workspace-graph-error-actions">
                <button className="workspace-graph-error-action" onClick={() => setRetryRevision((value) => value + 1)} type="button">
                  {t("workspaceGraph.ui.retry")}
                </button>
                <button className="workspace-graph-error-action" onClick={resetWorkspaceGraph} type="button">
                  {t("workspaceGraph.ui.resetSavedState")}
                </button>
              </div>
            )}
            tone="warning"
          >
            <strong>{t("workspaceGraph.ui.unavailable")}</strong> {error}
          </StatusBanner>
        ) : null}
        {scene && (scene.truncated || scene.completeness !== "complete" || scene.warnings.length) ? (
          <StatusBanner tone="warning">
            <strong>{coverageLabel(scene, t)}</strong>{" "}
            {scene.omission_reasons.map((reason) => omissionMessage(reason, t, request.budget)).join(" ")}
            {scene.warnings.length ? ` ${scene.warnings.map((warning) => sceneWarningMessage(warning, t)).join(" ")}` : null}
          </StatusBanner>
        ) : null}

        {scene ? (
          <>
            {mobileLayout ? selectionNote : null}
            {mapColumns?.nodeIds.length ? (
              <button
                className="workspace-graph-mobile-canvas-button"
                onClick={(event) => {
                  if (!mobileLayout) return;
                  modalOpenerRef.current = event.currentTarget;
                  setModal({ kind: "mobileCanvas" });
                }}
                ref={mobileCanvasButtonRef}
                type="button"
              >
                <Maximize2 aria-hidden="true" size={16} /> {t("workspaceGraph.ui.openCanvas")}
              </button>
            ) : null}
            {mapColumns?.nodeIds.length ? (
              <div className="workspace-graph-stage">
                {!mobileLayout ? renderCanvasPanel(scene, false) : null}
                <div className="workspace-graph-sidebar">
                {renderFolderIndex()}
                {selectedFolder && folderScene ? <WorkspaceMapFolderDetails
                  folderId={selectedFolder.id}
                  mobileLayout={mobileLayout}
                  memberLimit={mapBudget.documents}
                  onClose={closeFolderDetails}
                  onFolder={selectFolder}
                  onNavigate={handleDocumentNavigation}
                  onSelect={handleFolderMemberSelect}
                  prepared={folderScene}
                  scene={scene}
                /> : null}
                {!selectedFolder && (selectedNode || selectedEdge) ? <WorkspaceGraphDetails
                  canExpand={selectedNode?.grain === "cluster"
                    && expandableClusterIds.has(selectedNode.id)}
                  edge={selectedEdge}
                  expanding={selectedNode?.grain === "cluster" && expandingClusterId === selectedNode.id}
                  node={selectedNode}
                  onCenter={!mobileLayout ? (id) => canvasRef.current?.centerOn(id) : undefined}
                  onExpand={(clusterId) => expandCluster(clusterId)}
                  onNavigate={handleDocumentNavigation}
                /> : null}
                <WorkspaceGraphList
                  activeExpansion={activeExpansion}
                  expandableClusterIds={expandableClusterIds}
                  expandingClusterId={expandingClusterId}
                  focusedId={focusedId}
                  lens={state.lens}
                  mobileLayout={mobileLayout}
                  scene={scene}
                  selectedId={state.selectedId}
                  visibleEdgeIds={visibleEdgeIds}
                  visibleNodeIds={visibleNodeIds}
                  onSelect={(id) => {
                    setFolderSelection(null);
                    setState((current) => ({ ...current, selectedId: id }));
                    if (canvasRef.current) canvasRef.current.centerOn(id);
                    else pendingListCenterRef.current = id;
                  }}
                  onExpand={expandCluster}
                  onFocus={handleListFocus}
                  onBlur={handleListBlur}
                  onNavigate={handleDocumentNavigation}
                />
                </div>
              </div>
            ) : (
              <div className="empty-state empty-state-rich">
                <h2>{t("workspaceGraph.ui.empty")}</h2>
                <p>{t("workspaceGraph.ui.emptyBody")}</p>
                <button
                  className="workspace-graph-reset"
                  onClick={() => {
                    const fresh = freshWorkspaceGraphState();
                    setState(fresh);
                    setDraftFilters(cloneFilters(fresh.dataFilters));
                    setVisibleSearch("");
                  }}
                  type="button"
                >
                  {t("workspaceGraph.ui.resetFilters")}
                </button>
              </div>
            )}
            <WorkspaceGraphLegend />
          </>
        ) : null}
        <WorkspaceGraphAdvancedControls
          activeFilters={state.dataFilters}
          onOpenRefine={openRefine}
        />
        {scene ? <WorkspaceGraphMetadata scene={scene} /> : null}

        <p aria-atomic="true" aria-live="polite" className="visually-hidden">{announcement}</p>
      </section>
    </div>
    {typeof document !== "undefined" && modal?.kind === "refine"
      ? createPortal(
          <WorkspaceGraphRefineDrawer
            activeFilters={state.dataFilters}
            closeButtonRef={refineCloseRef}
            display={state.display}
            draftFilters={draftFilters}
            folderControlRef={refineFolderControlRef}
            folderSectionRef={refineFolderSectionRef}
            impact={estimateWorkspaceGraphImpact(scene, draftFilters)}
            onApply={applyFilters}
            onClose={closeRefine}
            onDisplay={handleMapDisplay}
            onDraftFilters={setDraftFilters}
            panelRef={modalPanelRef}
            scene={scene}
            target={modal.target}
            timeControlRef={refineTimeControlRef}
            timeSectionRef={refineTimeSectionRef}
          />,
          document.body,
        )
      : null}
    {typeof document !== "undefined"
      && modal?.kind === "mobileCanvas"
      && mobileLayout
      && scene
      && Boolean(mapColumns?.nodeIds.length)
      ? createPortal(renderCanvasPanel(scene, true), document.body)
      : null}
    </>
  );
}

function WorkspaceGraphAdvancedControls({
  activeFilters,
  onOpenRefine,
}: {
  activeFilters: WorkspaceGraphDataFilters;
  onOpenRefine: (
    target: WorkspaceGraphRefineTarget,
    trigger: HTMLButtonElement,
  ) => void;
}) {
  const { t } = useTranslation("graph");
  const activeCount = countActiveRefineFilters(activeFilters);
  return (
    <details className="workspace-graph-advanced-controls">
      <summary>
        {t("workspaceGraph.controls.advanced")}
        {activeCount ? (
          <span className="workspace-graph-advanced-count">
            {t("workspaceGraph.controls.activeFilterCount", { count: activeCount })}
          </span>
        ) : null}
      </summary>
      <div className="workspace-graph-advanced-body">
        <p>{t("workspaceGraph.controls.appliedSummary", {
          kinds: activeFilters.document_kinds.map((kind) => t(`workspaceGraph.controls.${kind}`)).join(" / "),
          count: activeCount,
        })}</p>
        {activeFilters.search ? <p>{t("workspaceGraph.controls.search")}: {activeFilters.search}</p> : null}
        <button className="workspace-graph-refine-trigger" onClick={(event) => onOpenRefine("overview", event.currentTarget)} type="button">{t("workspaceGraph.controls.editFilters")}</button>
        <button className="workspace-graph-refine-trigger" onClick={(event) => onOpenRefine("folder", event.currentTarget)} type="button">{t("workspaceGraph.controls.folder")}</button>
        <button className="workspace-graph-refine-trigger" onClick={(event) => onOpenRefine("time", event.currentTarget)} type="button">{t("workspaceGraph.controls.time")}</button>
      </div>
    </details>
  );
}

/** The Refine dialog is the sole owner of staged server-data controls. */
function WorkspaceGraphFilterChoices({
  draftFilters,
  onDraftFilters,
}: {
  draftFilters: WorkspaceGraphDataFilters;
  onDraftFilters: (filters: WorkspaceGraphDataFilters) => void;
}) {
  const { t } = useTranslation("graph");
  const quickFilters: Array<{ value: WorkspaceGraphQuickFilter; label: string }> = [
    { value: "orphans", label: t("workspaceGraph.controls.orphans") },
    { value: "highly_referenced", label: t("workspaceGraph.controls.highlyReferenced") },
    { value: "cross_folder", label: t("workspaceGraph.controls.crossFolder") },
    { value: "recently_connected", label: t("workspaceGraph.controls.recentlyConnected") },
  ];
  const toggleKind = (kind: "context" | "library") => {
    const selected = draftFilters.document_kinds.includes(kind);
    onDraftFilters({
      ...cloneFilters(draftFilters),
      document_kinds: selected
        ? draftFilters.document_kinds.filter((value) => value !== kind)
        : [...draftFilters.document_kinds, kind],
    });
  };
  const toggleQuickFilter = (value: WorkspaceGraphQuickFilter) => {
    const selected = draftFilters.quick_filters.includes(value);
    onDraftFilters({
      ...cloneFilters(draftFilters),
      quick_filters: selected
        ? draftFilters.quick_filters.filter((filter) => filter !== value)
        : [...draftFilters.quick_filters, value],
    });
  };
  return (
    <>
        <fieldset className="workspace-graph-refine-section workspace-graph-kind-control">
          <legend>{t("workspaceGraph.controls.kinds")}</legend>
          <label>
            <input
              checked={draftFilters.document_kinds.includes("context")}
              onChange={() => toggleKind("context")}
              type="checkbox"
            />
            {t("workspaceGraph.controls.context")}
          </label>
          <label>
            <input
              checked={draftFilters.document_kinds.includes("library")}
              onChange={() => toggleKind("library")}
              type="checkbox"
            />
            {t("workspaceGraph.controls.library")}
          </label>
        </fieldset>
        <fieldset className="workspace-graph-refine-section">
          <legend>{t("workspaceGraph.controls.quick")}</legend>
          <div className="workspace-graph-quick-filters">
          {quickFilters.map((filter) => (
            <button
              aria-pressed={draftFilters.quick_filters.includes(filter.value)}
              className="workspace-graph-quick-filter"
              key={filter.value}
              onClick={() => toggleQuickFilter(filter.value)}
              type="button"
            >
              {filter.label}
            </button>
          ))}
          </div>
        </fieldset>
        {!draftFilters.document_kinds.length ? (
          <p role="alert">{t("workspaceGraph.controls.chooseKind")}</p>
        ) : null}
    </>
  );
}

function countActiveRefineFilters(filters: WorkspaceGraphDataFilters): number {
  return countPendingRefineFilters(filters, DEFAULT_WORKSPACE_GRAPH_FILTERS);
}

function invalidWorkspaceGraphTimeWindow(filters: WorkspaceGraphDataFilters): boolean {
  const from = filters.time_from ? Date.parse(filters.time_from) : null;
  const to = filters.time_to ? Date.parse(filters.time_to) : null;
  return (from !== null && !Number.isFinite(from))
    || (to !== null && !Number.isFinite(to))
    || (from !== null && to !== null && from > to);
}

function WorkspaceGraphRefineDrawer({
  activeFilters,
  closeButtonRef,
  display,
  draftFilters,
  folderControlRef,
  folderSectionRef,
  impact,
  onApply,
  onClose,
  onDisplay,
  onDraftFilters,
  panelRef,
  scene,
  target,
  timeControlRef,
  timeSectionRef,
}: {
  activeFilters: WorkspaceGraphDataFilters;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
  display: WorkspaceGraphRestorableState["display"];
  draftFilters: WorkspaceGraphDataFilters;
  folderControlRef: RefObject<HTMLInputElement | null>;
  folderSectionRef: RefObject<HTMLFieldSetElement | null>;
  impact: number | null;
  onApply: () => void;
  onClose: () => void;
  onDisplay: (display: WorkspaceGraphRestorableState["display"]) => void;
  onDraftFilters: (filters: WorkspaceGraphDataFilters) => void;
  panelRef: RefObject<HTMLElement | null>;
  scene: WorkspaceGraphScene | null;
  target: WorkspaceGraphRefineTarget;
  timeControlRef: RefObject<HTMLSelectElement | null>;
  timeSectionRef: RefObject<HTMLFieldSetElement | null>;
}) {
  const { t } = useTranslation("graph");
  const invalidTimeWindow = invalidWorkspaceGraphTimeWindow(draftFilters);
  const pendingCount = countPendingRefineFilters(draftFilters, activeFilters);
  const pendingChanges = pendingCount > 0;
  return (
    <div
      className="workspace-graph-refine-scrim"
      data-refine-target={target}
      data-workspace-graph-modal="refine"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <aside
        aria-labelledby="workspace-graph-refine-title"
        aria-modal="true"
        className="workspace-graph-refine"
        ref={panelRef}
        role="dialog"
        tabIndex={-1}
      >
        <header>
          <div>
            <p aria-live="polite" className="eyebrow" role="status">{t(pendingChanges
              ? "workspaceGraph.drawer.pendingFilters" : "workspaceGraph.drawer.eyebrow", { count: pendingCount })}</p>
            <h2 id="workspace-graph-refine-title">{t("workspaceGraph.drawer.title")}</h2>
          </div>
          <button
            aria-label={t("workspaceGraph.drawer.close")}
            className="workspace-graph-refine-close"
            onClick={onClose}
            ref={closeButtonRef}
            type="button"
          >
            <X aria-hidden="true" size={18} />
          </button>
        </header>
        <p>
          {t("workspaceGraph.drawer.body")}
        </p>

        <div className="workspace-graph-refine-search">
          <label htmlFor="workspace-graph-server-search">{t("workspaceGraph.controls.search")}</label>
          <input
            id="workspace-graph-server-search"
            maxLength={200}
            onChange={(event) => onDraftFilters({
              ...cloneFilters(draftFilters),
              search: event.currentTarget.value,
            })}
            placeholder={t("workspaceGraph.controls.searchPlaceholder")}
            type="search"
            value={draftFilters.search}
          />
          <small>{t("workspaceGraph.drawer.searchHint")}</small>
        </div>

        <WorkspaceGraphFilterChoices draftFilters={draftFilters} onDraftFilters={onDraftFilters} />

        <fieldset
          className="workspace-graph-refine-section"
          ref={folderSectionRef}
          tabIndex={-1}
        >
          <legend>{t("workspaceGraph.drawer.folder")}</legend>
          <WorkspaceGraphFolderFilters filters={draftFilters} folders={scene?.facet_counts.folders ?? []}
            onChange={onDraftFilters} searchRef={folderControlRef} />
        </fieldset>

        <fieldset
          className="workspace-graph-refine-section"
          ref={timeSectionRef}
          tabIndex={-1}
        >
          <legend>{t("workspaceGraph.drawer.time")}</legend>
          <label>
            <span>{t("workspaceGraph.drawer.basis")}</span>
            <select
              onChange={(event) => onDraftFilters({
                ...cloneFilters(draftFilters),
                time_basis: event.currentTarget.value as WorkspaceGraphDataFilters["time_basis"],
              })}
              ref={timeControlRef}
              value={draftFilters.time_basis}
            >
              <option value="document_updated">{t("workspaceGraph.drawer.documentUpdated")}</option>
              <option value="relation_observed">{t("workspaceGraph.drawer.relationObserved")}</option>
            </select>
          </label>
          <div className="workspace-graph-time-range">
            <label>
              <span>{t("workspaceGraph.drawer.from")}</span>
              <input
                max={isoToDateInput(draftFilters.time_to)}
                onChange={(event) => onDraftFilters({
                  ...cloneFilters(draftFilters),
                  time_from: dateInputToIso(event.currentTarget.value, false),
                })}
                type="date"
                value={isoToDateInput(draftFilters.time_from)}
              />
            </label>
            <label>
              <span>{t("workspaceGraph.drawer.to")}</span>
              <input
                min={isoToDateInput(draftFilters.time_from)}
                onChange={(event) => onDraftFilters({
                  ...cloneFilters(draftFilters),
                  time_to: dateInputToIso(event.currentTarget.value, true),
                })}
                type="date"
                value={isoToDateInput(draftFilters.time_to)}
              />
            </label>
          </div>
          {invalidTimeWindow ? <p role="alert">{t("workspaceGraph.drawer.invalidTime")}</p> : null}
        </fieldset>

        <fieldset className="workspace-graph-refine-section">
          <legend>{t("workspaceGraph.drawer.display")}</legend>
          <label>
            <input
              checked={display.show_labels}
              onChange={(event) => onDisplay({ ...display, show_labels: event.currentTarget.checked })}
              type="checkbox"
            />
            {t("workspaceGraph.drawer.showLabels")}
          </label>
          <small>{t("workspaceGraph.drawer.showLabelsHint")}</small>
          <label>
            <input
              checked={display.show_orphans || activeFilters.quick_filters.includes("orphans")}
              disabled={activeFilters.quick_filters.includes("orphans")}
              onChange={(event) => onDisplay({ ...display, show_orphans: event.currentTarget.checked })}
              type="checkbox"
            />
            {t("workspaceGraph.ui.showOrphans")}
          </label>
          <small>{t("workspaceGraph.drawer.showOrphansHint")}</small>
          <label>
            <input
              checked={display.show_edges}
              onChange={(event) => onDisplay({ ...display, show_edges: event.currentTarget.checked })}
              type="checkbox"
            />
            {t("workspaceGraph.drawer.showRelationships")}
          </label>
        </fieldset>

        <div className="workspace-graph-impact" role="status">
          <strong>{t("workspaceGraph.drawer.impact")}</strong>
          <span>
            {impact === null
              ? t("workspaceGraph.drawer.impactUnknown")
              : t("workspaceGraph.drawer.impactValue", { count: impact })}
          </span>
        </div>
        <footer>
          <button className="workspace-graph-refine-cancel" onClick={onClose} type="button">{t("workspaceGraph.drawer.cancel")}</button>
          <button
            className="workspace-graph-refine-apply"
            disabled={!pendingChanges || !draftFilters.document_kinds.length || invalidTimeWindow}
            onClick={onApply}
            type="button"
          >
            {t("workspaceGraph.controls.apply")}
          </button>
        </footer>
      </aside>
    </div>
  );
}

function WorkspaceGraphMetadata({ scene }: { scene: WorkspaceGraphScene }) {
  const { t } = useTranslation("graph");
  const representedDocuments = scene.counts.rendered_documents
    + scene.nodes.reduce((count, node) =>
      count + (node.grain === "cluster" ? node.member_count : 0), 0);
  return (
    <section aria-label={t("workspaceGraph.metadata.aria")} className="workspace-graph-metadata">
      <dl>
        <div><dt>{t("workspaceGraph.metadata.coverage")}</dt><dd>{t("workspaceGraph.metadata.coverageValue", { count: representedDocuments })}</dd></div>
        <div><dt>{t("workspaceGraph.metadata.scene")}</dt><dd>{t("workspaceGraph.metadata.sceneValue", { clusters: scene.counts.rendered_clusters, documents: scene.counts.rendered_documents, relations: scene.counts.rendered_relations })}</dd></div>
        <div><dt>{t("workspaceGraph.metadata.representation")}</dt><dd>{workspaceGraphEnumLabel(scene.representation, t)}</dd></div>
        <div><dt>{t("workspaceGraph.metadata.completeness")}</dt><dd>{workspaceGraphEnumLabel(scene.completeness, t)}</dd></div>
        <div><dt>{t("workspaceGraph.metadata.snapshot")}</dt><dd>{formatSnapshot(scene.generated_at)}</dd></div>
        <div><dt>{t("workspaceGraph.metadata.metricBasis")}</dt><dd>{workspaceGraphMetricLabel(scene.metric_basis, t)}</dd></div>
      </dl>
      <p title={scene.graph_revision}>{t("workspaceGraph.metadata.revision", { algorithm: scene.algorithm_version, revision: shortRevision(scene.graph_revision) })}</p>
    </section>
  );
}

function WorkspaceGraphLegend() {
  const { t } = useTranslation("graph");
  return (
    <details className="graph-legend workspace-graph-legend">
      <summary>{t("workspaceGraph.legend.title")}</summary>
      <div className="graph-legend-groups">
        <ul aria-label={t("workspaceGraph.legend.shapes")}>
          <li><span aria-hidden="true" className="workspace-graph-node-cue is-context" />{t("workspaceGraph.legend.context")}</li>
          <li><span aria-hidden="true" className="workspace-graph-node-cue is-library" />{t("workspaceGraph.legend.library")}</li>
          <li><span aria-hidden="true" className="workspace-graph-node-cue is-cluster" />{t("workspaceGraph.legend.cluster")}</li>
        </ul>
        <ul aria-label={t("workspaceGraph.legend.lines")}>
          <li><span aria-hidden="true" className="graph-edge-swatch" />{t("workspaceGraph.legend.knowledge")}</li>
          <li><span aria-hidden="true" className="graph-edge-swatch graph-edge-swatch-evidence" />{t("workspaceGraph.legend.evidence")}</li>
        </ul>
        <ul aria-label={t("workspaceGraph.legend.size")}>
          <li>{t("workspaceGraph.legend.visibleRelationMetric")}</li>
        </ul>
      </div>
    </details>
  );
}

function WorkspaceGraphList({
  activeExpansion,
  expandableClusterIds,
  expandingClusterId,
  focusedId,
  lens,
  mobileLayout,
  onExpand,
  onFocus,
  onBlur,
  onNavigate,
  onSelect,
  scene,
  selectedId,
  visibleEdgeIds,
  visibleNodeIds,
}: {
  activeExpansion: { clusterId: string; cursor: string | null } | null;
  expandableClusterIds: Set<string>;
  expandingClusterId: string | null;
  focusedId: string | null;
  lens: WorkspaceGraphAvailableLens;
  mobileLayout: boolean;
  onExpand: (clusterId: string, cursor?: string | null) => void;
  onFocus: (id: string) => void;
  onBlur: (id: string) => void;
  onNavigate: WorkspaceGraphDocumentNavigation;
  onSelect: (id: string) => void;
  scene: WorkspaceGraphScene;
  selectedId: string | null;
  visibleEdgeIds: Set<string>;
  visibleNodeIds: Set<string>;
}) {
  const { t } = useTranslation("graph");
  const nodes = scene.nodes.filter((node) => visibleNodeIds.has(node.id));
  const edges = scene.edges.filter((edge) => visibleEdgeIds.has(edge.id));
  const labels = new Map(scene.nodes.map((node) => [node.id, node.label]));
  return (
    <section className="workspace-graph-list" aria-labelledby="workspace-graph-list-title"
      onFocusCapture={(event) => {
        const row = event.target.closest<HTMLElement>("[data-graph-node-id], [data-graph-edge-id]");
        const id = row?.dataset.graphNodeId ?? row?.dataset.graphEdgeId;
        if (id) onFocus(id);
      }}
      onBlurCapture={(event) => {
        const row = event.target.closest<HTMLElement>("[data-graph-node-id], [data-graph-edge-id]");
        if (event.relatedTarget instanceof Node && row?.contains(event.relatedTarget)) return;
        const id = row?.dataset.graphNodeId ?? row?.dataset.graphEdgeId;
        if (id) onBlur(id);
      }}
    >
      <h2 id="workspace-graph-list-title" tabIndex={-1}>{t("workspaceGraph.list.title")}</h2>
      <p>{t("workspaceGraph.list.description", { edges: edges.length, nodes: nodes.length })}</p>
      {activeExpansion?.cursor ? (
        <button
          className="workspace-graph-load-more"
          disabled={expandingClusterId === activeExpansion.clusterId}
          onClick={() => onExpand(activeExpansion.clusterId, activeExpansion.cursor)}
          type="button"
        >
          {expandingClusterId === activeExpansion.clusterId
            ? t("workspaceGraph.list.loadingMore")
            : t("workspaceGraph.list.loadMore")}
        </button>
      ) : null}
      <h3>{t("workspaceGraph.list.documents")}</h3>
      <WindowedWorkspaceGraphRows
        focusedId={focusedId}
        onBlur={onBlur}
        getId={(node) => node.id}
        hint={t("workspaceGraph.list.virtualKeyboardHint", { count: nodes.length })}
        items={nodes}
        label={t("workspaceGraph.list.documents")}
        mobileLayout={mobileLayout}
        selectedId={selectedId}
        testId="workspace-graph-node-list"
        renderRow={(node, index, virtualized, rowHeight, onKeyDown) => (
          <li
            aria-posinset={virtualized ? index + 1 : undefined}
            aria-setsize={virtualized ? nodes.length : undefined}
            className={virtualized ? "workspace-graph-virtual-row" : undefined}
            data-graph-node-id={node.id}
            key={node.id}
            style={virtualized ? { height: rowHeight } : undefined}
          >
            <button
              aria-pressed={selectedId === node.id}
              className="workspace-graph-list-select"
              data-list-index={virtualized ? index : undefined}
              onKeyDown={onKeyDown}
              onClick={() => onSelect(node.id)}
              type="button"
            >
              <strong>{node.label}</strong>
              <small>{nodeSummary(node, lens, t)}</small>
            </button>
            <div className="workspace-graph-list-actions">
              {node.grain === "document" ? <Link onClick={onNavigate} to={node.path}>{t("workspaceGraph.list.open")}</Link> : null}
              {node.grain === "cluster" && expandableClusterIds.has(node.id) ? (
                <button
                  className="workspace-graph-list-expand"
                  disabled={expandingClusterId !== null}
                  onClick={() => onExpand(node.id, null)}
                  type="button"
                >
                  {expandingClusterId === node.id
                    ? t("workspaceGraph.list.expanding")
                    : t("workspaceGraph.list.expand")}
                </button>
              ) : null}
            </div>
          </li>
        )}
      />
      <h3>{t("workspaceGraph.list.relationships")}</h3>
      <WindowedWorkspaceGraphRows
        focusedId={focusedId}
        onBlur={onBlur}
        getId={(edge) => edge.id}
        hint={t("workspaceGraph.list.virtualKeyboardHint", { count: edges.length })}
        items={edges}
        label={t("workspaceGraph.list.relationships")}
        mobileLayout={mobileLayout}
        selectedId={selectedId}
        testId="workspace-graph-edge-list"
        renderRow={(edge, index, virtualized, rowHeight, onKeyDown) => (
          <li
            aria-posinset={virtualized ? index + 1 : undefined}
            aria-setsize={virtualized ? edges.length : undefined}
            className={virtualized ? "workspace-graph-virtual-row" : undefined}
            data-graph-edge-id={edge.id}
            key={edge.id}
            style={virtualized ? { height: rowHeight } : undefined}
          >
            <button
              aria-pressed={selectedId === edge.id}
              className="workspace-graph-list-select"
              data-list-index={virtualized ? index : undefined}
              onKeyDown={onKeyDown}
              onClick={() => onSelect(edge.id)}
              type="button"
            >
              <strong>{workspaceGraphEnumLabel(edge.relation_kind, t)}</strong>
              <small>{labels.get(edge.source)} → {labels.get(edge.target)} · {workspaceGraphEnumLabel(edge.layer, t)}{edge.relation_count > 1 ? ` · ${t("workspaceGraph.list.relationCount", { count: edge.relation_count })}` : ""}</small>
            </button>
          </li>
        )}
      />
    </section>
  );
}

const VIRTUAL_LIST_THRESHOLD = 1_000;
const VIRTUAL_LIST_OVERSCAN = 6;

/** Metadata follows exactly the original map's visible sources and ancestors. */
function visibleWorkspaceMapFolderContext(
  prepared: WorkspaceMapPreparedFolderScene,
  visibleSourceIds: ReadonlySet<string>,
): WorkspaceMapFolderContext {
  const points = prepared.points.filter((point) => visibleSourceIds.has(point.sourceNodeId));
  const pointIds = new Set(points.map((point) => point.id));
  const foldersById = new Map(prepared.folders.map((folder) => [folder.id, folder]));
  const folderClosure = new Set<string>();
  const pointsByFolder = new Map<string, typeof points>();
  for (const point of points) {
    if (point.folderId === null) continue;
    const members = pointsByFolder.get(point.folderId) ?? [];
    members.push(point);
    pointsByFolder.set(point.folderId, members);
    let current: string | null = point.folderId;
    while (current !== null && !folderClosure.has(current)) {
      folderClosure.add(current);
      current = foldersById.get(current)!.parent_folder_id;
    }
  }
  const folders = prepared.folders.filter((folder) => folderClosure.has(folder.id)).map((folder) => ({
    ...folder,
    direct_member_count: (pointsByFolder.get(folder.id) ?? []).reduce((count, point) => count + point.memberCount, 0),
    childFolderIds: folder.childFolderIds.filter((id) => folderClosure.has(id)),
    pointIds: (pointsByFolder.get(folder.id) ?? []).map((point) => point.id),
  }));
  const relations = prepared.relations.filter((relation) => pointIds.has(relation.source) && pointIds.has(relation.target));
  const pointsById = new Map(points.map((point) => [point.id, point]));
  const bundles = new Map<string, WorkspaceMapPreparedFolderScene["folderBundles"][number]>();
  for (const relation of relations) {
    const source = pointsById.get(relation.source)!.folderId;
    const target = pointsById.get(relation.target)!.folderId;
    if (source === target) continue;
    const [sourceFolderId, targetFolderId] = source === null || target !== null && source < target
      ? [source, target] : [target, source];
    const key = JSON.stringify([sourceFolderId, targetFolderId]);
    const previous = bundles.get(key);
    bundles.set(key, { sourceFolderId, targetFolderId,
      relationCount: (previous?.relationCount ?? 0) + relation.relationCount,
      occurrenceWeight: (previous?.occurrenceWeight ?? 0) + relation.occurrenceWeight });
  }
  return { points, folders, relations, folderBundles: [...bundles.values()],
    // Used only with the original map fingerprint to scope metadata selection.
    // It is never supplied as a native layout or geometry identity.
    geometryFingerprint: prepared.geometryFingerprint };
}

function WorkspaceMapFolderDetails({ folderId, mobileLayout, memberLimit, onClose, onFolder, onNavigate, onSelect, prepared, scene }: {
  folderId: string;
  mobileLayout: boolean;
  memberLimit: number;
  onClose(): void;
  onFolder(id: string): void;
  onNavigate: WorkspaceGraphDocumentNavigation;
  onSelect(sourceId: string): void;
  prepared: WorkspaceMapFolderContext;
  scene: WorkspaceGraphScene;
}) {
  const { t } = useTranslation("graph");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const pendingNavigationRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (pendingNavigationRef.current !== folderId) return;
    pendingNavigationRef.current = null;
    headingRef.current?.focus({ preventScroll: true });
  }, [folderId]);
  const navigateFolder = (id: string) => {
    pendingNavigationRef.current = id;
    onFolder(id);
  };
  const folder = prepared.folders.find((item) => item.id === folderId);
  const foldersById = new Map(prepared.folders.map((item) => [item.id, item]));
  const pointsById = new Map(prepared.points.map((item) => [item.id, item]));
  const nodesById = new Map(scene.nodes.map((item) => [item.id, item]));
  const members = prepared.points.filter((point) => point.folderId === folderId)
    .sort((left, right) => (right.referenceCount ?? 0) + (right.evidenceCount ?? 0)
      - (left.referenceCount ?? 0) - (left.evidenceCount ?? 0)
      || (left.entityKey < right.entityKey ? -1 : 1));
  const displayedMembers = members.slice(0, memberLimit);
  const bundles = prepared.folderBundles.filter((bundle) => bundle.sourceFolderId === folderId
    || bundle.targetFolderId === folderId).sort((left, right) => right.relationCount - left.relationCount);
  const directions = new Map<string | null, { outgoing: number; incoming: number; undirected: number }>();
  for (const relation of prepared.relations) {
    const sourceFolder = pointsById.get(relation.source)!.folderId;
    const targetFolder = pointsById.get(relation.target)!.folderId;
    if (sourceFolder === targetFolder || sourceFolder !== folderId && targetFolder !== folderId) continue;
    const other = sourceFolder === folderId ? targetFolder : sourceFolder;
    const count = directions.get(other) ?? { outgoing: 0, incoming: 0, undirected: 0 };
    if (relation.direction === "undirected") count.undirected += relation.relationCount;
    else if (sourceFolder === folderId) count.outgoing += relation.relationCount;
    else count.incoming += relation.relationCount;
    directions.set(other, count);
  }
  if (!folder) return null;
  return <section aria-label={t("workspaceGraph.folders.detailsLabel")}
    className="workspace-graph-details workspace-map-folder-details" data-map-folder-details={folder.id}>
    <div className="workspace-map-folder-heading">
      <h2 ref={headingRef} tabIndex={-1}>{folder.label}</h2>
      <button aria-label={t("workspaceGraph.folders.close")} className="workspace-map-folder-action" onClick={onClose} type="button">
        <X aria-hidden="true" size={16} />
      </button>
    </div>
    {folder.parent_folder_id ? <button className="workspace-map-folder-action" onClick={() => navigateFolder(folder.parent_folder_id!)} type="button">
      {t("workspaceGraph.folders.parent", { label: foldersById.get(folder.parent_folder_id)?.label })}
    </button> : null}
    {folder.childFolderIds.length ? <div className="workspace-map-folder-children">
      <h3>{t("workspaceGraph.folders.children")}</h3>
      {folder.childFolderIds.map((id) => <button className="workspace-map-folder-action" key={id} onClick={() => navigateFolder(id)} type="button">
        {foldersById.get(id)?.label}
      </button>)}
    </div> : null}
    <h3>{t("workspaceGraph.folders.members", { count: folder.direct_member_count })}</h3>
    <WindowedWorkspaceGraphRows
      focusedId={null}
      getId={(point) => point.id}
      hint={t("workspaceGraph.list.virtualKeyboardHint", { count: displayedMembers.length })}
      items={displayedMembers}
      label={t("workspaceGraph.folders.members", { count: folder.direct_member_count })}
      mobileLayout={mobileLayout}
      onBlur={() => {}}
      selectedId={null}
      testId={mobileLayout ? "workspace-map-folder-members-mobile" : "workspace-map-folder-members"}
      renderRow={(point, index, virtualized, rowHeight, onKeyDown) => {
        const source = nodesById.get(point.sourceNodeId);
        return <li aria-posinset={virtualized ? index + 1 : undefined}
          aria-setsize={virtualized ? displayedMembers.length : undefined}
          className={virtualized ? "workspace-graph-virtual-row" : undefined}
          data-map-folder-member={point.id} key={point.id}
          style={virtualized ? { height: rowHeight } : undefined}>
          <button className="workspace-graph-list-select"
            data-list-index={virtualized ? index : undefined}
            onClick={() => onSelect(point.sourceNodeId)} onKeyDown={onKeyDown} type="button">
            <strong><span aria-hidden="true" className={`workspace-map-member-dot is-${point.entityKind}`} />{point.label}</strong>
            <small>{t(`workspaceGraph.enums.${point.entityKind}`)}{point.grain === "fragment"
              ? ` · ${t("workspaceGraph.folders.fragment", { count: point.memberCount })}` : ""}</small>
          </button>
          {source?.grain === "document" ? <Link onClick={onNavigate} to={source.path}>{t("workspaceGraph.list.open")}</Link> : null}
        </li>;
      }}
    />
    {members.length > displayedMembers.length ? <p>{t("workspaceGraph.folders.moreMembers", {
      count: members.length - displayedMembers.length,
    })}</p> : null}
    {bundles.length ? <>
      <h3>{t("workspaceGraph.folders.bundles")}</h3>
      <p>{t("workspaceGraph.folders.bundleLimit")}</p>
      <ul className="workspace-map-folder-bundles">{bundles.map((bundle) => {
        const other = bundle.sourceFolderId === folderId ? bundle.targetFolderId : bundle.sourceFolderId;
        const counts = directions.get(other)!;
        const label = other === null ? t("workspaceGraph.folders.unplaced") : foldersById.get(other)!.label;
        return <li key={other ?? "unplaced"}>
          {other !== null ? <button className="workspace-map-folder-action" onClick={() => navigateFolder(other)} type="button">{label}</button> : <strong>{label}</strong>}
          <span>{t("workspaceGraph.list.relationCount", { count: bundle.relationCount })}</span>
          <small>{t("workspaceGraph.folders.directions", counts)}</small>
        </li>;
      })}</ul>
    </> : null}
    <p>{t("workspaceGraph.folders.primaryOnly")}</p>
  </section>;
}

/** Keep small v1 scenes unchanged; larger Map scenes mount only nearby rows. */
function WindowedWorkspaceGraphRows<Item>({
  focusedId,
  onBlur,
  getId,
  hint,
  items,
  label,
  mobileLayout,
  renderRow,
  selectedId,
  testId,
}: {
  focusedId: string | null;
  onBlur: (id: string) => void;
  getId: (item: Item) => string;
  hint: string;
  items: Item[];
  label: string;
  mobileLayout: boolean;
  renderRow: (
    item: Item,
    index: number,
    virtualized: boolean,
    rowHeight: number,
    onKeyDown?: (event: ReactKeyboardEvent<HTMLButtonElement>) => void,
  ) => ReactNode;
  selectedId: string | null;
  testId: string;
}) {
  const listRef = useRef<HTMLUListElement>(null);
  const lastScrolledSelectionRef = useRef<string | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [focusIndex, setFocusIndex] = useState<number | null>(null);
  const virtualized = items.length > VIRTUAL_LIST_THRESHOLD;
  const rowHeight = mobileLayout ? 112 : 64;
  const viewportHeight = mobileLayout ? 336 : 448;
  const visibleRows = Math.ceil(viewportHeight / rowHeight);
  const maximumScrollTop = virtualized ? Math.max(0, items.length * rowHeight - viewportHeight) : 0;
  const boundedScrollTop = Math.min(maximumScrollTop, Math.max(0, scrollTop));
  const start = virtualized
    ? Math.max(0, Math.floor(boundedScrollTop / rowHeight) - VIRTUAL_LIST_OVERSCAN)
    : 0;
  const end = virtualized
    ? Math.min(items.length, start + visibleRows + VIRTUAL_LIST_OVERSCAN * 2)
    : items.length;
  const hintId = `${testId}-keyboard-hint`;

  useLayoutEffect(() => {
    if (scrollTop === boundedScrollTop) return;
    // Clamp both the first rendered window and the DOM offset when filtering
    // removes rows; otherwise stale scroll state can render an empty spacer.
    if (listRef.current) listRef.current.scrollTop = boundedScrollTop;
    setScrollTop(boundedScrollTop);
  }, [boundedScrollTop, scrollTop]);

  useLayoutEffect(() => {
    if (!virtualized || !focusedId) return;
    const index = items.findIndex((item) => getId(item) === focusedId);
    // Removing a focused DOM row need not dispatch blur in the browser. Do
    // not clear a focus belonging to the other (node/relationship) list.
    if (index >= 0 && (index < start || index >= end)) onBlur(focusedId);
  }, [end, focusedId, getId, items, onBlur, start, virtualized]);

  useEffect(() => {
    if (!virtualized || focusIndex === null) return;
    const button = listRef.current?.querySelector<HTMLButtonElement>(
      `button[data-list-index="${focusIndex}"]`,
    );
    if (button) {
      button.focus();
      setFocusIndex(null);
    }
  }, [end, focusIndex, start, virtualized]);

  useEffect(() => {
    if (lastScrolledSelectionRef.current === selectedId) return;
    lastScrolledSelectionRef.current = selectedId;
    if (!virtualized || !selectedId || !listRef.current) return;
    const index = items.findIndex((item) => getId(item) === selectedId);
    if (index < 0) return;
    const list = listRef.current;
    const rowTop = index * rowHeight;
    const rowBottom = rowTop + rowHeight;
    if (rowTop < list.scrollTop || rowBottom > list.scrollTop + viewportHeight) {
      list.scrollTop = Math.max(0, rowTop - Math.floor(viewportHeight / 2));
      setScrollTop(list.scrollTop);
    }
  }, [getId, items, rowHeight, selectedId, viewportHeight, virtualized]);

  function onVirtualKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    const list = event.currentTarget.closest("ul");
    if (!list) return;
    let next: number;
    switch (event.key) {
      case "ArrowDown": next = index + 1; break;
      case "ArrowUp": next = index - 1; break;
      case "PageDown": next = index + visibleRows; break;
      case "PageUp": next = index - visibleRows; break;
      case "Home": next = 0; break;
      case "End": next = items.length - 1; break;
      default: return;
    }
    event.preventDefault();
    next = Math.min(items.length - 1, Math.max(0, next));
    const rowTop = next * rowHeight;
    const rowBottom = rowTop + rowHeight;
    if (rowTop < list.scrollTop) list.scrollTop = rowTop;
    else if (rowBottom > list.scrollTop + viewportHeight) {
      list.scrollTop = rowBottom - viewportHeight;
    }
    setScrollTop(list.scrollTop);
    setFocusIndex(next);
  }

  return (
    <>
      {virtualized ? <p className="workspace-graph-list-window-hint" id={hintId}>{hint}</p> : null}
      <ul
        aria-describedby={virtualized ? hintId : undefined}
        aria-label={virtualized ? label : undefined}
        className={virtualized ? "workspace-graph-windowed-list" : undefined}
        data-testid={testId}
        data-total-count={items.length}
        onScroll={virtualized ? (event) => setScrollTop(event.currentTarget.scrollTop) : undefined}
        ref={listRef}
        style={virtualized ? { height: viewportHeight } : undefined}
      >
        {virtualized && start > 0 ? (
          <li aria-hidden="true" className="workspace-graph-list-spacer" role="presentation" style={{ height: start * rowHeight }} />
        ) : null}
        {items.slice(start, end).map((item, offset) => {
          const index = start + offset;
          return renderRow(
            item,
            index,
            virtualized,
            rowHeight,
            virtualized ? (event) => onVirtualKeyDown(event, index) : undefined,
          );
        })}
        {virtualized && end < items.length ? (
          <li aria-hidden="true" className="workspace-graph-list-spacer" role="presentation" style={{ height: (items.length - end) * rowHeight }} />
        ) : null}
      </ul>
    </>
  );
}

function WorkspaceGraphCanvasToolbar({
  canvasRef,
  exportEnabled,
  layoutPaused,
  onExport,
  onReset,
  onToggleLayout,
  reducedMotion,
  staticLayout,
}: {
  canvasRef: RefObject<GraphCanvasHandle | null>;
  exportEnabled: boolean;
  layoutPaused: boolean;
  onExport: () => void;
  onReset: () => void;
  onToggleLayout: () => void;
  reducedMotion: boolean;
  staticLayout: boolean;
}) {
  const { t } = useTranslation("graph");
  return (
    <div aria-label={t("workspaceGraph.canvas.aria")} className="graph-canvas-toolbar" role="toolbar">
      <GraphControlButton label={t("workspaceGraph.canvas.zoomIn")} onClick={() => canvasRef.current?.zoomIn()}><ZoomIn aria-hidden="true" size={17} /></GraphControlButton>
      <GraphControlButton label={t("workspaceGraph.canvas.zoomOut")} onClick={() => canvasRef.current?.zoomOut()}><ZoomOut aria-hidden="true" size={17} /></GraphControlButton>
      <GraphControlButton label={t("workspaceGraph.canvas.fit")} onClick={() => canvasRef.current?.fit()}><Maximize2 aria-hidden="true" size={17} /></GraphControlButton>
      <GraphControlButton
        disabled={reducedMotion || staticLayout}
        label={t(staticLayout
          ? "workspaceGraph.canvas.fixedLayout"
          : reducedMotion
            ? "workspaceGraph.canvas.reducedMotionLayout"
            : layoutPaused ? "workspaceGraph.canvas.resume" : "workspaceGraph.canvas.pause")}
        onClick={onToggleLayout}
      >
        {layoutPaused ? <Play aria-hidden="true" size={17} /> : <Pause aria-hidden="true" size={17} />}
      </GraphControlButton>
      <GraphControlButton label={t("workspaceGraph.canvas.reset")} onClick={onReset}><RotateCcw aria-hidden="true" size={17} /></GraphControlButton>
      <GraphControlButton disabled={!exportEnabled} label={t(exportEnabled ? "workspaceGraph.canvas.export" : "workspaceGraph.canvas.exportUnavailable")} onClick={onExport}><Download aria-hidden="true" size={17} /></GraphControlButton>
    </div>
  );
}

function GraphControlButton({
  children,
  disabled = false,
  label,
  onClick,
}: {
  children: ReactNode;
  disabled?: boolean;
  label: string;
  onClick: () => void;
}) {
  return <button aria-label={label} className="graph-icon-button" disabled={disabled} onClick={onClick} title={label} type="button">{children}</button>;
}

function WorkspaceGraphDetails({
  canExpand,
  edge,
  expanding,
  node,
  onCenter,
  onExpand,
  onNavigate,
}: {
  canExpand: boolean;
  edge: WorkspaceGraphEdge | null;
  expanding: boolean;
  node: WorkspaceGraphNode | null;
  onCenter?: (id: string) => void;
  onExpand: (clusterId: string) => void;
  onNavigate: WorkspaceGraphDocumentNavigation;
}) {
  const { t } = useTranslation("graph");
  return (
    <section aria-labelledby="workspace-graph-details-title" className="workspace-graph-details">
      <h2 id="workspace-graph-details-title">{t("workspaceGraph.details.title")}</h2>
      {!node && !edge ? <p>{t("workspaceGraph.details.empty")}</p> : null}
      {node ? (
        <WorkspaceGraphNodeDetails
          canExpand={canExpand}
          expanding={expanding}
          node={node}
          onCenter={onCenter}
          onExpand={onExpand}
          onNavigate={onNavigate}
        />
      ) : null}
      {edge ? (
        <dl>
          <dt>{t("workspaceGraph.details.relationship")}</dt><dd>{workspaceGraphEnumLabel(edge.relation_kind, t)}</dd>
          <dt>{t("workspaceGraph.details.layer")}</dt><dd>{workspaceGraphEnumLabel(edge.layer, t)}</dd>
          <dt>{t("workspaceGraph.details.direction")}</dt><dd>{workspaceGraphEnumLabel(edge.direction, t)}</dd>
          <dt>{t("workspaceGraph.details.relationsRepresented")}</dt><dd>{edge.relation_count}</dd>
          <dt>{t("workspaceGraph.details.crossFolder")}</dt><dd>{t(edge.cross_folder ? "workspaceGraph.details.yes" : "workspaceGraph.details.no")}</dd>
        </dl>
      ) : null}
    </section>
  );
}

function WorkspaceGraphNodeDetails({
  canExpand,
  expanding,
  node,
  onCenter,
  onExpand,
  onNavigate,
}: {
  canExpand: boolean;
  expanding: boolean;
  node: WorkspaceGraphNode;
  onCenter?: (id: string) => void;
  onExpand: (clusterId: string) => void;
  onNavigate: WorkspaceGraphDocumentNavigation;
}) {
  const { t } = useTranslation("graph");
  if (node.grain === "cluster") {
    return (
      <>
        <h3>{node.label}</h3>
        <dl>
          <dt>{t("workspaceGraph.details.kind")}</dt><dd>{workspaceGraphEnumLabel(node.cluster_kind, t)}</dd>
          <dt>{t("workspaceGraph.details.members")}</dt><dd>{node.member_count}</dd>
          <dt>{t("workspaceGraph.details.relations")}</dt><dd>{node.relation_count}</dd>
        </dl>
        {canExpand ? (
          <button className="workspace-graph-details-expand" disabled={expanding} onClick={() => onExpand(node.id)} type="button">
            {t(expanding ? "workspaceGraph.list.expanding" : "workspaceGraph.list.expand")}
          </button>
        ) : null}
        {onCenter ? (
          <div className="workspace-graph-detail-actions">
            <button className="workspace-graph-center-map" onClick={() => onCenter(node.id)} type="button">{t("workspaceGraph.details.centerMap")}</button>
          </div>
        ) : null}
      </>
    );
  }
  return (
    <>
      <h3>{node.label}</h3>
      <dl>
        <dt>{t("workspaceGraph.details.kind")}</dt><dd>{workspaceGraphEnumLabel(node.entity_kind, t)}</dd>
        <dt>{t("workspaceGraph.details.primaryFolder")}</dt><dd>{node.primary_folder_label}</dd>
        <dt>{t("workspaceGraph.details.referencedBy")}</dt><dd>{t("workspaceGraph.details.referencedByValue", { count: node.reference_count })}</dd>
        <dt>{t("workspaceGraph.details.evidence")}</dt><dd>{node.evidence_count}</dd>
        {node.also_appears_in.length ? (
          <><dt>{t("workspaceGraph.details.alsoAppears")}</dt><dd>{node.also_appears_in.map((placement) => placement.label).join(", ")}</dd></>
        ) : null}
      </dl>
      <div className="workspace-graph-detail-actions">
        {onCenter ? <button className="workspace-graph-center-map" onClick={() => onCenter(node.id)} type="button">{t("workspaceGraph.details.centerMap")}</button> : null}
        <Link onClick={onNavigate} to={node.path}>{t("workspaceGraph.details.openDocument")}</Link>
        {node.entity_kind === "context" ? (
          <Link onClick={onNavigate} to={`/context?target=${encodeURIComponent(node.entity_id)}&tab=graph`}>
            {t("workspaceGraph.details.explore")}
          </Link>
        ) : null}
      </div>
    </>
  );
}

function coverageLabel(scene: WorkspaceGraphScene, t: TFunction<"graph">) {
  return t(`workspaceGraph.coverage.${scene.completeness === "complete"
    ? "notice"
    : scene.completeness}`);
}

function omissionMessage(
  reason: WorkspaceGraphScene["omission_reasons"][number],
  t: TFunction<"graph">,
  budget: WorkspaceGraphOverviewRequest["budget"],
) {
  return t(`workspaceGraph.omissions.${reason}`, {
    limit: (reason === "document_budget" ? budget.documents : budget.relations)
      .toLocaleString(i18n.resolvedLanguage || "en"),
  });
}

function sceneWarningMessage(warning: string, t: TFunction<"graph">) {
  switch (warning) {
    case "secondary_placements_capped":
    case "compare_projection_not_available":
    case "growth_projection_not_available":
    case "provenance_projection_not_available":
      return t(`workspaceGraph.warnings.${warning}`);
    default:
      return t("workspaceGraph.warnings.unknown");
  }
}

function workspaceGraphEnumLabel(value: string, t: TFunction<"graph">) {
  switch (value) {
    case "context":
    case "library":
    case "folder_cluster":
    case "link_cluster":
    case "other_cluster":
    case "wikilink":
    case "citation_page":
    case "citation_source":
    case "knowledge":
    case "evidence":
    case "structure":
    case "provenance":
    case "directed":
    case "undirected":
    case "exact":
    case "aggregated":
    case "complete":
    case "capped":
    case "partial":
    case "privacy_suppressed":
      return t(`workspaceGraph.enums.${value}`);
    default:
      return t("workspaceGraph.enums.unknown");
  }
}

function workspaceGraphMetricLabel(value: string, t: TFunction<"graph">) {
  switch (value) {
    case "visible_published_links_and_citations.pre_quick.v1":
      return t("workspaceGraph.metrics.combined");
    case "distinct_visible_published_wikilink_sources.pre_quick.v1":
    case "distinct_visible_published_wikilink_sources.v1":
      return t("workspaceGraph.metrics.links");
    case "visible_published_citation_relations.pre_quick.v1":
    case "visible_published_citation_relations.v1":
      return t("workspaceGraph.metrics.citations");
    case "visible_primary_tree_placement.v1":
      return t("workspaceGraph.metrics.primaryPlacement");
    case "partial_union_of_authored_relations.v1":
      return t("workspaceGraph.metrics.partialRelations");
    case "unavailable_temporal_projection.v1":
      return t("workspaceGraph.metrics.temporalUnavailable");
    case "unavailable_gated_provenance.v1":
      return t("workspaceGraph.metrics.provenanceUnavailable");
    default:
      return t("workspaceGraph.metrics.unknown");
  }
}

function nodeSummary(
  node: WorkspaceGraphNode,
  lens: WorkspaceGraphAvailableLens,
  t: TFunction<"graph">,
) {
  if (node.grain === "cluster") {
    return t("workspaceGraph.list.clusterSummary", {
      kind: workspaceGraphEnumLabel(node.cluster_kind, t),
      members: node.member_count,
      relations: node.relation_count,
    });
  }
  const metric = lens === "combined"
    ? t("workspaceGraph.list.combinedMetric", { references: node.reference_count, evidence: node.evidence_count })
    : lens === "links"
    ? t("workspaceGraph.list.referenceMetric", { count: node.reference_count })
    : lens === "evidence"
      ? t("workspaceGraph.list.evidenceMetric", { count: node.evidence_count })
      : t("workspaceGraph.list.uniformMetric");
  return t("workspaceGraph.list.documentSummary", {
    folder: node.primary_folder_label,
    kind: workspaceGraphEnumLabel(node.entity_kind, t),
    metric,
  });
}

function exportWorkspaceGraph(scene: WorkspaceGraphScene, renderScene: GraphRenderScene) {
  if (!scene.capabilities.export) return;
  const payload = {
    schema_version: scene.schema_version,
    graph_revision: scene.graph_revision,
    representation: scene.representation,
    completeness: scene.completeness,
    nodes: renderScene.nodes,
    edges: renderScene.edges,
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `dtxwiki-knowledge-map-${shortRevision(scene.graph_revision)}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function cloneFilters(filters: WorkspaceGraphDataFilters): WorkspaceGraphDataFilters {
  return {
    ...filters,
    document_kinds: [...filters.document_kinds],
    quick_filters: [...filters.quick_filters],
    facet_rules: filters.facet_rules.map((rule) => ({ ...rule })),
  };
}

function workspaceGraphErrorMessage(error: unknown, fallback: string) {
  return error instanceof ApiError ? apiErrorMessage(error, fallback) : fallback;
}

function shortRevision(revision: string) {
  return revision.length > 12 ? `${revision.slice(0, 12)}…` : revision;
}

function formatSnapshot(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function isoToDateInput(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const year = String(date.getFullYear()).padStart(4, "0");
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function dateInputToIso(value: string, endOfDay: boolean) {
  if (!value) return null;
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) return null;
  const date = endOfDay
    ? new Date(year, month - 1, day, 23, 59, 59, 999)
    : new Date(year, month - 1, day, 0, 0, 0, 0);
  return date.toISOString();
}

function restoreAttribute(
  element: HTMLElement,
  name: string,
  previousValue: string | null,
) {
  if (previousValue === null) element.removeAttribute(name);
  else element.setAttribute(name, previousValue);
}

function isUsableModalOpener(opener: HTMLElement) {
  if (!opener.isConnected) return false;
  if (opener instanceof HTMLButtonElement && opener.disabled) return false;
  if (opener.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
  const style = opener.ownerDocument.defaultView?.getComputedStyle(opener);
  if (style?.display === "none" || style?.visibility === "hidden") return false;
  if (opener.offsetParent !== null || opener.getClientRects().length > 0) return true;
  return opener.ownerDocument.defaultView?.navigator.userAgent.includes("jsdom") ?? false;
}

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, [query]);
  return matches;
}

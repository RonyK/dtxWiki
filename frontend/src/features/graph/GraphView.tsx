import { Clock3, Download, Focus, Layers, List, Maximize2, Minimize2, MoreHorizontal, Network, RefreshCw, RotateCcw, SlidersHorizontal, X } from "lucide-react";
import type { TFunction } from "i18next";
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { MermaidDiagram } from "../pages/MarkdownView";
import { LOCAL_EDGE_VISUAL_KIND_ORDER, localEdgeKindsFor, localEdgeVisualKind } from "./local/edgeKinds";
import { decorateStaticDiagramSvg, graphSceneToMermaid, type StaticDiagramVisuals } from "./local/staticDiagram";
import { GraphCameraControls, GraphPreviewBadge, GraphScopeSelector, GraphTypeSelector, eventInvolves } from "./local/LocalGraphChrome";
import { LocalGraphTimeline, LocalTimelineMark } from "./local/LocalGraphTimeline";
import { timelinePresentation } from "./local/timelinePresentation";
import { historySceneCatalog, renderHistoryScene } from "./local/historyScene";
import { LocalGraphCoverageInfo } from "./local/LocalGraphCoverageInfo";
import { CitationEvidenceSection, type CitationPair } from "./shared/CitationEvidenceSection";
import { analyzeLocalScene, type LocalSceneAnalysis } from "./local/localGraphList";
import { LocalGraphListView, type LocalListMemory } from "./local/LocalGraphListView";
import { LocalNodeMeta, LocalRelationEnds, LocalRelationFlow, LocalSeedPath } from "./local/LocalGraphDetailsParts";
import { useFocusRecovery } from "./local/useFocusRecovery";
import type { LocalNodeVisuals } from "./local/LocalNodeCue";
import { localEdgeKindLabel, type LocalViewContext } from "./local/localRelationViews";
import type { RovingSelectionOrigin as LocalGraphSelectionOrigin } from "./shared/relationList/useRovingList";
import { localSceneScopeKey } from "./local/sceneScope";
import { GraphEdgeCue, GraphNodeCue, GraphSeedCue } from "./shared/GraphCues";
import { GRAPH_NODE_VISUALS, isActionEdgeVisualKind, readGraphTokens, type GraphEdgeVisualKind, type GraphNodeVisualKind } from "./shared/graphTokens";
import { hasSessionDetails, SessionActivityDetails } from "./shared/SessionActivityDetails";
import { isSessionGroupNode, isSessionNode } from "./shared/sessionNodes";
import { sessionRelationSummary, withSessionLabels } from "./sessionActivity";
import type { GraphRenderScene } from "./shared/rendererTypes";
import type {
  GraphEdge,
  GraphDepth,
  GraphRange,
  GraphRelationScope,
  GraphHistoryMode,
  GraphNode,
  GraphQueryPayload,
  GraphTimelineEvent,
} from "../../shared/api/graph";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { domainLabel } from "../../shared/lib/domainLabel";
import { searchResultHref } from "../../shared/lib/searchResultHref";
import {
  GraphCanvas,
  type GraphCanvasHandle,
  type RendererStatus,
} from "./GraphCanvas";
import {
  coAccessSentence,
  deriveGraphScene,
  graphFacets,
  sortedTimeline,
  type GraphFilters,
  type GraphScene,
} from "./graphModel";

type GraphViewProps = {
  active: boolean;
  onCollapse: (node: GraphNode) => Promise<void>;
  onDepthChange: (depth: GraphDepth, relationScope?: GraphRelationScope) => Promise<void>;
  onRefresh?: () => Promise<void>;
  onExpand: (node: GraphNode) => Promise<void>;
  onHistoryModeChange: (mode: GraphHistoryMode) => Promise<void>;
  payload: GraphQueryPayload;
  staticPayload?: GraphQueryPayload;
  workspaceGraphHref?: (node: GraphNode) => string | null;
  // Canonical Context permalinks are workspace-in-path; without the id the
  // list falls back to the query-param form (see searchResultHref).
  workspaceId?: string | null;
};

// Match Mermaid's default parsing limit. Larger Local scenes keep the
// complete text fallback rather than mounting a diagram that cannot render.
const STATIC_DIAGRAM_EDGE_LIMIT = 500;

export function GraphView({
  active,
  onCollapse,
  onDepthChange,
  onRefresh,
  onExpand,
  onHistoryModeChange,
  payload: wirePayload,
  staticPayload: wireStaticPayload = wirePayload,
  workspaceGraphHref,
  workspaceId = null,
}: GraphViewProps) {
  const { t } = useTranslation("graph");
  const { t: tChats } = useTranslation("chats");
  // Conversations are named once here, as their own screens name them.
  const payload = useMemo(() => withSessionLabels(wirePayload, t, tChats), [t, tChats, wirePayload]);
  const staticPayload = useMemo(() => wireStaticPayload === wirePayload ? payload
    : withSessionLabels(wireStaticPayload, t, tChats), [payload, t, tChats, wirePayload, wireStaticPayload]);
  const canvasRef = useRef<GraphCanvasHandle | null>(null);
  const graphRegionRef = useRef<HTMLElement | null>(null);
  const selectionOriginRef = useRef<{
    control: HTMLButtonElement;
    modality: LocalGraphSelectionOrigin["modality"];
    id: string;
    scene: GraphScene;
    workspaceId: string | null;
  } | null>(null);
  const actionGenerationRef = useRef(0);
  const frameRef = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [showFuture, setShowFuture] = useState(false);
  const [hoveredEventId, setHoveredEventId] = useState<string | null>(null);
  const [dismissedCoverageKey, setDismissedCoverageKey] = useState<string | null>(null);
  // Structure-only looks (outline, structure map) are counted but start hidden.
  const [historyVisibility, setHistoryVisibility] = useState({ recorded: true, background: true, referenced: false });
  const [listOpen, setListOpen] = useState(false);
  // The list's search, open groups and pages, kept while the canvas shows.
  const listMemoryRef = useRef<LocalListMemory | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [legendOpen, setLegendOpen] = useState(false);
  useEffect(() => {
    const change = () => setFullscreen(document.fullscreenElement === frameRef.current);
    document.addEventListener("fullscreenchange", change);
    return () => document.removeEventListener("fullscreenchange", change);
  }, []);
  const facets = useMemo(() => {
    const canvasFacets = graphFacets(payload);
    if (staticPayload === payload) return canvasFacets;
    const staticFacets = graphFacets(staticPayload);
    // Both surfaces share filters. Retain direct-only options when a broader
    // canvas budget omits them, including the user's explicit exclusions.
    return Object.fromEntries((Object.keys(canvasFacets) as (keyof GraphFilters)[]).map((key) => [
      key, [...new Set([...canvasFacets[key], ...staticFacets[key]])].sort(),
    ])) as GraphFilters;
  }, [payload, staticPayload]);
  const facetSignature = JSON.stringify(facets);
  const previousFacetsRef = useRef(facets);
  const [filters, setFilters] = useState<GraphFilters>(facets);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [pinnedPositions, setPinnedPositions] = useState<
    Record<string, { x: number; y: number }>
  >({});
  const [pinnedIds, setPinnedIds] = useState<string[]>([]);
  const [rendererStatus, setRendererStatus] = useState<RendererStatus>("loading");
  const [showLabels, setShowLabels] = useState(true);
  const [staticOpen, setStaticOpen] = useState(false);
  const [printing, setPrinting] = useState(false);
  const [timelinePositions, setTimelinePositions] = useState<Record<string, string | null>>({});
  const [playing, setPlaying] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);
  const [announcement, setAnnouncement] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [retryAction, setRetryAction] = useState<{ key: string; action: () => Promise<void> } | null>(null);
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const forcedColors = useMediaQuery("(forced-colors: active)");
  const tokens = useLocalGraphTokens(graphRegionRef, forcedColors);
  const nodeVisuals = useMemo(() => ({ tokens, forcedColors }), [tokens, forcedColors]);
  const staticVisuals = useMemo<StaticDiagramVisuals>(() => ({ tokens, forcedColors }), [tokens, forcedColors]);
  const expansionAvailability = payload.capabilities?.expand === false
    ? "unsupported"
    : payload.query.seeds.length >= 8
      ? "cap"
      : "available";
  const previousExpansionAvailabilityRef = useRef(expansionAvailability);
  const historical = payload.history.mode !== "current";
  const range: GraphRange = historical || payload.query.relation_scope === "direct" ? "direct"
    : payload.query.depth === 2 ? "extended" : "neighbors";
  const depthBusy = Boolean(busyAction?.startsWith("depth:") || busyAction?.startsWith("mode:"));
  const recordedEdgeIds = useMemo(() => new Set([
    ...sortedTimeline(payload).flatMap((event) => [...(event.edge_ids ?? []), ...(event.edge_snapshots ?? []).map((edge) => edge.id)]),
    ...payload.edges.filter((edge) => isActionEdgeVisualKind(localEdgeVisualKind(edge.edge_kind))).map((edge) => edge.id),
  ]), [payload]);
  const referencedCount = useMemo(() => payload.edges.filter((edge) => edge.action_kind === "referenced").length, [payload]);
  const applyHistoryVisibility = useCallback((value: GraphScene): GraphScene => {
    if (!historical) return value;
    const edges = value.edges.filter((edge) => (edge.action_kind !== "referenced" || historyVisibility.referenced)
      && (recordedEdgeIds.has(edge.id) ? historyVisibility.recorded : historyVisibility.background));
    return withoutDetachedSessions(value.nodes, edges, value.edges);
  }, [historical, historyVisibility, recordedEdgeIds]);
  const timeline = useMemo(
    () => localizedGraphTimeline(payload, filters, t, applyHistoryVisibility),
    [applyHistoryVisibility, filters, payload, t],
  );
  const canvasScopeKey = localSceneScopeKey({
    workspaceId, query: payload.query, filters, historyMode: payload.history.mode,
    cursor: null,
  });
  const timelineStateKey = JSON.stringify([workspaceId, payload.query.seeds, payload.history.mode,
    payload.query.depth, payload.query.relation_scope, payload.query.time, payload.query.budget]);
  const coverageKey = `${canvasScopeKey}:${payload.generated_at}`;
  const storedEvent = timelinePositions[timelineStateKey];
  const storedIndex = storedEvent ? timeline.findIndex((event) => event.id === storedEvent) : -1;
  const timelineCursor = !historical ? null : storedEvent === null ? -1
    : storedIndex >= 0 ? storedIndex : timeline.length - 1;
  const visibleCursor = timelineCursor ?? timeline.length - 1;
  const currentEvent = timeline[visibleCursor];
  const setTimelineCursor = useCallback((next: number | null | ((cursor: number | null) => number | null)) => {
    const index = typeof next === "function" ? next(timelineCursor) : next;
    const id = index === null ? timeline.at(-1)?.id : index < 0 ? null : timeline[index]?.id;
    setTimelinePositions((positions) => ({ ...positions, [timelineStateKey]: id ?? null }));
  }, [timelineStateKey, timeline, timelineCursor]);
  const filteredScene = useMemo(
    () => applyHistoryVisibility(deriveGraphScene(payload, filters, timelineCursor)),
    [applyHistoryVisibility, filters, payload, timelineCursor],
  );
  const scene = useMemo(() => localizedGraphScene(filteredScene, t), [filteredScene, t]);
  const historyCatalog = useMemo(() => localizedGraphScene(applyHistoryVisibility(historySceneCatalog(payload, filters)), t),
    [applyHistoryVisibility, filters, payload, t]);
  const sceneAnalysis = useMemo(() => analyzeLocalScene(scene, payload.query.seeds), [payload.query.seeds, scene]);
  // The document the graph is opened from is the primary query seed; seeds
  // added by expanding follow it and are not the current document.
  const currentNode = useMemo(() => {
    const primary = payload.query.seeds[0];
    return primary ? payload.nodes.find((node) => node.entity_kind === primary.kind && node.entity_id === primary.id) ?? null : null;
  }, [payload.nodes, payload.query.seeds]);
  const listContext = useMemo<LocalViewContext>(() => ({
    currentId: currentNode?.id ?? null,
    hrefFor: (node) => nodeHref(node, workspaceId ?? null),
    t,
    visuals: nodeVisuals,
  }), [currentNode, nodeVisuals, t, workspaceId]);
  const canvasScene = useMemo<GraphRenderScene>(() => {
    const hoveredIndex = timeline.findIndex((event) => event.id === hoveredEventId);
    const eventIndex = hoveredIndex >= 0 ? hoveredIndex : visibleCursor;
    const event = timeline[eventIndex];
    // Resolve previews at the event's cursor, independently of playback.
    // Removals use the prior frame; other operations use their resulting frame.
    const eventFrame = historical && event
      ? localizedGraphScene(applyHistoryVisibility(deriveGraphScene(payload, filters,
        eventIndex - (event.operation === "edge_removed" ? 1 : 0))), t)
      : { nodes: [], edges: [] };
    return {
      ...(historical ? renderHistoryScene(withCoAccessLabels(historyCatalog, t), withCoAccessLabels(scene, t),
        withCoAccessLabels(eventFrame, t), event, showFuture, payload.history.mode === "action", hoveredIndex >= 0, event?.summary)
        : withCoAccessLabels(scene, t)),
      scopeKey: canvasScopeKey,
      freezeLayout: historical,
      currentId: currentNode?.id ?? null,
    };
  }, [applyHistoryVisibility, canvasScopeKey, currentNode, filters, historical, historyCatalog, hoveredEventId, payload, scene, showFuture, t, timeline, visibleCursor]);
  const staticScene = useMemo(() => localizedGraphScene(applyHistoryVisibility(
    deriveGraphScene(staticPayload, filters, timelineCursor)), t),
  [applyHistoryVisibility, filters, staticPayload, t, timelineCursor]);
  const staticDiagram = useMemo(
    () => staticOpen && staticScene.edges.length <= STATIC_DIAGRAM_EDGE_LIMIT
      ? graphSceneToMermaid(staticScene, staticVisuals, { iconSlot: true })
      : null,
    [staticScene, staticOpen, staticVisuals],
  );
  const decorateStaticSvg = useCallback(
    (svg: string) => staticDiagram ? decorateStaticDiagramSvg(svg, staticDiagram.nodes, staticVisuals) : svg,
    [staticDiagram, staticVisuals],
  );
  const selectedNode = scene.nodes.find((node) => node.id === selectedId)
    ?? (historical ? historyCatalog.nodes.find((node) => node.id === selectedId) : null) ?? null;
  const selectedEdge = scene.edges.find((edge) => edge.id === selectedId)
    ?? (historical ? historyCatalog.edges.find((edge) => edge.id === selectedId) : null) ?? null;
  const effectiveSelectedId = selectedNode || selectedEdge ? selectedId : null;
  const effectiveFocusedId = focusedId && (
    scene.nodes.some((node) => node.id === focusedId)
    || scene.edges.some((edge) => edge.id === focusedId)
  ) ? focusedId : null;

  useLayoutEffect(() => {
    const selection = selectionOriginRef.current;
    selectionOriginRef.current = null;
    if (!selection || !active || selection.scene !== scene
      || selection.workspaceId !== workspaceId || selection.id !== effectiveSelectedId) return;
    // Keyboard activation hands off and reveals Details. A disappearing
    // pointer row recovers only the Graph root for immediate scoped Escape.
    // Both paths retain original scene/scope and never take newer focus.
    const currentFocus = document.activeElement;
    if (currentFocus !== selection.control
      && !(currentFocus === document.body && !selection.control.isConnected)) return;
    if (selection.control.isConnected
      && selection.control.closest("[hidden], details:not([open])")) return;
    const region = graphRegionRef.current;
    if (!region?.isConnected) return;
    if (selection.modality === "keyboard") {
      region.querySelector<HTMLElement>(".graph-details:not([hidden])")?.focus();
    } else if (!selection.control.isConnected && currentFocus === document.body) {
      region.focus({ preventScroll: true });
    }
  }, [active, effectiveSelectedId, scene, workspaceId]);

  useEffect(() => {
    const previous = previousFacetsRef.current;
    setFilters((current) => ({
      nodeFamilies: syncFacetSelection(
        current.nodeFamilies,
        previous.nodeFamilies,
        facets.nodeFamilies,
      ),
      layers: syncFacetSelection(current.layers, previous.layers, facets.layers),
      actionKinds: syncFacetSelection(
        current.actionKinds,
        previous.actionKinds,
        facets.actionKinds,
      ),
      edgeKinds: syncFacetSelection(
        current.edgeKinds,
        previous.edgeKinds,
        facets.edgeKinds,
      ),
      directions: syncFacetSelection(
        current.directions,
        previous.directions,
        facets.directions,
      ),
      statuses: syncFacetSelection(
        current.statuses,
        previous.statuses,
        facets.statuses,
      ),
      provenances: syncFacetSelection(
        current.provenances,
        previous.provenances,
        facets.provenances,
      ),
    }));
    previousFacetsRef.current = facets;
    // The signature deliberately tracks values, not the memoized object identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [facetSignature]);

  useEffect(() => {
    const visibleIds = new Set([
      ...scene.nodes.map((node) => node.id),
      ...scene.edges.map((edge) => edge.id),
    ]);
    if (selectedId && !visibleIds.has(selectedId) && !(historical && (historyCatalog.nodes.some((node) => node.id === selectedId) || historyCatalog.edges.some((edge) => edge.id === selectedId)))) setSelectedId(null);
    if (focusedId && !visibleIds.has(focusedId)) setFocusedId(null);
  }, [focusedId, historical, historyCatalog, scene, selectedId]);

  useEffect(() => {
    const region = graphRegionRef.current;
    if (!region) return undefined;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (document.fullscreenElement === frameRef.current) {
        event.preventDefault(); void document.exitFullscreen(); return;
      }
      const target = event.target instanceof Element ? event.target.closest("details[open]") : null;
      if (target && target.matches(".graph-type-comparison,.graph-frame-filters,.graph-canvas-more,.graph-details-more,.graph-local-coverage-info")) {
        target.removeAttribute("open"); target.querySelector("summary")?.focus();
        event.preventDefault(); event.stopPropagation(); return;
      }
      if (!effectiveSelectedId && !effectiveFocusedId) return;
      event.preventDefault();
      event.stopPropagation();
      setSelectedId(null);
      setFocusedId(null);
      requestAnimationFrame(() => region.focus());
    };
    region.addEventListener("keydown", handleEscape);
    return () => region.removeEventListener("keydown", handleEscape);
  }, [effectiveFocusedId, effectiveSelectedId]);

  useEffect(() => {
    const closeOutside = (event: PointerEvent) => {
      if (!(event.target instanceof Node)) return;
      for (const details of Array.from(graphRegionRef.current?.querySelectorAll<HTMLDetailsElement>(
        ".graph-type-comparison[open],.graph-frame-filters[open],.graph-canvas-more[open],.graph-details-more[open]",
      ) ?? [])) if (!details.contains(event.target)) details.open = false;
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, []);

  useEffect(() => {
    if (!active) return undefined;
    // Mount synchronously before the browser snapshots the print document.
    // Text needs no Mermaid layout and still works above its 500-edge limit.
    const beforePrint = () => flushSync(() => setPrinting(true));
    const afterPrint = () => setPrinting(false);
    window.addEventListener("beforeprint", beforePrint);
    window.addEventListener("afterprint", afterPrint);
    return () => {
      window.removeEventListener("beforeprint", beforePrint);
      window.removeEventListener("afterprint", afterPrint);
    };
  }, [active]);

  useEffect(() => {
    if (active) return;
    // Context keeps the graph view mounted after first visit so its scene,
    // filters, selection, and pins survive a tab round-trip. Do not leave its
    // timer or WebGL layout alive behind the hidden tab, though.
    canvasRef.current?.pauseLayout();
    setPlaying(false);
    setPrinting(false);
  }, [active]);

  useEffect(() => {
    if (!active || !playing) return undefined;
    if (visibleCursor >= timeline.length - 1) {
      setPlaying(false);
      // The last event remains its observed snapshot, including deletions.
      setAnnouncement(
        t("graph.timeline.completeAnnouncement", {
          defaultValue: "Timeline playback complete.",
        }),
      );
      return undefined;
    }
    const timeout = window.setTimeout(() => {
      setTimelineCursor((cursor) => {
        const current = cursor ?? -1;
        return Math.min(timeline.length - 1, current + 1);
      });
    }, Math.round(900 / playbackSpeed));
    return () => window.clearTimeout(timeout);
  }, [active, playbackSpeed, playing, setTimelineCursor, t, timeline.length, visibleCursor]);

  useEffect(() => {
    if (!active) {
      previousExpansionAvailabilityRef.current = expansionAvailability;
      return;
    }
    if (previousExpansionAvailabilityRef.current === expansionAvailability) return;
    previousExpansionAvailabilityRef.current = expansionAvailability;
    setAnnouncement(
      expansionAvailability === "cap"
        ? t("graph.expandCap", {
            defaultValue:
              "Collapse an expanded page before adding another (maximum 8 seeds).",
          })
        : expansionAvailability === "unsupported"
          ? t("graph.expandUnsupported", {
              defaultValue: "This graph projection does not support expansion.",
            })
          : t("graph.expandAvailable", {
              defaultValue: "Additional Context pages can be expanded again.",
            }),
    );
  }, [active, expansionAvailability, t]);

  const coverageNotice = graphCoverageNotice(payload, t);
  const omissionDescriptions = [...new Set(
    (payload.omissions ?? []).map((omission) => graphOmissionDescription(omission, t)),
  )];
  const hasDisplayQuota = payload.omissions?.some(
    (omission) => omission.reason === "node_quota" || omission.reason === "relation_quota",
  );
  const activeFilters = [
    ...excludedFilters("nodeFamilies", facets.nodeFamilies, filters.nodeFamilies),
    ...excludedFilters("layers", facets.layers, filters.layers),
    ...excludedFilters("actionKinds", facets.actionKinds, filters.actionKinds),
    ...excludedFilters("edgeKinds", facets.edgeKinds, filters.edgeKinds),
    ...excludedFilters("directions", facets.directions, filters.directions),
    ...excludedFilters("statuses", facets.statuses, filters.statuses),
    ...excludedFilters("provenances", facets.provenances, filters.provenances),
  ];
  const relationKinds = useMemo(() => LOCAL_EDGE_VISUAL_KIND_ORDER.map((kind) => {
    const edgeKinds = facets.edgeKinds.filter((edgeKind) => localEdgeVisualKind(edgeKind) === kind);
    return {
      kind,
      edgeKinds,
      count: payload.edges.filter((edge) => localEdgeVisualKind(edge.edge_kind) === kind).length,
      enabled: edgeKinds.length > 0 && edgeKinds.every((edgeKind) => filters.edgeKinds.includes(edgeKind)),
    };
  }), [facets.edgeKinds, filters.edgeKinds, payload.edges]);

  function toggleFilter(key: keyof GraphFilters, value: string) {
    setFilters((current) => {
      const selected = current[key] as readonly string[];
      return {
        ...current,
        [key]: selected.includes(value)
          ? selected.filter((entry) => entry !== value)
          : [...selected, value].sort(),
      } as GraphFilters;
    });
  }

  function toggleRelationKinds(edgeKinds: string[]) {
    setFilters((current) => {
      const selected = new Set(current.edgeKinds);
      const disable = edgeKinds.every((kind) => selected.has(kind));
      for (const kind of edgeKinds) {
        if (disable) selected.delete(kind);
        else selected.add(kind);
      }
      return { ...current, edgeKinds: [...selected].sort() };
    });
  }

  function selectItem(id: string, origin: LocalGraphSelectionOrigin) {
    if (!active || !scene.nodes.some((node) => node.id === id)
      && !scene.edges.some((edge) => edge.id === id)) return;
    const validOrigin = origin.control.getAttribute("data-local-graph-id") === id
      && graphRegionRef.current?.contains(origin.control)
      && !origin.control.closest("[hidden], details:not([open])");
    if (!validOrigin) return;
    const keyboardOrigin = origin.modality === "keyboard"
      && document.activeElement === origin.control;
    selectionOriginRef.current = id !== effectiveSelectedId
      && (origin.modality === "pointer" || keyboardOrigin)
      ? { ...origin, id, scene, workspaceId }
      : null;
    setSelectedId(id);
    setFocusedId(id);
    if (keyboardOrigin && id === effectiveSelectedId) {
      // Both state setters can bail for a same-ID activation. There is no
      // deferred focus work for a later metadata update to replay.
      graphRegionRef.current?.querySelector<HTMLElement>(".graph-details:not([hidden])")
        ?.focus();
    }
  }

  const clearItemFocus = useCallback((id: string) => {
    setFocusedId((current) => current === id ? null : current);
  }, []);

  function clearSelection() {
    selectionOriginRef.current = null;
    setSelectedId(null);
    setFocusedId(null);
    graphRegionRef.current?.focus({ preventScroll: true });
  }

  function selectCanvasItem(id: string | null) {
    selectionOriginRef.current = null;
    if (id === null) clearSelection();
    else {
      setSelectedId(id);
      setFocusedId(null);
    }
    // The WebGL canvas is intentionally absent from the keyboard tab order.
    // Move focus to the graph region so an immediate Escape remains scoped to
    // this non-modal interaction and can restore the prior graph control.
    graphRegionRef.current?.focus({ preventScroll: true });
  }

  function stepTimeline(direction: -1 | 1) {
    setPlaying(false);
    const next = Math.max(
      -1,
      Math.min(timeline.length - 1, visibleCursor + direction),
    );
    setTimelineCursor(next);
    const event = timeline[next];
    setAnnouncement(
      event
        ? t("graph.timeline.stepAnnouncement", {
            defaultValue: "{{position}} of {{total}}: {{label}}",
            label: event.label,
            position: next + 1,
            total: timeline.length,
          })
        : t("graph.timeline.startAnnouncement", {
            defaultValue: "Timeline reset to the starting scene.",
          }),
    );
  }

  function startPlayback() {
    if (timeline.length === 0) return;
    if (reducedMotion) {
      if (visibleCursor >= timeline.length - 1) setTimelineCursor(0);
      else stepTimeline(1);
      setAnnouncement(
        t("graph.timeline.reducedMotionAnnouncement", {
          defaultValue:
            "Reduced motion is enabled. One timeline event was revealed.",
        }),
      );
      return;
    }
    if (visibleCursor >= timeline.length - 1) setTimelineCursor(-1);
    setPlaying(true);
    setAnnouncement(
      t("graph.timeline.playingAnnouncement", {
        defaultValue: "Timeline playback started.",
      }),
    );
  }

  function pausePlayback() {
    setPlaying(false);
    setAnnouncement(
      t("graph.timeline.pausedAnnouncement", {
        defaultValue: "Timeline playback paused.",
      }),
    );
  }

  async function runAction(key: string, action: () => Promise<void>) {
    const generation = ++actionGenerationRef.current;
    setBusyAction(key);
    setActionError(null);
    setRetryAction(null);
    try {
      await action();
    } catch {
      if (generation !== actionGenerationRef.current) return;
      setRetryAction({ key, action });
      setActionError(
        t("graph.actionError", {
          defaultValue: "The graph could not be updated. Try again.",
        }),
      );
    } finally {
      if (generation === actionGenerationRef.current) setBusyAction(null);
    }
  }

  async function changeRange(next: GraphRange) {
    if (historical || next === range) return;
    await runAction(`depth:${next}`, () => onDepthChange(next === "extended" ? 2 : 1, next === "direct" ? "direct" : "neighbors"));
  }

  async function changeHistoryMode(mode: GraphHistoryMode) {
    if (mode === payload.history.mode) return;
    setPlaying(false);
    setHoveredEventId(null);
    await runAction(`mode:${mode}`, () => onHistoryModeChange(mode));
  }

  function seekEvent(index: number) {
    setPlaying(false);
    setHoveredEventId(null);
    setTimelineCursor(index);
  }

  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement === frameRef.current) await document.exitFullscreen();
      else await frameRef.current?.requestFullscreen();
    } catch { setRetryAction(null); setActionError(t("graph.view.fullscreenUnavailable")); }
  }

  function showEventInGraph() {
    const id = currentEvent?.node_ids?.find((id) => canvasScene.nodes.some((node) => node.id === id && !node.hidden))
      ?? currentEvent?.edge_ids?.find((id) => canvasScene.edges.some((edge) => edge.id === id && !edge.hidden));
    if (id) canvasRef.current?.centerOn(id);
  }

  function togglePin(node: GraphNode) {
    if (pinnedIds.includes(node.id)) {
      setPinnedIds((current) => current.filter((id) => id !== node.id));
      setPinnedPositions((current) => {
        const next = { ...current };
        delete next[node.id];
        return next;
      });
      canvasRef.current?.pin(node.id, false);
      return;
    }
    // A keyboard pin asks the adapter to retain the current position; a
    // pointer drag reports and stores its exact position.
    setPinnedIds((current) => [...current, node.id]);
    const position = canvasRef.current?.pin(node.id, true);
    if (position) {
      setPinnedPositions((current) => ({ ...current, [node.id]: position }));
    }
  }

  function resetView() {
    setPinnedPositions({});
    setPinnedIds([]);
    setSelectedId(null);
    setFocusedId(null);
    canvasRef.current?.reset();
  }

  function exportFilteredScene() {
    const blob = new Blob([graphSceneToMermaid(scene, staticVisuals, { iconSlot: false }).source], {
      type: "text/plain;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `dtxwiki-graph-${payload.projection_revision}.mmd`;
    link.click();
    URL.revokeObjectURL(url);
  }

  // Stage status is built once. Over the canvas it floats; in List mode the
  // list replaces the canvas, so the same status joins the list instead and
  // never sits under (or over) the list's own controls.
  const loadingView = busyAction ? <div className="graph-canvas-loading" role="status"><RefreshCw size={20} aria-hidden="true" />
    {t("graph.view.loading", { view: busyAction.startsWith("mode:") ? t(`graph.view.${busyAction.slice(5)}.name`) : t(`graph.view.${payload.history.mode}.name`) })}</div> : null;
  // History views hide relations with their own two chips; those count as filters here.
  const hiddenByChoice = activeFilters.length > 0 || historical && (!historyVisibility.recorded || !historyVisibility.background);
  const emptyView = <>
    <p>{t(hiddenByChoice ? "graph.view.allHidden" : "graph.view.noRelations")}</p>
    {hiddenByChoice ? <button className="inline-action" type="button" onClick={() => {
      setFilters(facets); setHistoryVisibility((state) => ({ ...state, recorded: true, background: true }));
    }}>{t("graph.filters.clearAll")}</button> : historical ? null : <small>{t("graph.view.addRelations")}</small>}
  </>;
  const coverageShown = !!coverageNotice && dismissedCoverageKey !== coverageKey;
  const coverageView = coverageShown ? <div className="graph-canvas-coverage">
    <LocalGraphCoverageInfo active={active} descriptions={omissionDescriptions}
      heading={coverageNotice.heading} messages={coverageNotice.messages} payload={payload} workspaceId={workspaceId}
      folderOnly={coverageNotice.folderOnly} tone={coverageNotice.tone}
      budgetNote={hasDisplayQuota ? t("graph.omissions.appliedBudget", { nodes: payload.budget.applied.nodes, edges: payload.budget.applied.edges }) : undefined} />
    <button type="button" aria-label={t("graph.view.dismissNotice")} onClick={() => setDismissedCoverageKey(coverageKey)}><X size={14} aria-hidden="true" /></button>
  </div> : null;
  // The floating notice stays operable above Details, so Details (panel or
  // expanded sheet) starts below it: the stage publishes the notice's bottom
  // edge. The stage is observed too: its width moves the notice (history, phone).
  useLayoutEffect(() => {
    const stage = stageRef.current;
    const notice = stage?.querySelector<HTMLElement>(":scope > .graph-canvas-coverage");
    if (!stage || !notice || typeof ResizeObserver === "undefined") return undefined;
    const update = () => stage.style.setProperty("--graph-stage-notice-bottom", `${notice.offsetTop + notice.offsetHeight + 8}px`);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(notice);
    observer.observe(stage);
    return () => {
      observer.disconnect();
      stage.style.removeProperty("--graph-stage-notice-bottom");
    };
  }, [coverageShown, historical, listOpen]);
  return (
    <section
      aria-labelledby="graph-view-heading"
      className="graph-view"
      data-testid="graph-view"
      ref={graphRegionRef}
      tabIndex={-1}
    >
      <h3 className="visually-hidden" id="graph-view-heading">{t("graph.heading")}</h3>
      <GraphTypeSelector mode={payload.history.mode} busy={!active || Boolean(busyAction?.startsWith("mode:"))} onChange={(mode) => void changeHistoryMode(mode)} />
      <div className={`graph-frame ${historical ? "graph-frame-history" : ""}`} ref={frameRef}>
        {actionError ? <StatusBanner tone="warning">{actionError}{retryAction ? <button className="inline-action" type="button" onClick={() => {
          void runAction(retryAction.key, retryAction.action);
        }}>{t("graph.view.retry")}</button> : null}</StatusBanner> : null}
        <div className="graph-frame-header">
          <GraphScopeSelector range={range} historical={historical} busy={!active || depthBusy} onChange={(next) => void changeRange(next)} />
          <div className="graph-local-relation-chips" role="group" aria-label={t("graph.localToolbar.relationships")}>
            {!historical ? relationKinds.filter(({ count }) => count > 0).map(({ kind, edgeKinds, count, enabled }) => <button
              aria-label={`${localRelationLabel(t, kind)} ${count}`} aria-pressed={enabled}
              className={`graph-local-relation-chip graph-local-relation-chip-${kind}`} key={kind}
              onClick={() => toggleRelationKinds(edgeKinds)} type="button">
              <GraphEdgeCue className="graph-local-relation-line" color={nodeVisuals.tokens.edge[kind]} kind={kind} width={18} />
              {localRelationLabel(t, kind)}
              <span className="graph-local-relation-count">{count}</span>
            </button>) : (["background", "recorded"] as const).map((kind) => <button key={kind} type="button"
              className={`graph-local-relation-chip graph-local-relation-chip-${kind === "recorded" ? "action" : "link"}`}
              aria-pressed={historyVisibility[kind]} title={kind === "background" ? t("graph.historyUi.baseline") : undefined}
              onClick={() => setHistoryVisibility((state) => ({ ...state, [kind]: !state[kind] }))}>
              <GraphEdgeCue className="graph-local-relation-line" kind={kind === "recorded" ? "action" : "link"} width={18}
                color={nodeVisuals.tokens.edge[kind === "recorded" ? "action" : "link"]} />
              {t(kind === "recorded" ? "graph.historyUi.historyRelations" : "graph.historyUi.currentRelations")}
              {kind === "recorded" ? <GraphPreviewBadge /> : null}
              <span>{kind === "recorded" ? recordedEdgeIds.size : payload.edges.filter((edge) => !recordedEdgeIds.has(edge.id)).length}</span>
            </button>)}
            {historical && referencedCount > 0 ? <button type="button" aria-pressed={historyVisibility.referenced}
              className="graph-local-relation-chip graph-local-relation-chip-actionReference"
              title={t("graph.historyUi.referencedHint")}
              onClick={() => setHistoryVisibility((state) => ({ ...state, referenced: !state.referenced }))}>
              <GraphEdgeCue className="graph-local-relation-line" kind="actionReference" width={18}
                color={nodeVisuals.tokens.edge.actionReference} />
              {t("graph.historyUi.referenced")}
              <span>{referencedCount}</span>
            </button> : null}
          </div>
      <details
        aria-label={t("graph.filters.heading", { defaultValue: "Graph filters" })}
        className="graph-filter-bar graph-filter-options graph-frame-filters"
      >
        <summary title={t("graph.filters.heading")}><SlidersHorizontal size={16} aria-hidden="true" />
          <span>{t("graph.localToolbar.moreFilters", { defaultValue: "More filters" })}</span>
          {" "}
          <span className="graph-filter-active-count" aria-live="polite">
            {t("graph.filters.activeCount", {
              defaultValue: "{{count}} active filters",
              count: activeFilters.length,
            })}
          </span>
        </summary>
        <div className="graph-filter-popover">
          {historical ? <fieldset className="graph-filter-group"><legend>{t("graph.localToolbar.relationships")}</legend><div>
            <label><input type="checkbox" checked={historyVisibility.background} onChange={(e) => setHistoryVisibility((state) => ({ ...state, background: e.target.checked }))} />{t("graph.historyUi.currentRelations")}</label>
            <label><input type="checkbox" checked={historyVisibility.recorded} onChange={(e) => setHistoryVisibility((state) => ({ ...state, recorded: e.target.checked }))} />{t("graph.historyUi.historyRelations")} <GraphPreviewBadge /></label>
            {referencedCount > 0 ? <label><input type="checkbox" checked={historyVisibility.referenced} onChange={(e) => setHistoryVisibility((state) => ({ ...state, referenced: e.target.checked }))} />{t("graph.historyUi.referenced")} ({referencedCount})</label> : null}
          </div></fieldset> : null}
          <div className="graph-filter-summary">
          <button
            className="graph-filter-clear"
            disabled={activeFilters.length === 0}
            onClick={() => setFilters(facets)}
            type="button"
          >
            {t("graph.filters.clearAll", { defaultValue: "Clear all" })}
          </button>
        </div>
        {activeFilters.length ? (
          <>
            <p>{t("graph.omissions.localFilters", {
              defaultValue: "Some items are hidden by your active filters. Clear filters to show them again.",
            })}</p>
            <ul
              aria-label={t("graph.filters.active", { defaultValue: "Active filters" })}
              className="graph-active-filters"
            >
              {activeFilters.map(({ key, value }) => (
                <li key={`${key}:${value}`}>
                  <button
                    aria-label={t("graph.filters.remove", {
                      defaultValue: "Remove filter: without {{value}}",
                      value: graphLabel(t, graphLabelGroupForFilter(key), value),
                    })}
                    className="graph-filter-remove"
                    onClick={() => toggleFilter(key, value)}
                    type="button"
                  >
                    {t("graph.filters.without", {
                      defaultValue: "Without {{value}}",
                      value: graphLabel(t, graphLabelGroupForFilter(key), value),
                    })} ×
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : null}
        <div className="graph-filter-options-grid">
          <GraphFilterGroup
            filterKey="nodeFamilies"
            label={t("graph.filters.nodeFamilies", { defaultValue: "Node type" })}
            options={facets.nodeFamilies}
            selected={filters.nodeFamilies}
            onToggle={toggleFilter}
          />
          <GraphFilterGroup
            filterKey="layers"
            label={t("graph.filters.layers", { defaultValue: "Relationship layer" })}
            options={facets.layers}
            selected={filters.layers}
            onToggle={toggleFilter}
          />
          {facets.actionKinds.length ? (
            <GraphFilterGroup
              filterKey="actionKinds"
              label={t("graph.filters.actions", { defaultValue: "Action" })}
              options={facets.actionKinds}
              selected={filters.actionKinds}
              onToggle={toggleFilter}
            />
          ) : null}
          <GraphFilterGroup
            filterKey="edgeKinds"
            label={t("graph.filters.edgeKinds", { defaultValue: "Relationship type" })}
            options={facets.edgeKinds}
            selected={filters.edgeKinds}
            onToggle={toggleFilter}
          />
          <GraphFilterGroup
            filterKey="directions"
            label={t("graph.filters.directions", { defaultValue: "Direction" })}
            options={facets.directions}
            selected={filters.directions}
            onToggle={toggleFilter}
          />
          <GraphFilterGroup
            filterKey="statuses"
            label={t("graph.filters.statuses", { defaultValue: "Node status" })}
            options={facets.statuses}
            selected={filters.statuses}
            onToggle={toggleFilter}
          />
          <GraphFilterGroup
            filterKey="provenances"
            label={t("graph.filters.provenances", { defaultValue: "Provenance" })}
            options={facets.provenances}
            selected={filters.provenances}
            onToggle={toggleFilter}
          />
        </div>
        </div>
      </details>

        </div>
      <div className={`graph-local-stage ${busyAction ? "is-loading" : ""} ${listOpen ? "is-list-view" : ""}`} aria-busy={!!busyAction} ref={stageRef}>
        {!listOpen ? loadingView : null}
        {!listOpen && !busyAction && !historical && scene.edges.length === 0 ? <div className="graph-canvas-empty" role="status">{emptyView}</div> : null}
        <div className="graph-canvas-context">
          {currentNode ? <button aria-label={t("graph.view.locateCurrent", { label: currentNode.label })} className="graph-seed-pill"
            onClick={() => canvasRef.current?.locate(currentNode.id)} title={t("graph.view.locateCurrent", { label: currentNode.label })} type="button">
            <GraphSeedCue color={tokens.node.context} />{t("graph.view.seed")} <strong>{currentNode.label}</strong>
            {payload.query.seeds.length > 1 ? <small>+{payload.query.seeds.length - 1}</small> : null}
          </button> : null}
          {historical ? <span className="graph-time-pill"><Clock3 size={16} aria-hidden="true" />
            {t("graph.historyUi.recordedAt")} <strong>{currentEvent ? formatVersionTimestamp(currentEvent.occurred_at) : t("graph.timeline.start")}</strong>
            {Math.max(0, visibleCursor + 1)}/{timeline.length}</span> : null}
        </div>
        <div className="graph-canvas-utilities" role="toolbar" aria-label={t("graph.view.utilities")}>
          <GraphIconButton disabled={!active || !!busyAction || !onRefresh} label={t("graph.view.refresh")} onClick={() => {
            setPlaying(false); if (onRefresh) void runAction("refresh", onRefresh);
          }}><RefreshCw size={17} aria-hidden="true" className={busyAction === "refresh" ? "graph-refreshing" : undefined} /></GraphIconButton>
          <GraphIconButton className="graph-fullscreen-control" label={t(fullscreen ? "graph.view.exitFullscreen" : "graph.view.fullscreen")} onClick={() => void toggleFullscreen()}>
            {fullscreen ? <Minimize2 size={17} aria-hidden="true" /> : <Maximize2 size={17} aria-hidden="true" />}
          </GraphIconButton>
          <details className="graph-canvas-more"><summary aria-label={t("graph.view.more")} title={t("graph.view.more")}><MoreHorizontal size={17} aria-hidden="true" /></summary>
            <div>
              <button className="graph-mobile-fullscreen" type="button" onClick={() => void toggleFullscreen()}><Maximize2 size={16} aria-hidden="true" />{t(fullscreen ? "graph.view.exitFullscreen" : "graph.view.fullscreen")}</button>
              {/* Camera, labels and future items only change the drawing: List mode leaves them out. */}
              {!listOpen ? <button type="button" onClick={resetView}><RotateCcw size={16} aria-hidden="true" />{t("graph.resetView")}</button> : null}
              <button type="button" disabled={payload.capabilities?.export === false} onClick={exportFilteredScene}><Download size={16} aria-hidden="true" />{t("graph.export")}</button>
              {!fullscreen ? <button type="button" onClick={() => { setStaticOpen(true); requestAnimationFrame(() => graphRegionRef.current?.querySelector(".graph-static-fallback")?.scrollIntoView({ block: "nearest" })); }}>{t("graph.view.openStatic")}</button> : null}
              {!listOpen ? <label><input type="checkbox" checked={showLabels} onChange={(e) => setShowLabels(e.target.checked)} />{t("graph.localToolbar.labels")}</label> : null}
              {historical && !listOpen ? <label><input type="checkbox" checked={showFuture} onChange={(e) => setShowFuture(e.target.checked)} />{t("graph.historyUi.showFuture")}</label> : null}
            </div>
          </details>
        </div>
        {!listOpen ? coverageView : null}
        {listOpen ? <div className="graph-list-layer">
          <LocalGraphListView active={active} analysis={sceneAnalysis} context={listContext} empty={emptyView}
            memory={listMemoryRef} memoryKey={JSON.stringify([workspaceId, payload.query.seeds[0] ?? null])}
            notice={loadingView || coverageView ? <>{loadingView}{coverageView}</> : null} onBlur={clearItemFocus}
            onFocus={setFocusedId} onSelect={selectItem} scopeKey={canvasScopeKey} selectedId={effectiveSelectedId} />
        </div> : null}
        <GraphCameraControls disabled={rendererStatus !== "ready"} onZoomIn={() => canvasRef.current?.zoomIn()} onZoomOut={() => canvasRef.current?.zoomOut()} onFit={() => canvasRef.current?.fit()} />
        {canvasScene.nodes.length ? (
          <GraphCanvas
          ref={canvasRef}
          viewportInsets={() => {
            const stage = graphRegionRef.current?.querySelector(".graph-local-stage")?.getBoundingClientRect();
            const detail = graphRegionRef.current?.querySelector(".graph-details:not([hidden])")?.getBoundingClientRect();
            // Details is a bottom sheet in a narrow frame (fullscreen too), a side panel otherwise.
            const sheet = !!stage && !!detail && detail.width > stage.width * .75;
            return { top: historical ? 90 : 60, left: 0,
              right: detail && !sheet ? detail.width + 24 : 0,
              bottom: detail && sheet ? detail.height + 12 : 0 };
          }}
          active={active}
          focusedId={effectiveFocusedId}
          forcedColors={forcedColors}
          // The adapter owns transient pointer hover. Feeding it back as
          // keyboard focus makes edge hover replace a selected node's star.
          onHover={() => undefined}
          onPin={(id, position) => {
            setPinnedIds((current) => current.includes(id) ? current : [...current, id]);
            setPinnedPositions((current) => ({ ...current, [id]: position }));
          }}
          onRendererStatusChange={(nextStatus) => {
            setRendererStatus(nextStatus);
            if (nextStatus === "error") setStaticOpen(true);
          }}
          onSelect={selectCanvasItem}
          pinnedIds={pinnedIds}
          pinnedPositions={pinnedPositions}
          reducedMotion={reducedMotion}
          scene={canvasScene}
          selectedId={effectiveSelectedId}
          showLabels={showLabels}
          />
        ) : (
          <p className="graph-empty-state">
            {t("graph.emptyFiltered", {
              defaultValue: "No nodes match the current filter and timeline position.",
            })}
          </p>
        )}
        <GraphDetails
          active={active}
          citationEvidenceActive={active && !historical && timelineCursor === null}
          citationEvidenceRevision={payload.projection_revision}
          historyEvents={historical ? timeline : undefined}
          historyEdges={historyCatalog.edges}
          historyNodes={historical ? historyCatalog.nodes : undefined}
          historicalOnly={historical && !!selectedId && !scene.nodes.some((node) => node.id === selectedId) && !scene.edges.some((edge) => edge.id === selectedId)}
          currentEventId={currentEvent?.id}
          onSeekEvent={(id) => seekEvent(timeline.findIndex((event) => event.id === id))}
          busyAction={busyAction}
          edge={selectedEdge}
          node={selectedNode}
          nodes={scene.nodes}
          analysis={sceneAnalysis}
          listContext={listContext}
          onCenter={(id) => canvasRef.current?.centerOn(id)}
          onClearSelection={clearSelection}
          onBlur={clearItemFocus}
          onCollapse={(node) => runAction(`collapse:${node.id}`, () => onCollapse(node))}
          onExpand={(node) => runAction(`expand:${node.id}`, () => onExpand(node))}
          onFocus={setFocusedId}
          onSelect={selectItem}
          onTogglePin={togglePin}
          pinnedIds={pinnedIds}
          querySeeds={payload.query.seeds}
          supportsExpand={payload.capabilities?.expand !== false}
          workspaceGraphHref={workspaceGraphHref}
          workspaceId={workspaceId}
        />
      </div>

        {historical ? <LocalGraphTimeline cursor={visibleCursor} onCursorChange={seekEvent} restoredLatest={!!storedEvent && storedIndex < 0}
          onHoverEvent={setHoveredEventId} onPause={pausePlayback} onPlay={startPlayback}
          onSpeedChange={setPlaybackSpeed} onStep={stepTimeline} onShowEvent={showEventInGraph}
          payload={payload} playing={playing} reducedMotion={reducedMotion} selectedId={selectedId}
          speed={playbackSpeed} timeline={timeline} /> : null}
        <footer className="graph-frame-status">
          <strong>{t(`graph.range.${range}.name`)} · {t(payload.history.mode === "action" ? "graph.range.actionDescription" : `graph.range.${range}.description`)}</strong>
          <span className="graph-view-count" aria-live={playing ? "off" : "polite"}>{t("graph.visibleCount", { nodes: scene.nodes.length, edges: scene.edges.length })}</span>
          <time dateTime={payload.generated_at}>{t("graph.view.fetched", { time: formatVersionTimestamp(payload.generated_at) })}</time>
          {coverageNotice ? <span className="graph-status-omitted">{t("graph.view.omitted")}</span> : null}
          <div className="graph-status-actions">
            <span className="graph-view-switch" role="group" aria-label={t("graph.viewMode.group")}>
              <button type="button" aria-pressed={!listOpen} onClick={() => setListOpen(false)}><Network size={15} aria-hidden="true" />{t("graph.viewMode.graph")}</button>
              <button type="button" aria-pressed={listOpen} onClick={() => setListOpen(true)}><List size={15} aria-hidden="true" />{t("graph.viewMode.list")}</button>
            </span>
            <button type="button" aria-expanded={legendOpen} onClick={() => setLegendOpen((open) => !open)}><Layers size={15} aria-hidden="true" />{t("graph.view.legend")}</button>
          </div>
        </footer>
        <div hidden={!legendOpen}><GraphLegend historical={historical} nodeVisuals={nodeVisuals} /></div>
      </div>

      <p className="visually-hidden" aria-live="polite" aria-atomic="true">{announcement}</p>


      <details
        className="graph-static-fallback"
        open={staticOpen}
        onToggle={(event) => setStaticOpen(event.currentTarget.open)}
      >
        <summary>
          {rendererStatus === "error"
            ? t("graph.staticFallbackFailed", { defaultValue: "Static graph fallback" })
            : t("graph.staticFallback", { defaultValue: "Static graph view" })}
        </summary>
        {staticOpen ? staticDiagram !== null ? (
          <MermaidDiagram decorateSvg={decorateStaticSvg} source={staticDiagram.source} />
        ) : (
          <>
            <p>{t("graph.staticFallbackList", {
              defaultValue: "This scene exceeds the diagram limit of {{limit}} relationships. All nodes and relationships are listed here.",
              limit: STATIC_DIAGRAM_EDGE_LIMIT,
            })}</p>
            <GraphTextContents scene={staticScene} />
          </>
        ) : null}
      </details>

      <section
        aria-hidden="true"
        className="graph-print-fallback"
        data-testid="graph-print-fallback"
      >
        <h4>{t("graph.printFallback", { defaultValue: "Graph relationships" })}</h4>
        {printing ? <GraphTextContents scene={scene} /> : null}
      </section>
    </section>
  );
}

function GraphTextContents({ scene }: { scene: GraphScene }) {
  const { t } = useTranslation("graph");
  const nodeLabels = new Map(scene.nodes.map((node) => [node.id, node.label]));
  return (
    <div className="graph-text-contents">
      <h5>{t("graph.nodes", { defaultValue: "Nodes" })}</h5>
      <ul aria-label={t("graph.nodes", { defaultValue: "Nodes" })}>
        {scene.nodes.map((node) => (
          <li key={node.id}>
            <strong>{node.label}</strong>{" · "}
            {graphLabel(t, "graphEntityKind", node.entity_kind)}
          </li>
        ))}
      </ul>
      <h5>{t("graph.edges", { defaultValue: "Relationships" })}</h5>
      <ul aria-label={t("graph.edges", { defaultValue: "Relationships" })}>
        {scene.edges.map((edge) => (
          <li key={edge.id}>
            {nodeLabels.get(edge.source) ?? edge.source}{" —"}{edge.label}
            {edge.direction === "undirected" ? "— " : "→ "}
            {nodeLabels.get(edge.target) ?? edge.target}
          </li>
        ))}
      </ul>
    </div>
  );
}

function GraphFilterGroup({
  filterKey,
  label,
  onToggle,
  options,
  selected,
}: {
  filterKey: keyof GraphFilters;
  label: string;
  onToggle: (key: keyof GraphFilters, value: string) => void;
  options: readonly string[];
  selected: readonly string[];
}) {
  const { t } = useTranslation("graph");
  if (!options.length) return null;
  return (
    <fieldset className="graph-filter-group">
      <legend>{label}</legend>
      <div>
        {options.map((option) => (
          <label key={option}>
            <input
              checked={selected.includes(option)}
              onChange={() => onToggle(filterKey, option)}
              type="checkbox"
            />
            <span>{graphLabel(t, graphLabelGroupForFilter(filterKey), option)}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

const LEGEND_NODE_KINDS = {
  current: ["context", "agent", "library", "conversation", "folder"],
  history: ["context", "version", "agent", "library", "conversation", "folder"],
} as const satisfies Record<string, readonly GraphNodeVisualKind[]>;
const LEGEND_TIMELINE_MARKS = ["version", "ask", "tool", "review", "activity", "added", "removed"] as const;
// Current relations draw no activity verb; history draws each verb's stroke.
const LEGEND_EDGE_KINDS = {
  current: ["link", "citation", "coAccess", "membership", "action"],
  history: LOCAL_EDGE_VISUAL_KIND_ORDER,
} as const satisfies Record<string, readonly GraphEdgeVisualKind[]>;

/**
 * A fixed legend per mode: every glyph and line this mode can draw, in one
 * order, whatever the current scene happens to contain. Each line style
 * carries the relationship names it draws.
 */
function GraphLegend({ historical, nodeVisuals }: { historical: boolean; nodeVisuals: LocalNodeVisuals }) {
  const { t } = useTranslation("graph");
  const { tokens, forcedColors } = nodeVisuals;
  const relationNames = (kind: GraphEdgeVisualKind) => {
    if (kind === "action") return t(historical ? "graph.legend.edgeNote.actionOther" : "graph.legend.edgeNote.action");
    if (isActionEdgeVisualKind(kind)) {
      return localEdgeKindsFor(kind).map((actionKind) => graphLabel(t, "graphActionKind", actionKind)).join(" · ");
    }
    const names = [...new Set(localEdgeKindsFor(kind).map((edgeKind) => graphLabel(t, "graphEdgeKind", edgeKind)))]
      .filter((name) => name !== t(`graph.legend.edge.${kind}`));
    return names.join(" · ");
  };
  return (
    <div className="graph-legend" data-legend-mode={historical ? "history" : "current"}>
      <h4 className="visually-hidden">{t("graph.legend.heading", { defaultValue: "Legend" })}</h4>
      <div className="graph-legend-groups">
        <ul aria-label={t("graph.legend.nodes", { defaultValue: "Node types" })}>
          <li data-node-visual-kind="seed">
            <GraphSeedCue color={tokens.node.context} />
            {t("graph.legend.node.seed")}
          </li>
          {LEGEND_NODE_KINDS[historical ? "history" : "current"].map((kind) => (
            <li data-node-visual-kind={kind} key={kind}>
              <GraphNodeCue className="graph-local-node-cue" color={tokens.node[GRAPH_NODE_VISUALS[kind].color]}
                forcedColors={forcedColors} kind={kind} surface={tokens.surface} />
              {t(`graph.legend.node.${kind}`)}
            </li>
          ))}
        </ul>
        <ul aria-label={t("graph.legend.edges", { defaultValue: "Relationship line styles" })} className="graph-legend-stack">
          {LEGEND_EDGE_KINDS[historical ? "history" : "current"].map((kind) => {
            const names = relationNames(kind);
            const label = historical && kind === "action" ? "actionOther" : kind;
            return (
              <li data-edge-visual-kind={kind} key={kind}>
                <GraphEdgeCue color={tokens.edge[kind]} kind={kind} />
                <span>{t(`graph.legend.edge.${label}`)}{names ? <small>{names}</small> : null}</span>
              </li>
            );
          })}
          {historical ? (
            <li data-edge-visual-kind="removed">
              <GraphEdgeCue kind="removed" />
              <span>{t("graph.legend.edge.removed")}</span>
            </li>
          ) : null}
        </ul>
        {historical ? (
          <ul aria-label={t("graph.legend.history", { defaultValue: "History marks" })} className="graph-legend-stack">
            <li data-legend-mark="event-target">
              <GraphNodeCue className="graph-local-node-cue" color={tokens.node.library} forcedColors={forcedColors}
                kind="library" ring={tokens.highlight} surface={tokens.surface} />
              {t("graph.legend.eventTarget")}
            </li>
            {LEGEND_TIMELINE_MARKS.map((kind) => (
              <li data-legend-mark={`timeline-${kind}`} key={kind}>
                <LocalTimelineMark kind={kind} />
                {t(`graph.historyUi.kind.${kind}`)}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </div>
  );
}



function GraphDetails({
  active,
  historyEvents,
  historyEdges,
  historyNodes,
  historicalOnly,
  currentEventId,
  onSeekEvent,
  citationEvidenceActive,
  citationEvidenceRevision,
  busyAction,
  edge,
  node,
  nodes,
  analysis,
  listContext,
  onCenter,
  onClearSelection,
  onBlur,
  onCollapse,
  onExpand,
  onFocus,
  onSelect,
  onTogglePin,
  pinnedIds,
  querySeeds,
  supportsExpand,
  workspaceGraphHref,
  workspaceId,
}: {
  active: boolean;
  historyEvents?: GraphTimelineEvent[];
  historyEdges: GraphEdge[];
  historyNodes?: GraphNode[];
  historicalOnly: boolean;
  currentEventId?: string;
  onSeekEvent: (id: string) => void;
  citationEvidenceActive: boolean;
  citationEvidenceRevision: number;
  busyAction: string | null;
  edge: GraphEdge | null;
  node: GraphNode | null;
  nodes: GraphNode[];
  analysis: LocalSceneAnalysis;
  listContext: LocalViewContext;
  onCenter: (id: string) => void;
  onClearSelection: () => void;
  onBlur: (id: string) => void;
  onCollapse: (node: GraphNode) => void;
  onExpand: (node: GraphNode) => void;
  onFocus: (id: string) => void;
  onSelect: (id: string, origin: LocalGraphSelectionOrigin) => void;
  onTogglePin: (node: GraphNode) => void;
  pinnedIds: string[];
  querySeeds: GraphQueryPayload["query"]["seeds"];
  supportsExpand: boolean;
  workspaceGraphHref?: (node: GraphNode) => string | null;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("graph");
  const detailsRef = useRef<HTMLElement>(null);
  // A timeline step or refresh can drop the focused row from Details: focus
  // the same item if it is still here, else the panel itself.
  useFocusRecovery(detailsRef, () => detailsRef.current, node?.id ?? edge?.id ?? null);
  const [sheetExpanded, setSheetExpanded] = useState(false);
  const actionReasonId = useId();
  useLayoutEffect(() => {
    // A newly selected object starts at its title and actions. Keep the
    // sidebar's scroll position for ordinary updates to the same selection;
    // never scroll the outer reader or move keyboard/pointer focus here.
    if (detailsRef.current) detailsRef.current.scrollTop = 0;
    setSheetExpanded(false);
  }, [node?.id, edge?.id]);
  useLayoutEffect(() => {
    // Expanding upward from a scrolled canvas must keep the close control
    // and the revealed actions within the reader viewport.
    if (sheetExpanded) detailsRef.current?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [sheetExpanded]);
  if (!node && !edge) {
    return (
      <aside className="graph-details" aria-labelledby="graph-details-heading" hidden ref={detailsRef} tabIndex={-1}>
        <h4 id="graph-details-heading">{t("graph.details.heading", { defaultValue: "Details" })}</h4>
        <p>{t("graph.details.empty", { defaultValue: "Select a node or relationship to inspect it." })}</p>
      </aside>
    );
  }
  const evidence = node?.evidence ?? edge?.evidence;
  const href = node ? nodeHref(node, workspaceId) : null;
  const coAccess = edge ? coAccessSentence(edge, t) : null;
  const sessionEdge = edge && isSessionNode(nodes.find((item) => item.id === edge.source))
    ? sessionRelationSummary(edge, t, (value) => formatVersionTimestamp(value)) : null;
  const detailEdges = [...new Map([...analysis.edges, ...historyEdges].map((entry) => [entry.id, entry])).values()];
  const citationSource = edge && ["citation_page", "citation_source", "citation_agent"].includes(edge.edge_kind)
    ? nodes.find((item) => item.id === edge.source && item.entity_kind === "context_page") : undefined;
  const citationTarget = citationSource && edge ? nodes.find((item) => item.id === edge.target) : undefined;
  const citationPair: CitationPair | null = citationSource && citationTarget && (citationTarget.entity_kind === "context_page" || citationTarget.entity_kind === "library_item" || citationTarget.entity_kind === "agent_page")
    ? { source_page_id: citationSource.entity_id, target_kind: citationTarget.entity_kind, target_id: citationTarget.entity_id } : null;
  const nodeActions = node ? selectedNodeActions({
    busyAction, node, querySeeds, supportsExpand, t, workspaceGraphHref,
  }) : null;
  const clearLabel = t("graph.details.clearSelection", { defaultValue: "Clear selection" });
  return (
    <aside className={`graph-details ${sheetExpanded ? "is-sheet-expanded" : ""}`} aria-labelledby="graph-details-heading" ref={detailsRef} tabIndex={-1}>
      <div className="graph-details-title-row">
        <h4 id="graph-details-heading">{t("graph.details.heading")}</h4>
        <button aria-label={clearLabel} className="graph-details-close" onClick={onClearSelection} type="button"><X size={17} aria-hidden="true" /></button>
      </div>
      <button className="graph-details-sheet-toggle" type="button" aria-expanded={sheetExpanded} onClick={() => setSheetExpanded((value) => !value)}>{t(sheetExpanded ? "graph.details.collapseSheet" : "graph.details.expandSheet")}</button>
      <h5 className="graph-details-title">{node?.label ?? (edge ? localEdgeKindLabel(edge, t) : null)}</h5>
      {historicalOnly ? <p className="graph-history-inactive">{t("graph.historyUi.notPresent")}</p> : null}
      {node ? <LocalNodeMeta context={listContext} node={node} /> : null}
      {nodeActions?.reason ? <p className="graph-action-reason" id={actionReasonId}>{nodeActions.reason}</p> : null}
      {edge ? <LocalRelationEnds analysis={analysis} catalog={historyNodes} context={listContext} edge={edge} onSelect={onSelect} /> : null}
      {coAccess ? <p className="graph-edge-summary">{coAccess}</p> : null}
      {sessionEdge ? <p className="graph-edge-summary">{sessionEdge}</p> : null}
      {node && hasSessionDetails(node) ? <SessionActivityDetails active={active} edges={detailEdges} href={href}
        node={node} nodes={nodes} workspaceId={workspaceId} /> : null}
      {node?.records?.length ? <GraphNodeRecords records={node.records} /> : null}
      {/* Expanded documents are seeds too; their path from the current document
          is how a reader reaches the relations between them outside the canvas. */}
      {node ? <LocalSeedPath analysis={analysis} context={listContext} node={node} onSelect={onSelect} /> : null}
      {historyEvents ? <div className="graph-item-history">
        <h5>{t("graph.historyUi.itemRecords")} <GraphPreviewBadge /></h5>
        <ul>{historyEvents.filter((event) => eventInvolves(event, node?.id ?? edge?.id ?? null, historyEdges)).map((event) => <li key={event.id}>
          <button type="button" aria-current={currentEventId === event.id ? "step" : undefined} onClick={() => onSeekEvent(event.id)}>
            <time dateTime={event.occurred_at}>{formatVersionTimestamp(event.occurred_at)}</time><span>{event.label}</span><small>{t("graph.historyUi.seek")}</small>
          </button>
        </li>)}</ul>
        {!historyEvents.some((event) => eventInvolves(event, node?.id ?? edge?.id ?? null, historyEdges)) ? <p>{t("graph.historyUi.noItemRecords")}</p> : null}
      </div> : null}
      <dl>
        {node ? <>{node.actor_label ? <GraphDetail label={t("graph.details.actor", { defaultValue: "Actor" })} value={node.actor_label} /> : null}</> : null}
        {edge ? <><GraphDetail label={t("graph.details.layer", { defaultValue: "Layer" })} value={graphLabel(t, "graphLayer", edge.layer)} /><GraphDetail label={t("graph.details.relationship", { defaultValue: "Relationship" })} value={graphLabel(t, "graphEdgeKind", edge.edge_kind)} />{edge.action_kind ? <GraphDetail label={t("graph.details.action", { defaultValue: "Action" })} value={graphLabel(t, "graphActionKind", edge.action_kind)} /> : null}<GraphDetail label={t("graph.details.direction", { defaultValue: "Direction" })} value={graphLabel(t, "graphDirection", edge.direction)} /><GraphDetail label={t("graph.details.provenance", { defaultValue: "Provenance" })} value={graphLabel(t, "graphProvenance", edge.provenance)} />{edge.aggregate && !coAccess ? <GraphDetail label={t("graph.details.aggregate", { defaultValue: "Aggregate" })} value={String(edge.count ?? edge.weight)} /> : null}</> : null}
        {(node?.occurred_at || edge?.occurred_at) ? <GraphDetail label={t("graph.details.time", { defaultValue: "Time" })} value={formatVersionTimestamp((node?.occurred_at ?? edge?.occurred_at)!, "long")} /> : null}
        {evidence ? <><GraphDetail label={t("graph.details.evidence", { defaultValue: "Evidence" })} value={evidence.label ?? graphLabel(t, "graphEvidenceKind", evidence.kind)} /><GraphDetail label={t("graph.details.evidenceRef", { defaultValue: "Evidence reference" })} value={evidence.available ? evidence.ref ?? evidence.id ?? t("graph.details.available", { defaultValue: "Available" }) : t("graph.details.unavailable", { defaultValue: "Unavailable" })} /></> : null}
      </dl>
      {workspaceId && citationPair ? <CitationEvidenceSection active={citationEvidenceActive} pair={citationPair} revision={citationEvidenceRevision} workspaceId={workspaceId} /> : null}
      {node ? <LocalRelationFlow active={active} analysis={analysis} context={listContext} key={node.id} node={node}
        onBlur={onBlur} onFocus={onFocus} onSelect={onSelect} scopeKey={`${workspaceId ?? ""}:${node.id}`} /> : null}
      <div className="graph-details-footer">
        {href ? <Link className="graph-details-open" to={href}>{t("graph.open")}</Link> : null}
        {node && nodeActions ? <GraphSelectedNodeActions actions={{ ...nodeActions, href: null }} node={node} onCollapse={onCollapse} onExpand={onExpand} reasonId={actionReasonId} /> : null}
        <details className="graph-details-more" onToggle={(event) => { if (event.currentTarget.open) setSheetExpanded(true); }}><summary aria-label={t("graph.view.more")}><MoreHorizontal size={17} aria-hidden="true" /></summary><div>
          <button type="button" onClick={(event) => {
            event.currentTarget.closest("details")?.removeAttribute("open");
            setSheetExpanded(false);
            const id = (node ?? edge)!.id;
            requestAnimationFrame(() => onCenter(id));
          }}>{t("graph.details.center")}</button>
          {node ? <button type="button" onClick={() => onTogglePin(node)}>{t(pinnedIds.includes(node.id) ? "graph.details.unpin" : "graph.details.pin")}</button> : null}
          {nodeActions?.href ? <Link to={nodeActions.href}>{t("graph.showInKnowledgeMap")}</Link> : null}
        </div></details>
      </div>
    </aside>
  );
}


type SelectedNodeActions = {
  action: "expand" | "collapse";
  disabled: boolean;
  href: string | null;
  reason: string | undefined;
};

/** Policy is computed once for both the controls and their full-body reason. */
function selectedNodeActions({
  busyAction, node, querySeeds, supportsExpand, t, workspaceGraphHref,
}: {
  busyAction: string | null;
  node: GraphNode;
  querySeeds: GraphQueryPayload["query"]["seeds"];
  supportsExpand: boolean;
  t: TFunction;
  workspaceGraphHref?: (node: GraphNode) => string | null;
}): SelectedNodeActions {
  const seedKey = `${node.entity_kind}:${node.entity_id}`;
  const primary = querySeeds[0];
  const isPrimary = Boolean(primary && `${primary.kind}:${primary.id}` === seedKey);
  const isExpanded = !isPrimary && querySeeds.some((seed) => `${seed.kind}:${seed.id}` === seedKey);
  const isContextPage = node.entity_kind === "context_page";
  const atSeedCap = querySeeds.length >= 8;
  const canCollapse = supportsExpand && isContextPage && isExpanded;
  const canExpand = supportsExpand && isContextPage && !isPrimary && !isExpanded && !atSeedCap;
  const reason = !supportsExpand ? t("graph.expandUnsupported")
    : !isContextPage ? t("graph.expandUnavailable")
    : isPrimary ? t("graph.primarySeed")
    : atSeedCap && !isExpanded ? t("graph.expandCap") : undefined;
  return {
    action: isExpanded ? "collapse" : "expand",
    disabled: isExpanded ? !canCollapse || busyAction === `collapse:${node.id}`
      : !canExpand || busyAction === `expand:${node.id}`,
    href: workspaceGraphHref?.(node) ?? null,
    reason,
  };
}

function GraphSelectedNodeActions({ actions, node, onCollapse, onExpand, reasonId }: {
  actions: SelectedNodeActions;
  node: GraphNode;
  onCollapse: (node: GraphNode) => void;
  onExpand: (node: GraphNode) => void;
  reasonId: string;
}) {
  const { t } = useTranslation("graph");
  return (
    <div className="graph-details-context-actions">
      {actions.href ? <Link to={actions.href}>{t("graph.showInKnowledgeMap")}</Link> : null}
      <button
        aria-describedby={actions.reason ? reasonId : undefined}
        className="graph-list-action-button"
        disabled={actions.disabled}
        onClick={() => actions.action === "collapse" ? onCollapse(node) : onExpand(node)}
        type="button"
      >
        <Focus aria-hidden="true" size={15} />
        {t(`graph.${actions.action}`)}
      </button>
    </div>
  );
}

/** The change and audit rows folded into a version: one line per action. */
function GraphNodeRecords({ records }: { records: NonNullable<GraphNode["records"]> }) {
  const { t } = useTranslation("graph");
  const byAction = new Map<string, { count: number; last: string }>();
  for (const record of records) {
    const entry = byAction.get(record.action_kind);
    byAction.set(record.action_kind, {
      count: (entry?.count ?? 0) + 1,
      last: entry && entry.last > record.occurred_at ? entry.last : record.occurred_at,
    });
  }
  return (
    <div className="graph-node-records">
      <h5>{t("graph.details.records")}</h5>
      <ul>{[...byAction.entries()].map(([action, { count, last }]) => <li key={action}>
        {graphLabel(t, "graphActionKind", action)} {count > 1 ? `×${count}` : ""}
        <time dateTime={last}>{formatVersionTimestamp(last)}</time>
      </li>)}</ul>
    </div>
  );
}

function GraphDetail({ label, value }: { label: string; value: string }) {
  return <><dt>{label}</dt><dd>{value}</dd></>;
}

function GraphIconButton({ children, className = "", disabled = false, label, onClick }: { children: React.ReactNode; className?: string; disabled?: boolean; label: string; onClick: () => void }) {
  return <button aria-label={label} className={`graph-icon-button ${className}`} disabled={disabled} onClick={onClick} title={label} type="button">{children}</button>;
}

function nodeHref(node: GraphNode, workspaceId: string | null) {
  if (!workspaceId) return null;
  return searchResultHref(
    {
      id: node.entity_id,
      type: node.entity_kind,
      concept_path: node.path,
      source_ref: { kind: node.entity_kind, id: node.entity_id },
    },
    workspaceId,
  );
}

function syncFacetSelection<T extends string>(
  selected: T[],
  previousOptions: T[],
  nextOptions: T[],
) {
  const selectedSet = new Set(selected);
  const previouslyAllSelected = previousOptions.every((option) => selectedSet.has(option));
  return previouslyAllSelected
    ? [...nextOptions]
    : selected.filter((option) => nextOptions.includes(option));
}

function excludedFilters(
  key: keyof GraphFilters,
  options: readonly string[],
  selected: readonly string[],
) {
  return options
    .filter((option) => !selected.includes(option))
    .map((value) => ({ key, value }));
}

function uniqueSceneValues(values: string[]) {
  return [...new Set(values)].sort((left, right) => left.localeCompare(right));
}

/**
 * A conversation hangs off the seed it used. When filters hide every path
 * from a seed to a conversation, the conversation goes, and so do the
 * documents that are in the scene only because it used them. Other nodes
 * keep the history view's rule: a node stays while its relations are hidden
 * or not yet recorded.
 */
function withoutDetachedSessions(nodes: GraphNode[], edges: GraphEdge[], allEdges: GraphEdge[]): GraphScene {
  const sessions = new Set(nodes.filter((node) => isSessionNode(node) || isSessionGroupNode(node)).map((node) => node.id));
  if (!sessions.size) return { nodes, edges };
  const nonSessionRelation = new Set(allEdges.filter((edge) => !sessions.has(edge.source))
    .flatMap((edge) => [edge.source, edge.target]));
  const sessionOnly = new Set(allEdges.filter((edge) => sessions.has(edge.source))
    .map((edge) => edge.target).filter((id) => !nonSessionRelation.has(id)));
  const reached = new Set(nodes.filter((node) => node.is_seed).map((node) => node.id));
  const neighbours = new Map<string, string[]>();
  for (const edge of edges) {
    neighbours.set(edge.source, [...(neighbours.get(edge.source) ?? []), edge.target]);
    neighbours.set(edge.target, [...(neighbours.get(edge.target) ?? []), edge.source]);
  }
  const pending = [...reached];
  while (pending.length) {
    for (const next of neighbours.get(pending.pop()!) ?? []) {
      if (!reached.has(next)) { reached.add(next); pending.push(next); }
    }
  }
  const kept = nodes.filter((node) => reached.has(node.id) || !(sessions.has(node.id) || sessionOnly.has(node.id)));
  const keptIds = new Set(kept.map((node) => node.id));
  return { nodes: kept, edges: edges.filter((edge) => keptIds.has(edge.source) && keptIds.has(edge.target)) };
}

function localizedGraphScene(scene: GraphScene, t: TFunction): GraphScene {
  return {
    nodes: scene.nodes,
    edges: scene.edges.map((edge) => ({
      ...edge,
      label: edge.action_kind
        ? graphLabel(t, "graphActionKind", edge.action_kind)
        : graphLabel(t, "graphEdgeKind", edge.edge_kind),
    })),
  };
}

function withCoAccessLabels(scene: GraphScene, t: TFunction<"graph">): GraphScene {
  if (!scene.edges.some((edge) => edge.edge_kind === "co_access")) return scene;
  return {
    nodes: scene.nodes,
    edges: scene.edges.map((edge) => {
      const sentence = coAccessSentence(edge, t);
      return sentence ? { ...edge, label: sentence } : edge;
    }),
  };
}

function localizedGraphTimeline(
  payload: GraphQueryPayload,
  filters: GraphFilters,
  t: TFunction,
  applyVisibility: (scene: GraphScene) => GraphScene = (scene) => scene,
) {
  const events = sortedTimeline(payload);
  const nodes = new Map(payload.nodes.map((node) => [node.id, node]));
  const edges = new Map(payload.edges.map((edge) => [edge.id, edge]));
  for (const event of events) {
    for (const node of timelineNodeSnapshots(event)) nodes.set(node.id, node);
    for (const edge of timelineEdgeSnapshots(event)) edges.set(edge.id, edge);
  }
  // Each frame once: event i changes the view when frame i differs from i-1.
  const frames = [-1, ...events.keys()].map((cursor) => applyVisibility(deriveGraphScene(payload, filters, cursor)));
  return events.map((event, index) => {
    const visibleChange = !sameGraphScene(frames[index], frames[index + 1]);
    const action = graphLabel(t, "graphActionKind", event.action_kind);
    const edge = [
      ...timelineEdgeSnapshots(event),
      ...(event.edge_ids ?? []).flatMap((id) => edges.get(id) ? [edges.get(id)!] : []),
    ][0];
    if (edge) {
      return {
        ...event,
        ...timelinePresentation(event, payload.history.mode, nodes, edges, t),
        visible_change: visibleChange,
        label: t("graph.timeline.edgeEvent", {
          action,
          defaultValue: "{{source}} —{{action}}→ {{target}}",
          source: nodes.get(edge.source)?.label ?? edge.source,
          target: nodes.get(edge.target)?.label ?? edge.target,
        }),
      };
    }
    const subjects = [
      ...timelineNodeSnapshots(event),
      ...(event.node_ids ?? []).flatMap((id) => nodes.get(id) ? [nodes.get(id)!] : []),
    ];
    const subjectLabels = uniqueSceneValues(subjects.map((node) => node.label));
    return {
      ...event,
      ...timelinePresentation(event, payload.history.mode, nodes, edges, t),
      visible_change: visibleChange,
      label: subjectLabels.length
        ? t("graph.timeline.entityEvent", {
            action,
            defaultValue: "{{entity}} · {{action}}",
            entity: subjectLabels.join(", "),
          })
        : action,
    };
  });
}

/**
 * Same order and same content. Frames share their unchanged node and edge
 * objects, so identity settles almost every pair and JSON only the rest.
 */
function sameGraphScene(left: GraphScene, right: GraphScene) {
  const same = <T,>(a: readonly T[], b: readonly T[]) => a.length === b.length
    && a.every((item, index) => item === b[index] || JSON.stringify(item) === JSON.stringify(b[index]));
  return same(left.nodes, right.nodes) && same(left.edges, right.edges);
}

function timelineNodeSnapshots(event: GraphTimelineEvent) {
  return event.node_snapshots ?? [];
}

function timelineEdgeSnapshots(event: GraphTimelineEvent) {
  return event.edge_snapshots ?? [];
}

type GraphLabelGroup =
  | "graphActionKind"
  | "graphDirection"
  | "graphEdgeKind"
  | "graphEntityKind"
  | "graphEvidenceKind"
  | "graphHistoryBasis"
  | "graphHistoryStatus"
  | "graphLayer"
  | "graphNodeFamily"
  | "graphProvenance"
  | "graphStatus";

function graphLabel(t: TFunction, group: GraphLabelGroup, value: string) {
  return domainLabel("graph", group, value, t);
}

function localRelationLabel(t: TFunction, kind: GraphEdgeVisualKind) {
  const labels: Record<GraphEdgeVisualKind, string> = {
    link: t("graph.localToolbar.link", { defaultValue: "Links" }),
    citation: t("graph.localToolbar.citation", { defaultValue: "Citations" }),
    coAccess: t("graph.localToolbar.coAccess", { defaultValue: "Read together" }),
    membership: t("graph.localToolbar.membership", { defaultValue: "Membership" }),
    actionWrite: t("graph.legend.edge.actionWrite"),
    actionCite: t("graph.legend.edge.actionCite"),
    actionRead: t("graph.legend.edge.actionRead"),
    actionReference: t("graph.legend.edge.actionReference"),
    action: t("graph.localToolbar.action", { defaultValue: "Actions" }),
  };
  return labels[kind];
}

function graphLabelGroupForFilter(key: keyof GraphFilters): GraphLabelGroup {
  const groups: Record<keyof GraphFilters, GraphLabelGroup> = {
    actionKinds: "graphActionKind",
    directions: "graphDirection",
    edgeKinds: "graphEdgeKind",
    layers: "graphLayer",
    nodeFamilies: "graphNodeFamily",
    provenances: "graphProvenance",
    statuses: "graphStatus",
  };
  return groups[key];
}

function graphCoverageNotice(payload: GraphQueryPayload, t: TFunction) {
  const omissions = payload.omissions ?? [];
  const codes = [...new Set([...(payload.warnings ?? []), ...(payload.history.warnings ?? [])])];
  // Partial temporal coverage belongs to the Timeline, not to a missing-item
  // warning on an otherwise complete graph. Real budgets/privacy limits stay.
  if (payload.history.mode !== "current" && !payload.truncated && omissions.length === 0
    && payload.completeness === "complete"
    && codes.every((code) => code === "history_coverage_partial")) return null;
  const hasCoverageRestriction = payload.truncated
    || payload.completeness !== "complete"
    || payload.history.status !== "complete"
    || omissions.length > 0;
  if (!hasCoverageRestriction && !codes.length) return null;

  // Older responses classify the independent folder grouping limit as a
  // display-budget cap. Only the explicit folder-only case is informational;
  // source limits, privacy suppression, unknown reasons and history bounds
  // remain warnings. Never infer that the current projection is complete.
  const folderOnly = omissions.length > 0
    && omissions.every((omission) => omission.reason === "folder_node")
    && payload.completeness !== "bounded_sample"
    && payload.completeness !== "privacy_suppressed"
    && payload.history.status === "complete"
    && codes.every((code) => code === "graph_budget_applied" || code === "usage_relation_signals_disabled");
  const settingsOnly = !hasCoverageRestriction
    && codes.every((code) => code === "usage_relation_signals_disabled");
  const historyCodes = new Set([
    "graph_timeline_catalog_bounded", "history_coverage_partial",
  ]);
  // Observation history also supplies current relationships. Its source cap
  // is not merely a timeline restriction, even when history.status is complete.
  // Only explicit complete/untruncated scene metadata permits this distinction.
  const currentHistoryOnly = payload.history.mode === "current"
    && payload.completeness === "complete"
    && !payload.truncated
    && omissions.length === 0
    && (payload.history.status !== "complete" || codes.some((code) => historyCodes.has(code)))
    && codes.every((code) => historyCodes.has(code));
  const nonDisplayReasons = new Set(["folder_node", "frontier_cap", "folder_source_cap", "relation_source_cap"]);
  const onlyNonDisplayOmissions = omissions.length > 0
    && omissions.every((omission) => nonDisplayReasons.has(omission.reason));
  const messageCodes = codes.filter((code) => code !== "graph_budget_applied"
    || (!onlyNonDisplayOmissions && !currentHistoryOnly));
  return {
    folderOnly,
    tone: folderOnly || settingsOnly ? "info" as const : "warning" as const,
    heading: folderOnly
      ? t("graph.omissions.folderNotice", { defaultValue: "Some folders are not shown." })
      : settingsOnly
        ? t("graph.omissions.settingNotice", { defaultValue: "Relationship setting" })
        : currentHistoryOnly
          ? t("graph.omissions.timelineNotice", { defaultValue: "Timeline coverage is limited." })
          : t("graph.coverageWarning", { defaultValue: "This graph has bounded or partial coverage." }),
    messages: graphWarningMessages(t, messageCodes, payload.history.mode === "current"),
  };
}

function graphOmissionDescription(
  omission: NonNullable<GraphQueryPayload["omissions"]>[number],
  t: TFunction,
) {
  const reasons: Record<string, string> = {
    node_quota: t("graph.omissions.nodeQuota", { defaultValue: "Node display limit" }),
    relation_quota: t("graph.omissions.relationQuota", { defaultValue: "Relationship display limit" }),
    frontier_cap: t("graph.omissions.frontierCap", { defaultValue: "Expansion inspection limit" }),
    folder_node: t("graph.omissions.folderNode", { defaultValue: "Folder grouping limit" }),
    folder_source_cap: t("graph.omissions.folderSourceCap", { defaultValue: "Folder inspection limit" }),
    relation_source_cap: t("graph.omissions.relationSourceCap", { defaultValue: "Relationship inspection limit" }),
  };
  const groups: Record<string, string> = {
    folder: t("graph.omissions.group.folder", { defaultValue: "Folder relationships" }),
    seed: t("graph.omissions.group.seed", { defaultValue: "Seed relationships" }),
    h1: t("graph.omissions.group.h1", { defaultValue: "Among distance-1 neighbors" }),
    h12: t("graph.omissions.group.h12", { defaultValue: "Between distance 1 and 2" }),
    h2: t("graph.omissions.group.h2", { defaultValue: "Among distance-2 neighbors" }),
  };
  const reason = Object.hasOwn(reasons, omission.reason)
    ? reasons[omission.reason]
    : t("graph.omissions.unknown", { defaultValue: "Unspecified exclusion" });
  const distance = omission.hop === 1 || omission.hop === 2
    ? t("graph.omissions.distance", { defaultValue: "Distance {{hop}}", hop: omission.hop }) : null;
  const group = omission.relation_class && Object.hasOwn(groups, omission.relation_class)
    ? groups[omission.relation_class] : null;
  return [reason, distance, group].filter(Boolean).join(" · ");
}

function graphWarningMessages(t: TFunction, codes: string[], currentMode: boolean) {
  const known: Record<string, string> = {
    as_of_history_unsupported: t("graph.warnings.asOfUnsupported", {
      defaultValue: "The selected point-in-time view is not supported by this history source.",
    }),
    graph_budget_applied: t("graph.warnings.budgetApplied", {
      defaultValue: "The graph was limited to the requested display budget.",
    }),
    graph_projection_source_bounded: t("graph.warnings.projectionBounded", {
      defaultValue: "The current graph inspection reached a source limit; some nodes or relationships may be missing.",
    }),
    graph_history_source_bounded: currentMode
      ? t("graph.warnings.currentObservationBounded", {
          defaultValue: "Observation inspection reached a source limit; current relationships may be missing.",
        })
      : t("graph.warnings.historyBounded", {
          defaultValue: "Only a bounded portion of graph history was inspected.",
        }),
    graph_timeline_catalog_bounded: t("graph.warnings.timelineCatalogBounded", {
      defaultValue: "The timeline catalog reached its inspection limit, so older graph events may be omitted.",
    }),
    history_coverage_partial: t("graph.warnings.coveragePartial", {
      defaultValue: "Timeline coverage is partial.",
    }),
    usage_relation_signals_disabled: t("graph.warnings.usageSignalsDisabled", {
      defaultValue: "Read-together relationships are turned off for this workspace.",
    }),
  };
  const unknown = t("graph.warnings.unknown", {
    defaultValue: "Some graph data could not be shown.",
  });
  return [...new Set(codes.map((code) => Object.hasOwn(known, code) ? known[code] : unknown))];
}

function readLocalGraphTokens(element: Element, forcedColors: boolean) {
  const theme = element.closest("[data-graph-theme]")?.getAttribute("data-graph-theme")
    ?? element.closest("[data-theme]")?.getAttribute("data-theme");
  return readGraphTokens(element, forcedColors ? "forced" : theme === "dark" ? "dark" : "light");
}

function useLocalGraphTokens(regionRef: { current: HTMLElement | null }, forcedColors: boolean) {
  const [tokens, setTokens] = useState(() => readLocalGraphTokens(document.documentElement, forcedColors));
  useLayoutEffect(() => {
    const region = regionRef.current;
    if (!region) return undefined;
    // Ancestor class/style changes (a dialog opening) re-read the tokens;
    // unchanged values keep their identity, so nothing downstream redraws.
    const update = () => {
      const next = readLocalGraphTokens(region, forcedColors);
      setTokens((current) => JSON.stringify(current) === JSON.stringify(next) ? current : next);
    };
    update();
    if (typeof MutationObserver === "undefined") return undefined;
    // One observer per GraphView, not per row. Ancestors supply inherited
    // custom tokens, system colors and theme; the root supplies scoped ones.
    const observer = new MutationObserver(update);
    const attributes = { attributes: true, attributeFilter: ["class", "data-theme", "data-graph-theme", "style"] };
    for (let element: Element | null = region; element; element = element.parentElement) observer.observe(element, attributes);
    return () => observer.disconnect();
  }, [forcedColors, regionRef]);
  return tokens;
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

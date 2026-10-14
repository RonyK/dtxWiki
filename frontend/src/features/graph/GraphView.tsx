import {
  Download,
  Focus,
  Maximize2,
  Pause,
  Play,
  RotateCcw,
  StepBack,
  StepForward,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import type { TFunction } from "i18next";
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { MarkdownView } from "../pages/MarkdownView";
import { LOCAL_EDGE_VISUAL_KIND_ORDER, localEdgeVisualKind } from "./local/edgeKinds";
import { localNodeVisualKind } from "./local/nodeVisual";
import { LocalGraphCoverageInfo } from "./local/LocalGraphCoverageInfo";
import { LocalGraphList, type LocalGraphSelectionOrigin } from "./local/LocalGraphList";
import { localSceneScopeKey } from "./local/sceneScope";
import { buildSceneNavigation, type SceneNavigation } from "./sceneNavigation";
import { GRAPH_EDGE_PATTERN, readGraphTokens, type GraphEdgeVisualKind, type GraphNodeVisualKind, type GraphVisualTokens } from "./shared/graphTokens";
import type {
  GraphEdge,
  GraphDepth,
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
  graphActionFamily,
  graphFacets,
  graphSceneToMermaid,
  sortedTimeline,
  type GraphFilters,
  type GraphScene,
} from "./graphModel";

type GraphViewProps = {
  active: boolean;
  onCollapse: (node: GraphNode) => Promise<void>;
  onDepthChange: (depth: GraphDepth) => Promise<void>;
  onExpand: (node: GraphNode) => Promise<void>;
  onHistoryModeChange: (mode: GraphHistoryMode) => Promise<void>;
  payload: GraphQueryPayload;
  workspaceGraphHref?: (node: GraphNode) => string | null;
  // Canonical Context permalinks are workspace-in-path; without the id the
  // list falls back to the query-param form (see searchResultHref).
  workspaceId?: string | null;
};

const PLAYBACK_SPEEDS = [0.5, 1, 2] as const;
// Match Mermaid's default parsing limit. Larger Local scenes keep the
// complete text fallback rather than mounting a diagram that cannot render.
const STATIC_DIAGRAM_EDGE_LIMIT = 500;
type LocalNodeVisuals = { tokens: GraphVisualTokens; forcedColors: boolean };

export function GraphView({
  active,
  onCollapse,
  onDepthChange,
  onExpand,
  onHistoryModeChange,
  payload,
  workspaceGraphHref,
  workspaceId = null,
}: GraphViewProps) {
  const { t } = useTranslation("graph");
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
  const depthReasonId = useId();
  const facets = useMemo(() => graphFacets(payload), [payload]);
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
  const [timelineCursor, setTimelineCursor] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);
  const [announcement, setAnnouncement] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const forcedColors = useMediaQuery("(forced-colors: active)");
  const tokens = useLocalGraphTokens(graphRegionRef, forcedColors);
  const nodeVisuals = useMemo(() => ({ tokens, forcedColors }), [tokens, forcedColors]);
  const expansionAvailability = payload.capabilities?.expand === false
    ? "unsupported"
    : payload.query.seeds.length >= 8
      ? "cap"
      : "available";
  const previousExpansionAvailabilityRef = useRef(expansionAvailability);
  const supportsDepthTwo = payload.history.mode === "current";
  const depthBusy = Boolean(busyAction?.startsWith("depth:") || busyAction?.startsWith("mode:"));
  const timeline = useMemo(
    () => localizedGraphTimeline(payload, filters, t),
    [filters, payload, t],
  );
  const filteredScene = useMemo(
    () => deriveGraphScene(payload, filters, timelineCursor),
    [filters, payload, timelineCursor],
  );
  const scene = useMemo(
    () => localizedGraphScene(filteredScene, t),
    [filteredScene, t],
  );
  const sceneNavigation = useMemo(() => buildSceneNavigation(scene), [scene]);
  // The canvas shows an edge's label while it is hovered or selected; a
  // "read together" edge shows what it rests on instead of its kind.
  const canvasScopeKey = localSceneScopeKey({
    workspaceId, query: payload.query, filters, historyMode: payload.history.mode,
    cursor: timelineCursor, event: timelineCursor === null ? undefined : timeline[timelineCursor],
  });
  const canvasScene = useMemo(
    () => ({ ...withCoAccessLabels(scene, t), scopeKey: canvasScopeKey }),
    [canvasScopeKey, scene, t],
  );
  const mermaidSource = useMemo(
    () => staticOpen && scene.edges.length <= STATIC_DIAGRAM_EDGE_LIMIT
      ? graphSceneToMermaid(scene)
      : null,
    [scene, staticOpen],
  );
  const visibleCursor = timelineCursor ?? timeline.length - 1;
  const selectedNode = scene.nodes.find((node) => node.id === selectedId) ?? null;
  const selectedEdge = scene.edges.find((edge) => edge.id === selectedId) ?? null;
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
    if (selectedId && !visibleIds.has(selectedId)) setSelectedId(null);
    if (focusedId && !visibleIds.has(focusedId)) setFocusedId(null);
  }, [focusedId, scene, selectedId]);

  useEffect(() => {
    const region = graphRegionRef.current;
    if (!region) return undefined;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
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
      // The last scrub/step position represents the actor-visible current
      // projection, not merely the final observation snapshot. Normalize the
      // autoplay path the same way so provenance/evidence cannot diverge by
      // control method when a stable edge ID has newer current metadata.
      setTimelineCursor(null);
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
  }, [active, playbackSpeed, playing, t, timeline.length, visibleCursor]);

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
    graphRegionRef.current?.focus();
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
    graphRegionRef.current?.focus();
  }

  function stepTimeline(direction: -1 | 1) {
    setPlaying(false);
    const next = Math.max(
      -1,
      Math.min(timeline.length - 1, visibleCursor + direction),
    );
    setTimelineCursor(next === timeline.length - 1 ? null : next);
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
      // `null` is the actor-visible current projection, while a numeric cursor
      // is an observed historical frame. Keep those states distinct even for
      // a one-event timeline so repeated Play presses can reveal frame 0 and
      // then advance back to current instead of becoming stuck on frame 0.
      if (timelineCursor === null) setTimelineCursor(0);
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
    try {
      await action();
    } catch {
      if (generation !== actionGenerationRef.current) return;
      setActionError(
        t("graph.actionError", {
          defaultValue: "The graph could not be updated. Try again.",
        }),
      );
    } finally {
      if (generation === actionGenerationRef.current) setBusyAction(null);
    }
  }

  async function changeDepth(depth: GraphDepth) {
    if (!supportsDepthTwo || depth === payload.query.depth) return;
    await runAction(`depth:${depth}`, () => onDepthChange(depth));
  }

  async function changeHistoryMode(mode: GraphHistoryMode) {
    setPlaying(false);
    await runAction(`mode:${mode}`, async () => {
      await onHistoryModeChange(mode);
      setTimelineCursor(null);
      setSelectedId(null);
    });
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
    const blob = new Blob([graphSceneToMermaid(scene)], {
      type: "text/plain;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `dtxwiki-graph-${payload.projection_revision}.mmd`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section
      aria-labelledby="graph-view-heading"
      className="graph-view"
      data-testid="graph-view"
      ref={graphRegionRef}
      tabIndex={-1}
    >
      <header className="graph-view-header">
        <div>
          <h3 id="graph-view-heading">{t("graph.heading", { defaultValue: "Graph relationships" })}</h3>
          <p>
            {t("graph.description", {
              defaultValue:
                "Explore documents, conversations, actions, and the evidence connecting them.",
            })}
          </p>
        </div>
        <p className="graph-view-count" aria-live="polite">
          {t("graph.visibleCount", {
            defaultValue: "{{nodes}} nodes · {{edges}} edges",
            edges: scene.edges.length,
            nodes: scene.nodes.length,
          })}
        </p>
      </header>

      {coverageNotice && !coverageNotice.folderOnly ? (
        <StatusBanner tone={coverageNotice.tone}>
          <strong>{coverageNotice.heading}</strong>
          {coverageNotice.messages.length ? <> <span>{coverageNotice.messages.join(" ")}</span></> : null}
        </StatusBanner>
      ) : null}
      {omissionDescriptions.length && !coverageNotice?.folderOnly ? (
        <details className="graph-filter-bar graph-filter-options">
          <summary>{t("graph.omissions.heading", { defaultValue: "What was left out" })}</summary>
          <ul>
            {omissionDescriptions.map((description) => <li key={description}>{description}</li>)}
          </ul>
          {hasDisplayQuota ? (
            <p>{t("graph.omissions.appliedBudget", {
              defaultValue: "Applied display limit: {{nodes}} nodes · {{edges}} relationships.",
              nodes: payload.budget.applied.nodes,
              edges: payload.budget.applied.edges,
            })}</p>
          ) : null}
          <p>{t("graph.omissions.noCounts", {
            defaultValue: "The server does not report how many items were excluded.",
          })}</p>
        </details>
      ) : null}
      {actionError ? <StatusBanner tone="warning">{actionError}</StatusBanner> : null}

      {scene.nodes.length > 0 && scene.edges.length === 0 ? (
        <p className="graph-no-relationships">
          {t("graph.noRelationships", {
            defaultValue: "No graph relationships to show.",
          })}
        </p>
      ) : null}

      <div className="graph-canvas-toolbar" role="toolbar" aria-label={t("graph.canvasControls", { defaultValue: "Graph view controls" })}>
        <div
          aria-label={t("graph.depth.heading", { defaultValue: "Graph depth" })}
          aria-busy={depthBusy}
          className="graph-local-depth-control"
          role="group"
        >
          <span className="graph-local-toolbar-title">{t("graph.depth.label", { defaultValue: "Depth" })}</span>
          {([1, 2] as const).map((depth) => (
            <button
              aria-describedby={depth === 2 && !supportsDepthTwo ? depthReasonId : undefined}
              aria-label={t("graph.depth.option", { defaultValue: "Depth {{depth}}", depth })}
              aria-pressed={(payload.query.depth ?? 1) === depth}
              className="graph-local-label-toggle"
              disabled={!active || depthBusy || (depth === 2 && !supportsDepthTwo)}
              key={depth}
              onClick={() => void changeDepth(depth)}
              type="button"
            >
              {depth}
            </button>
          ))}
          {!supportsDepthTwo ? (
            <small className="graph-local-depth-note" id={depthReasonId}>
              {t("graph.depth.historyOneHop", { defaultValue: "History views currently support depth 1 only." })}
            </small>
          ) : null}
        </div>
        <GraphIconButton label={t("graph.zoomIn", { defaultValue: "Zoom in" })} onClick={() => canvasRef.current?.zoomIn()}>
          <ZoomIn aria-hidden="true" size={17} />
        </GraphIconButton>
        <GraphIconButton label={t("graph.zoomOut", { defaultValue: "Zoom out" })} onClick={() => canvasRef.current?.zoomOut()}>
          <ZoomOut aria-hidden="true" size={17} />
        </GraphIconButton>
        <GraphIconButton label={t("graph.fit", { defaultValue: "Fit graph" })} onClick={() => canvasRef.current?.fit()}>
          <Maximize2 aria-hidden="true" size={17} />
        </GraphIconButton>
        <GraphIconButton label={t("graph.resetView", { defaultValue: "Reset viewport, pins, and selection" })} onClick={resetView}>
          <RotateCcw aria-hidden="true" size={17} />
        </GraphIconButton>
        <GraphIconButton
          disabled={payload.capabilities?.export === false}
          label={t("graph.export", { defaultValue: "Export filtered graph" })}
          onClick={exportFilteredScene}
        >
          <Download aria-hidden="true" size={17} />
        </GraphIconButton>
        <button
          aria-pressed={showLabels}
          className="graph-local-label-toggle"
          onClick={() => setShowLabels((current) => !current)}
          type="button"
        >
          {t("graph.localToolbar.labels", { defaultValue: "Labels" })}
        </button>
        {coverageNotice?.folderOnly ? (
          <LocalGraphCoverageInfo
            active={active}
            descriptions={omissionDescriptions}
            heading={coverageNotice.heading}
            messages={coverageNotice.messages}
            payload={payload}
            workspaceId={workspaceId}
          />
        ) : null}
      </div>

      <div className="graph-local-stage">
        {scene.nodes.length ? (
          <GraphCanvas
          ref={canvasRef}
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
          busyAction={busyAction}
          edge={selectedEdge}
          node={selectedNode}
          nodes={scene.nodes}
          navigation={sceneNavigation}
          nodeVisuals={nodeVisuals}
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

      <div
        aria-label={t("graph.localToolbar.relationships", { defaultValue: "Relationships" })}
        className="graph-local-toolbar"
        role="toolbar"
      >
        <span className="graph-local-toolbar-title">
          {t("graph.localToolbar.relationships", { defaultValue: "Relationships" })}
        </span>
        <div className="graph-local-relation-chips">
          {relationKinds.map(({ kind, edgeKinds, count, enabled }) => (
            <button
              aria-label={`${localRelationLabel(t, kind)} ${count}`}
              aria-pressed={enabled}
              className={`graph-local-relation-chip graph-local-relation-chip-${kind}`}
              disabled={edgeKinds.length === 0}
              key={kind}
              onClick={() => toggleRelationKinds(edgeKinds)}
              type="button"
            >
              <span aria-hidden="true" className="graph-local-relation-line" />
              {localRelationLabel(t, kind)}
              <span className="graph-local-relation-count">{count}</span>
            </button>
          ))}
        </div>
      </div>

      <details
        aria-label={t("graph.filters.heading", { defaultValue: "Graph filters" })}
        className="graph-filter-bar graph-filter-options"
      >
        <summary>
          <span>{t("graph.localToolbar.moreFilters", { defaultValue: "More filters" })}</span>
          {" · "}
          <span aria-live="polite">
            {t("graph.filters.activeCount", {
              defaultValue: "{{count}} active filters",
              count: activeFilters.length,
            })}
          </span>
        </summary>
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
      </details>

      <GraphLegend nodeVisuals={nodeVisuals} scene={scene} />

      <GraphTimeline
        busy={Boolean(busyAction?.startsWith("mode:"))}
        cursor={visibleCursor}
        historyMode={payload.history.mode}
        onCursorChange={(next) => {
          setPlaying(false);
          setTimelineCursor(next >= timeline.length - 1 ? null : next);
        }}
        onHistoryModeChange={changeHistoryMode}
        onPause={pausePlayback}
        onPlay={startPlayback}
        onReset={() => {
          setPlaying(false);
          setTimelineCursor(-1);
          setAnnouncement(t("graph.timeline.startAnnouncement", { defaultValue: "Timeline reset to the starting scene." }));
        }}
        onSpeedChange={setPlaybackSpeed}
        onStep={stepTimeline}
        payload={payload}
        playing={playing}
        reducedMotion={reducedMotion}
        speed={playbackSpeed}
        timeline={timeline}
      />

      <p className="sr-only" aria-live="polite" aria-atomic="true">{announcement}</p>

      <div className="graph-parity-grid">
        <GraphRelationshipList
          active={active}
          edges={scene.edges}
          nodes={scene.nodes}
          nodeVisuals={nodeVisuals}
          onBlur={clearItemFocus}
          onFocus={setFocusedId}
          onSelect={selectItem}
          selectedId={effectiveSelectedId}
          workspaceId={workspaceId}
        />
      </div>

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
        {staticOpen ? mermaidSource !== null ? (
          <MarkdownView
            body={`\`\`\`mermaid\n${mermaidSource}\n\`\`\``}
            wikilinkMode="plain"
          />
        ) : (
          <>
            <p>{t("graph.staticFallbackList", {
              defaultValue: "This scene exceeds the diagram limit of {{limit}} relationships. All nodes and relationships are listed here.",
              limit: STATIC_DIAGRAM_EDGE_LIMIT,
            })}</p>
            <GraphTextContents scene={scene} />
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

function GraphLegend({ nodeVisuals, scene }: { nodeVisuals: LocalNodeVisuals; scene: GraphScene }) {
  const { t } = useTranslation("graph");
  const { tokens, forcedColors } = nodeVisuals;
  const visibleNodeKinds = new Set(scene.nodes.map((node) => localNodeVisualKind(node.entity_kind, node.family)));
  const nodeKinds = (["context", "library", "conversation", "folder"] as const)
    .filter((kind) => visibleNodeKinds.has(kind));
  const visibleEdgeKinds = new Set(scene.edges.map((edge) => localEdgeVisualKind(edge.edge_kind)));
  const edgeVisualKinds = LOCAL_EDGE_VISUAL_KIND_ORDER.filter((kind) => visibleEdgeKinds.has(kind));
  const edgeKinds = uniqueSceneValues(scene.edges.map((edge) => edge.edge_kind));
  const actionKinds = uniqueSceneValues(
    scene.edges.flatMap((edge) => edge.action_kind ? [edge.action_kind] : []),
  );
  return (
    <details className="graph-legend">
      <summary>{t("graph.legend.heading", { defaultValue: "Legend" })}</summary>
      {scene.edges.some((edge) => localEdgeVisualKind(edge.edge_kind) === "action") ? (
        <p className="graph-local-action-legend graph-local-relation-chip-action">
          <span aria-hidden="true" className="graph-local-relation-line" />
          {t("graph.legend.actionLines", {
            defaultValue: "Long-dashed lines show action occurrences, not document links or citations.",
          })}
        </p>
      ) : null}
      <div className="graph-legend-groups">
        <ul aria-label={t("graph.legend.nodes", { defaultValue: "Node types" })}>
          {nodeKinds.map((kind) => (
            <li data-node-visual-kind={kind} key={kind}>
              <LocalNodeSwatch color={tokens.node[kind]} forcedColors={forcedColors} kind={kind} surface={tokens.surface} />
              {t(`graph.legend.node.${kind}`)}
            </li>
          ))}
          {scene.nodes.some((node) => node.is_seed) ? (
            <li data-node-visual-kind="seed">
              <svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16">
                <circle cx="8" cy="8" fill={tokens.node.context} r="5.5" stroke="currentColor" strokeWidth="2.64" />
              </svg>
              {t("graph.legend.node.seed")}
            </li>
          ) : null}
        </ul>
        <ul aria-label={t("graph.legend.edges", { defaultValue: "Relationship line styles" })}>
          {edgeVisualKinds.map((kind) => (
            <li data-edge-visual-kind={kind} key={kind}>
              <svg aria-hidden="true" height="16" viewBox="0 0 34 16" width="34">
                <line stroke={tokens.edge[kind]} strokeDasharray={GRAPH_EDGE_PATTERN[kind]?.join(" ")}
                  strokeWidth={kind === "membership" ? 1.1 : kind === "action" ? 1.7 : 2} x1="1" x2="33" y1="8" y2="8" />
              </svg>
              {t(`graph.legend.edge.${kind}`)}
            </li>
          ))}
        </ul>
        {(edgeKinds.length || actionKinds.length) ? (
          <ul aria-label={t("graph.legend.meanings", { defaultValue: "Relationship meanings" })}>
            {edgeKinds.map((kind) => (
              <li key={`kind:${kind}`}>
                <span aria-hidden="true" className="graph-kind-cue">→</span>
                {graphLabel(t, "graphEdgeKind", kind)}
              </li>
            ))}
            {actionKinds.map((kind) => (
              <li key={`action:${kind}`}>
                <span
                  aria-hidden="true"
                  className={`graph-action-cue graph-action-cue-${graphActionFamily(kind)}`}
                >
                  {graphActionFamilyCue(kind)}
                </span>
                {graphLabel(t, "graphActionKind", kind)}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </details>
  );
}

function LocalNodeSwatch({ color, forcedColors, kind, surface }: {
  color: string; forcedColors: boolean; kind: GraphNodeVisualKind; surface: string;
}) {
  return (
    <svg aria-hidden="true" className="graph-local-node-cue" data-node-visual-kind={kind} height="16" viewBox="0 0 16 16" width="16">
      {kind === "library" ? <rect fill={color} height="11" width="11" x="2.5" y="2.5" />
        : kind === "folder" ? <circle cx="8" cy="8" fill="none" r="5.5" stroke={color} strokeWidth="2.64" />
        : <circle cx="8" cy="8" fill={color} r="5.5" />}
      {kind === "conversation" && forcedColors ? (
        <><circle cx="8" cy="8" fill={surface} r="4.4" /><circle cx="8" cy="8" fill={color} r="3.025" /></>
      ) : null}
    </svg>
  );
}

function LocalNodeCue({ node, visuals }: { node: GraphNode; visuals: LocalNodeVisuals }) {
  const kind = localNodeVisualKind(node.entity_kind, node.family);
  return <LocalNodeSwatch color={visuals.tokens.node[kind]} forcedColors={visuals.forcedColors}
    kind={kind} surface={visuals.tokens.surface} />;
}

function GraphTimeline({
  busy,
  cursor,
  historyMode,
  onCursorChange,
  onHistoryModeChange,
  onPause,
  onPlay,
  onReset,
  onSpeedChange,
  onStep,
  payload,
  playing,
  reducedMotion,
  speed,
  timeline,
}: {
  busy: boolean;
  cursor: number;
  historyMode: GraphHistoryMode;
  onCursorChange: (cursor: number) => void;
  onHistoryModeChange: (mode: GraphHistoryMode) => void;
  onPause: () => void;
  onPlay: () => void;
  onReset: () => void;
  onSpeedChange: (speed: number) => void;
  onStep: (direction: -1 | 1) => void;
  payload: GraphQueryPayload;
  playing: boolean;
  reducedMotion: boolean;
  speed: number;
  timeline: ReturnType<typeof localizedGraphTimeline>;
}) {
  const { t } = useTranslation("graph");
  const event = timeline[cursor];
  return (
    <section className="graph-timeline" aria-labelledby="graph-timeline-heading">
      <div className="graph-timeline-heading-row">
        <div>
          <h4 id="graph-timeline-heading">{t("graph.timeline.heading", { defaultValue: "Timeline" })}</h4>
          <p>
            {t("graph.timeline.coverage", {
              defaultValue: "{{basis}} · {{status}} · coverage {{from}} to {{to}}",
              basis: graphLabel(t, "graphHistoryBasis", payload.history.basis),
              from: payload.history.coverage_from ? formatVersionTimestamp(payload.history.coverage_from) : t("graph.timeline.unknown", { defaultValue: "unknown" }),
              status: graphLabel(t, "graphHistoryStatus", payload.history.status),
              to: payload.history.watermark ? formatVersionTimestamp(payload.history.watermark) : t("graph.timeline.now", { defaultValue: "now" }),
            })}
          </p>
        </div>
        <label className="graph-history-mode">
          <span>{t("graph.timeline.mode", { defaultValue: "History mode" })}</span>
          <select
            disabled={busy}
            value={historyMode}
            onChange={(event) => onHistoryModeChange(event.target.value as GraphHistoryMode)}
          >
            <option value="current">{t("graph.timeline.modeCurrent", { defaultValue: "Current" })}</option>
            <option value="appearance">{t("graph.timeline.modeAppearance", { defaultValue: "Appearance" })}</option>
            <option value="action">{t("graph.timeline.modeAction", { defaultValue: "Actions" })}</option>
          </select>
        </label>
      </div>
      {historyMode === "current" || !timeline.length ? (
        <p className="graph-timeline-event">
          {historyMode === "current"
            ? t("graph.timeline.currentScene", {
                defaultValue: "Showing the current graph. Choose a history mode to inspect past events.",
              })
            : t("graph.timeline.noEvents", {
                defaultValue: "No history events are available for this view.",
              })}
        </p>
      ) : <>
      {reducedMotion ? (
        <p className="graph-timeline-motion-note">
          {t("graph.timeline.reducedMotion", {
            defaultValue: "Reduced motion is on; Play reveals one event at a time.",
          })}
        </p>
      ) : null}
      <div className="graph-timeline-controls">
        <GraphIconButton disabled={!timeline.length} label={playing ? t("graph.timeline.pause", { defaultValue: "Pause" }) : t("graph.timeline.play", { defaultValue: "Play" })} onClick={playing ? onPause : onPlay}>
          {playing ? <Pause aria-hidden="true" size={17} /> : <Play aria-hidden="true" size={17} />}
        </GraphIconButton>
        <GraphIconButton disabled={cursor < 0 || !timeline.length} label={t("graph.timeline.previous", { defaultValue: "Previous event" })} onClick={() => onStep(-1)}>
          <StepBack aria-hidden="true" size={17} />
        </GraphIconButton>
        <GraphIconButton disabled={cursor >= timeline.length - 1 || !timeline.length} label={t("graph.timeline.next", { defaultValue: "Next event" })} onClick={() => onStep(1)}>
          <StepForward aria-hidden="true" size={17} />
        </GraphIconButton>
        <GraphIconButton disabled={!timeline.length} label={t("graph.timeline.reset", { defaultValue: "Reset timeline" })} onClick={onReset}>
          <RotateCcw aria-hidden="true" size={17} />
        </GraphIconButton>
        <input
          aria-label={t("graph.timeline.scrubber", { defaultValue: "Timeline position" })}
          aria-valuetext={event ? `${event.label}, ${formatVersionTimestamp(event.occurred_at)}` : t("graph.timeline.start", { defaultValue: "Start" })}
          disabled={!timeline.length}
          max={timeline.length}
          min={0}
          onChange={(event) => onCursorChange(Number(event.target.value) - 1)}
          type="range"
          value={Math.max(0, cursor + 1)}
        />
        <span className="graph-timeline-position">{Math.max(0, cursor + 1)} / {timeline.length}</span>
        <label className="graph-timeline-speed">
          <span>{t("graph.timeline.speed", { defaultValue: "Speed" })}</span>
          <select disabled={reducedMotion || !timeline.length} onChange={(event) => onSpeedChange(Number(event.target.value))} value={speed}>
            {PLAYBACK_SPEEDS.map((option) => <option key={option} value={option}>{option}×</option>)}
          </select>
        </label>
      </div>
      <p className="graph-timeline-event">
        {event
          ? <><strong>{event.label}</strong><span>{graphLabel(t, "graphActionKind", event.action_kind)} · {formatVersionTimestamp(event.occurred_at, "long")}</span>{!event.visible_change ? <em>{t("graph.timeline.filteredEvent", { defaultValue: "No visible change under the current filters." })}</em> : null}</>
          : t("graph.timeline.start", { defaultValue: "Start" })}
      </p>
      </>}
    </section>
  );
}

function GraphRelationshipList({
  active,
  edges,
  nodes,
  nodeVisuals,
  onBlur,
  onFocus,
  onSelect,
  selectedId,
  workspaceId,
}: {
  active: boolean;
  edges: GraphEdge[];
  nodes: GraphNode[];
  nodeVisuals: LocalNodeVisuals;
  onBlur: (id: string) => void;
  onFocus: (id: string) => void;
  onSelect: (id: string, origin: LocalGraphSelectionOrigin) => void;
  selectedId: string | null;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("graph");
  const nodeLabels = new Map(nodes.map((node) => [node.id, node.label]));
  const scopeKey = workspaceId ?? "";
  return (
    <section className="graph-relationship-list" aria-labelledby="graph-relationship-list-heading">
      <h4 id="graph-relationship-list-heading">{t("graph.relationshipList", { defaultValue: "Accessible graph contents" })}</h4>
      <p>{t("graph.relationshipListDescription", { defaultValue: "Tab into each list once. Use arrow keys to highlight items, then Enter or Space to select and access their actions in Details." })}</p>
      <LocalGraphList
        active={active} defaultOpen empty={t("graph.emptyFiltered")} items={nodes}
        label={t("graph.nodes", { defaultValue: "Nodes" })}
        onBlur={onBlur} onFocus={onFocus} onSelect={onSelect}
        renderItem={(node) => <><LocalNodeCue node={node} visuals={nodeVisuals} />
          <span><strong>{node.label}</strong><small>{graphLabel(t, "graphNodeFamily", node.family)}{node.status ? ` · ${graphLabel(t, "graphStatus", node.status)}` : ""}</small></span></>}
        scopeKey={scopeKey} selectedId={selectedId}
      />
      <LocalGraphList
        active={active} empty={t("graph.emptyFiltered")} items={edges}
        label={t("graph.edges", { defaultValue: "Relationships" })}
        onBlur={onBlur} onFocus={onFocus} onSelect={onSelect}
        renderItem={(edge) => <><span className={`graph-edge-swatch graph-edge-swatch-${edge.layer}`} aria-hidden="true" />
          <span><strong>{edge.label}</strong><small>{nodeLabels.get(edge.source) ?? edge.source} → {nodeLabels.get(edge.target) ?? edge.target}</small></span></>}
        scopeKey={scopeKey} selectedId={selectedId}
      />
    </section>
  );
}

function GraphDetails({
  active,
  busyAction,
  edge,
  node,
  nodes,
  navigation,
  nodeVisuals,
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
  busyAction: string | null;
  edge: GraphEdge | null;
  node: GraphNode | null;
  nodes: GraphNode[];
  navigation: SceneNavigation;
  nodeVisuals: LocalNodeVisuals;
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
  const actionReasonId = useId();
  useLayoutEffect(() => {
    // A newly selected object starts at its title and actions. Keep the
    // sidebar's scroll position for ordinary updates to the same selection;
    // never scroll the outer reader or move keyboard/pointer focus here.
    if (detailsRef.current) detailsRef.current.scrollTop = 0;
  }, [node?.id, edge?.id]);
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
  const nodeLabels = new Map(nodes.map((entry) => [entry.id, entry.label]));
  const edgeSentence = edge ? t("graph.details.edgeSentence", {
    action: edge.action_kind
      ? graphLabel(t, "graphActionKind", edge.action_kind)
      : graphLabel(t, "graphEdgeKind", edge.edge_kind),
    defaultValue: "{{source}} —{{action}}→ {{target}}",
    source: nodeLabels.get(edge.source) ?? edge.source,
    target: nodeLabels.get(edge.target) ?? edge.target,
  }) : null;
  const coAccess = edge ? coAccessSentence(edge, t) : null;
  const nodeActions = node ? selectedNodeActions({
    busyAction, node, querySeeds, supportsExpand, t, workspaceGraphHref,
  }) : null;
  const clearLabel = t("graph.details.clearSelection", { defaultValue: "Clear selection" });
  return (
    <aside className="graph-details" aria-labelledby="graph-details-heading" ref={detailsRef} tabIndex={-1}>
      <div className="graph-details-control-region">
        <h4 id="graph-details-heading">{t("graph.details.heading", { defaultValue: "Details" })}</h4>
        <div className="graph-details-actions">
          {href ? (
            <Link to={href}>
              {t("graph.open", { defaultValue: "Open" })}
            </Link>
          ) : null}
          <button className="graph-list-action-button" onClick={() => onCenter((node ?? edge)!.id)} type="button">
            {t("graph.details.center", { defaultValue: "Center" })}
          </button>
          {node ? (
            <button className="graph-list-action-button" onClick={() => onTogglePin(node)} type="button">
              {pinnedIds.includes(node.id)
                ? t("graph.details.unpin", { defaultValue: "Unpin" })
                : t("graph.details.pin", { defaultValue: "Pin" })}
            </button>
          ) : null}
          <button aria-label={clearLabel} className="graph-list-action-button graph-details-clear-selection"
            onClick={onClearSelection} title={clearLabel} type="button">
            <X aria-hidden="true" className="graph-details-clear-icon" size={17} />
            <span className="graph-details-clear-label">{clearLabel}</span>
          </button>
        </div>
        {node && nodeActions ? <GraphSelectedNodeActions
          actions={nodeActions} node={node} onCollapse={onCollapse} onExpand={onExpand}
          reasonId={actionReasonId}
        /> : null}
      </div>
      <h5>{node?.label ?? edge?.label}</h5>
      {nodeActions?.reason ? <p className="graph-action-reason" id={actionReasonId}>{nodeActions.reason}</p> : null}
      <p>{t("graph.details.selectionHint", {
        defaultValue: "Other nodes stay visible but dimmed to highlight direct relationships. Click or tap empty space, or press Escape, to clear the selection.",
      })}</p>
      {edgeSentence ? <p className="graph-edge-summary">{edgeSentence}</p> : null}
      {coAccess ? <p className="graph-edge-summary">{coAccess}</p> : null}
      <dl>
        {node ? <><GraphDetail label={t("graph.details.type", { defaultValue: "Type" })} value={graphLabel(t, "graphNodeFamily", node.family)} /><GraphDetail label={t("graph.details.kind", { defaultValue: "Kind" })} value={graphLabel(t, "graphEntityKind", node.entity_kind)} />{node.status ? <GraphDetail label={t("graph.details.status", { defaultValue: "Status" })} value={graphLabel(t, "graphStatus", node.status)} /> : null}{node.actor_label ? <GraphDetail label={t("graph.details.actor", { defaultValue: "Actor" })} value={node.actor_label} /> : null}</> : null}
        {edge ? <><GraphDetail label={t("graph.details.layer", { defaultValue: "Layer" })} value={graphLabel(t, "graphLayer", edge.layer)} /><GraphDetail label={t("graph.details.relationship", { defaultValue: "Relationship" })} value={graphLabel(t, "graphEdgeKind", edge.edge_kind)} />{edge.action_kind ? <GraphDetail label={t("graph.details.action", { defaultValue: "Action" })} value={graphLabel(t, "graphActionKind", edge.action_kind)} /> : null}<GraphDetail label={t("graph.details.direction", { defaultValue: "Direction" })} value={graphLabel(t, "graphDirection", edge.direction)} /><GraphDetail label={t("graph.details.provenance", { defaultValue: "Provenance" })} value={graphLabel(t, "graphProvenance", edge.provenance)} />{edge.aggregate && !coAccess ? <GraphDetail label={t("graph.details.aggregate", { defaultValue: "Aggregate" })} value={String(edge.count ?? edge.weight)} /> : null}</> : null}
        {(node?.occurred_at || edge?.occurred_at) ? <GraphDetail label={t("graph.details.time", { defaultValue: "Time" })} value={formatVersionTimestamp((node?.occurred_at ?? edge?.occurred_at)!, "long")} /> : null}
        {evidence ? <><GraphDetail label={t("graph.details.evidence", { defaultValue: "Evidence" })} value={evidence.label ?? graphLabel(t, "graphEvidenceKind", evidence.kind)} /><GraphDetail label={t("graph.details.evidenceRef", { defaultValue: "Evidence reference" })} value={evidence.available ? evidence.ref ?? evidence.id ?? t("graph.details.available", { defaultValue: "Available" }) : t("graph.details.unavailable", { defaultValue: "Unavailable" })} /></> : null}
      </dl>
      {node ? <>
        <GraphSceneNodeList
          active={active}
          heading={t("graph.sceneNavigation.neighbors", { defaultValue: "Direct neighbors in this scene" })}
          empty={t("graph.sceneNavigation.noNeighbors", { defaultValue: "No direct neighbors are connected in this scene." })}
          nodes={navigation.neighborsByNode.get(node.id) ?? []}
          nodeVisuals={nodeVisuals}
          onBlur={onBlur}
          onFocus={onFocus}
          onSelect={onSelect}
          open
          selectedId={node.id}
          showKeyboardHint
          scopeKey={`${workspaceId ?? ""}:${node.id}`}
        />
        {navigation.membersByFolder.has(node.id) ? <GraphSceneNodeList
          active={active}
          heading={t("graph.sceneNavigation.members", { defaultValue: "Members in this scene" })}
          empty={t("graph.sceneNavigation.noMembers", { defaultValue: "No members are connected in this scene." })}
          nodes={navigation.membersByFolder.get(node.id)!}
          nodeVisuals={nodeVisuals}
          onBlur={onBlur}
          onFocus={onFocus}
          onSelect={onSelect}
          open
          selectedId={node.id}
          scopeKey={`${workspaceId ?? ""}:${node.id}`}
        /> : null}
        {navigation.foldersByMember.has(node.id) ? <GraphSceneNodeList
          active={active}
          heading={t("graph.sceneNavigation.folders", { defaultValue: "Folders in this scene" })}
          empty={t("graph.sceneNavigation.noFolders", { defaultValue: "No folders are connected in this scene." })}
          nodes={navigation.foldersByMember.get(node.id)!}
          nodeVisuals={nodeVisuals}
          onBlur={onBlur}
          onFocus={onFocus}
          onSelect={onSelect}
          selectedId={node.id}
          scopeKey={`${workspaceId ?? ""}:${node.id}`}
        /> : null}
      </> : null}
    </aside>
  );
}

function GraphSceneNodeList({
  active,
  heading,
  empty,
  nodes,
  nodeVisuals,
  onBlur,
  onFocus,
  onSelect,
  open = false,
  selectedId,
  scopeKey,
  showKeyboardHint = false,
}: {
  active: boolean;
  heading: string;
  empty: string;
  nodes: GraphNode[];
  nodeVisuals: LocalNodeVisuals;
  onBlur: (id: string) => void;
  onFocus: (id: string) => void;
  onSelect: (id: string, origin: LocalGraphSelectionOrigin) => void;
  open?: boolean;
  selectedId: string | null;
  scopeKey: string;
  showKeyboardHint?: boolean;
}) {
  const { t } = useTranslation("graph");
  return <LocalGraphList
    active={active}
    className="graph-scene-navigation"
    defaultOpen={open}
    description={showKeyboardHint ? t("graph.sceneNavigation.keyboardHint", {
      defaultValue: "Tab into this list once. Use Up/Down or Home/End to highlight items. Press Enter or Space to select and focus Details.",
    }) : undefined}
    empty={empty} items={nodes} label={heading}
    onBlur={onBlur} onFocus={onFocus} onSelect={onSelect}
    renderItem={(node) => <><LocalNodeCue node={node} visuals={nodeVisuals} />
      <span><strong>{node.label}</strong><small>{graphLabel(t, "graphNodeFamily", node.family)}</small></span></>}
    scopeKey={scopeKey} selectedId={selectedId}
  />;
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

function GraphDetail({ label, value }: { label: string; value: string }) {
  return <><dt>{label}</dt><dd>{value}</dd></>;
}

function GraphIconButton({ children, disabled = false, label, onClick }: { children: React.ReactNode; disabled?: boolean; label: string; onClick: () => void }) {
  return <button aria-label={label} className="graph-icon-button" disabled={disabled} onClick={onClick} title={label} type="button">{children}</button>;
}

function nodeHref(node: GraphNode, workspaceId: string | null) {
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
) {
  const events = sortedTimeline(payload);
  const nodes = new Map(payload.nodes.map((node) => [node.id, node]));
  const edges = new Map(payload.edges.map((edge) => [edge.id, edge]));
  for (const event of events) {
    for (const node of timelineNodeSnapshots(event)) nodes.set(node.id, node);
    for (const edge of timelineEdgeSnapshots(event)) edges.set(edge.id, edge);
  }
  return events.map((event, index) => {
    const before = deriveGraphScene(payload, filters, index - 1);
    const after = deriveGraphScene(payload, filters, index);
    const visibleChange = graphSceneSignature(before) !== graphSceneSignature(after);
    const action = graphLabel(t, "graphActionKind", event.action_kind);
    const edge = [
      ...timelineEdgeSnapshots(event),
      ...(event.edge_ids ?? []).flatMap((id) => edges.get(id) ? [edges.get(id)!] : []),
    ][0];
    if (edge) {
      return {
        ...event,
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

function graphSceneSignature(scene: GraphScene) {
  return JSON.stringify({
    edges: scene.edges.map((edge) => edge),
    nodes: scene.nodes.map((node) => node),
  });
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
    action: t("graph.localToolbar.action", { defaultValue: "Actions" }),
  };
  return labels[kind];
}

function graphActionFamilyCue(actionKind: string) {
  const cues = {
    authoring: "✎",
    governance: "✓",
    retrieval: "◇",
    tool: "⚙",
    other: "A",
  } as const;
  return cues[graphActionFamily(actionKind)];
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
    const update = () => setTokens(readLocalGraphTokens(region, forcedColors));
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

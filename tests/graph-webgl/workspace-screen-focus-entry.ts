import { NativeGraph as Graph, type GraphConfig } from "../../frontend/src/features/graph/workspace/cosmosNative";
import { createElement, StrictMode, useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { WorkspaceGraphScreen } from "../../frontend/src/features/graph/WorkspaceGraphScreen";
import { isWorkspaceGraphResponse, type WorkspaceGraphOverviewRequest, type WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";
import { initI18n } from "../../frontend/src/shared/i18n";
import { freshWorkspaceGraphState, saveWorkspaceGraphState } from "../../frontend/src/features/graph/workspaceGraphModel";
import "../../frontend/src/styles/index.css";

type NativeGraphObservation = {
  id: number;
  ticks: number;
  positionUploads: number;
  renders: number;
  topologyUploads: number;
  nodeCount: number | null;
  destroys: number;
  callsAfterDestroy: number;
};

declare global {
  interface Window {
    __workspaceScreenFocus: {
      scene: WorkspaceGraphScene;
      queries: string[];
      requests: WorkspaceGraphOverviewRequest[];
      ready: boolean;
      role: "owner" | "viewer";
      holdQueries: boolean;
      subscribeAuthority(listener: () => void): () => void;
      changeRole(role: "owner" | "viewer"): void;
      readScene(request?: WorkspaceGraphOverviewRequest): Promise<WorkspaceGraphScene>;
      releaseQueries(): void;
      pointerHover(index: number | null): void;
      pointClick(index: number): void;
      pointScreen(index: number): number[];
      observeNativePointer(): void;
      simulationLifecycle(): NativeGraphObservation[];
      snapshot(): {
        points: number[] | null;
        links: number[] | null;
        selectedPoint: number | null;
        selectedLink: number | null;
        positions: number[];
        pinned: number[];
        running: boolean;
        layoutRunning: boolean | null;
        layoutTicks: number | null;
        physics: string | null;
        hoveredIndex: number | null;
        zoom: number | null;
        center: number[] | null;
        geometryUploads: number;
        topologyUploads: number;
        glError: number | null;
        pointGreyoutOpacity: number | null;
        linkGreyoutOpacity: number | null;
        focusedPointRingColor: GraphConfig["focusedPointRingColor"];
        linkAlphas: number[];
      };
    };
  }
}

const pc10k = new URLSearchParams(window.location.search).has("pc10k");
const count = pc10k ? 10000 : new URLSearchParams(window.location.search).has("mobileOverBudget") ? 2001 : 2000;
const scene: WorkspaceGraphScene = {
  schema_version: "workspace-graph-scene.v1",
  workspace_scope_considered: true,
  head_mode: "published",
  graph_revision: "screen-focus-0000000000000001",
  generated_at: "2026-10-03T00:00:00+00:00",
  algorithm_version: "screen-focus-fixture.v1",
  metric_basis: "visible_published_links_and_citations.pre_quick.v1",
  representation: "exact", completeness: "complete", truncated: false,
  counts: { candidate_documents: count, candidate_relations: count - 1,
    rendered_documents: count, rendered_clusters: 0, rendered_relations: count - 1 },
  omission_reasons: [], warnings: [], expansion_cursor: null,
  nodes: Array.from({ length: count }, (_, index) => ({
    grain: "document" as const,
    id: `context:${String(index).padStart(4, "0")}`,
    entity_kind: "context" as const,
    entity_id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
    label: `Keyboard Document ${String(index).padStart(4, "0")}`,
    path: `/context/${index}`, primary_folder_id: null, primary_folder_label: "Workspace",
    also_appears_in: [], reference_count: index === 0 ? 0 : 1,
    evidence_count: 0, relation_count: index === 0 || index === count - 1 ? 1 : 2,
    updated_at: "2026-10-03T00:00:00+00:00",
  })),
  edges: Array.from({ length: count - 1 }, (_, index) => ({
    id: `edge:${String(index).padStart(4, "0")}`,
    source: `context:${String(index).padStart(4, "0")}`,
    target: `context:${String(index + 1).padStart(4, "0")}`,
    relation_kind: "wikilink" as const, label: "links to", layer: "knowledge" as const,
    direction: "directed" as const, aggregate: false, relation_count: 1, cross_folder: false,
  })),
  facet_counts: { document_kinds: [{ value: "context", label: "Context", count }],
    relation_kinds: [{ value: "wikilink", label: "Wiki link", count: count - 1 }],
    folders: [], quick_filters: [] },
  capabilities: { expand: false, evidence: false, export: true, directory: true,
    compare: false, growth: false, provenance: false },
};
if (!isWorkspaceGraphResponse({ data: scene })) {
  throw new Error("Native Screen focus fixture does not satisfy the current workspace graph API contract.");
}

// The opt-in scale fixture carries real native buffers: 10k original document
// IDs and 30k distinct directed relations. Default 2k fixtures stay unchanged.
if (pc10k) {
  scene.edges = Array.from({ length: count * 3 }, (_, index) => {
    const source = Math.floor(index / 3);
    const offset = [1, 7, 31][index % 3];
    return {
      id: `scale-edge:${index}`, source: `context:${String(source).padStart(4, "0")}`,
      target: `context:${String((source + offset) % count).padStart(4, "0")}`,
      relation_kind: "wikilink" as const, label: "links to", layer: "knowledge" as const,
      direction: "directed" as const, aggregate: false, relation_count: 1, cross_folder: false,
    };
  });
  scene.nodes = scene.nodes.map((node) => ({ ...node, relation_count: 6 }));
  scene.counts.candidate_relations = scene.edges.length;
  scene.counts.rendered_relations = scene.edges.length;
  scene.facet_counts.relation_kinds[0].count = scene.edges.length;
}

// Only the authority-transition case uses this restricted Library item. The
// ordinary focus fixture remains byte-for-byte the same 2k Context chain.
if (new URLSearchParams(window.location.search).has("authority")) {
  const node = scene.nodes[count - 1];
  if (node.grain !== "document") throw new Error("Expected a document fixture");
  scene.nodes[count - 1] = { ...node, entity_kind: "library", label: "Owner-only Library" };
  scene.edges[count - 2] = {
    ...scene.edges[count - 2], relation_kind: "citation_source", layer: "evidence", label: "cites source",
  };
  scene.facet_counts.document_kinds = [
    { value: "context", label: "Context", count: count - 1 },
    { value: "library", label: "Library", count: 1 },
  ];
  scene.facet_counts.relation_kinds = [
    { value: "wikilink", label: "Wiki link", count: count - 2 },
    { value: "citation_source", label: "Source citation", count: 1 },
  ];
}
// Concentrate the real Screen's folder details instead of only exercising
// the ordinary document list. This remains a 2k native graph at either size.
const folderMembers = Number(new URLSearchParams(window.location.search).get("folderMembers"));
if (folderMembers > 0 && folderMembers <= count) {
  const folderId = "00000000-0000-4000-8000-000000003001";
  scene.nodes = scene.nodes.map((node, index) => node.grain === "document"
    ? { ...node, primary_folder_id: index < folderMembers ? folderId : null,
      primary_folder_label: index < folderMembers ? "Folder documents" : "Workspace" }
    : node);
  scene.facet_counts.folders = [{ value: folderId, label: "Folder documents", count: folderMembers }];
  const nodesById = new Map(scene.nodes.map((node) => [node.id, node]));
  scene.folder_topology = {
    schema_version: "workspace-graph-folders.v1",
    folders: [{ id: folderId, label: "Folder documents", parent_folder_id: null,
      direct_member_count: folderMembers }],
    memberships: scene.nodes.map((node) => {
      if (node.grain !== "document") throw new Error("Expected document folder fixture");
      return { node_id: node.id, folder_id: node.primary_folder_id,
        entity_kind: node.entity_kind, member_count: 1 };
    }),
    edge_slices: scene.edges.map((edge) => {
      const source = nodesById.get(edge.source)!;
      const target = nodesById.get(edge.target)!;
      if (source.grain !== "document" || target.grain !== "document") {
        throw new Error("Expected document folder relation fixture");
      }
      return { edge_id: edge.id, source_folder_id: source.primary_folder_id,
        source_entity_kind: source.entity_kind, target_folder_id: target.primary_folder_id,
        target_entity_kind: target.entity_kind, relation_count: edge.relation_count,
        occurrence_weight: 1 };
    }),
  };
  scene.edges = scene.edges.map((edge) => {
    const source = nodesById.get(edge.source)!;
    const target = nodesById.get(edge.target)!;
    return { ...edge, occurrence_weight: 1, cross_folder: source.grain === "document" && target.grain === "document"
      && source.primary_folder_id !== target.primary_folder_id };
  });
  if (!isWorkspaceGraphResponse({ data: scene })) throw new Error("Invalid folder-member fixture scene");
}
// Refine's large-facet fixture changes only this opt-in scene. All ordinary
// 2k focus/authority/member cases retain their original source and assertions.
if (new URLSearchParams(window.location.search).has("refineFolders")) {
  const folders = Array.from({ length: 300 }, (_, index) => ({
    value: `00000000-0000-4000-8000-${String(index + 4000).padStart(12, "0")}`,
    label: `Native folder ${String(index).padStart(3, "0")}`,
    count: Math.floor((count - 2 + 299 - index) / 300),
  }));
  scene.nodes = scene.nodes.map((node, index) => node.grain === "document" && index >= 2
    ? { ...node, primary_folder_id: folders[(index - 2) % 300].value,
      primary_folder_label: folders[(index - 2) % 300].label }
    : node);
  scene.facet_counts.folders = [...folders, { value: "unplaced", label: "No visible placement", count: 2 }];
  if (!isWorkspaceGraphResponse({ data: scene })) throw new Error("Invalid large Refine facet fixture");
}
const authorityListeners = new Set<() => void>();
const heldQueries: Array<() => void> = [];
function visibleScene(role: "owner" | "viewer"): WorkspaceGraphScene {
  if (role === "owner") return scene;
  const nodes = scene.nodes.filter((node) => node.grain !== "document" || node.entity_kind !== "library");
  const visibleIds = new Set(nodes.map((node) => node.id));
  const edges = scene.edges.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target));
  return {
    ...scene, nodes, edges, graph_revision: "screen-focus-viewer-0000000001",
    counts: { ...scene.counts, candidate_documents: nodes.length, rendered_documents: nodes.length,
      candidate_relations: edges.length, rendered_relations: edges.length },
    facet_counts: { ...scene.facet_counts,
      document_kinds: [{ value: "context", label: "Context", count: nodes.length }],
      relation_kinds: [{ value: "wikilink", label: "Wiki link", count: edges.length }] },
  };
}

/** Synthetic transport boundary only, not the real server's aggregation. */
function budgetedScaleScene(result: WorkspaceGraphScene, request?: WorkspaceGraphOverviewRequest): WorkspaceGraphScene {
  if (!pc10k || !request || result.nodes.length <= request.budget.documents) return result;
  const nodes = result.nodes.slice(0, request.budget.documents);
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = result.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
    .slice(0, request.budget.relations);
  const edgeIds = new Set(edges.map((edge) => edge.id));
  const memberships = result.folder_topology?.memberships.filter((row) => nodeIds.has(row.node_id));
  const folders = result.folder_topology?.folders.map((folder) => ({ ...folder,
    direct_member_count: memberships?.filter((row) => row.folder_id === folder.id).length ?? 0,
  })).filter((folder) => folder.direct_member_count > 0);
  return {
    ...result, nodes, edges, completeness: "partial", truncated: true,
    counts: { ...result.counts, rendered_documents: nodes.length, rendered_relations: edges.length },
    omission_reasons: ["document_budget"],
    folder_topology: result.folder_topology ? {
      ...result.folder_topology, folders: folders!, memberships: memberships!,
      edge_slices: result.folder_topology.edge_slices.filter((row) => edgeIds.has(row.edge_id)),
    } : undefined,
  };
}

// Observe real engine calls; every call still reaches the native Cosmos/WebGL
// implementation. Neither the Screen nor its renderer is replaced.
let graph: Graph | null = null;
let highlight: Partial<GraphConfig> = {};
let geometryUploads = 0;
let topologyUploads = 0;
let linkAlphas: number[] = [];
const pinsByGraph = new WeakMap<Graph, number[]>();
let hoveredIndex: number | null = null;
const pointerObservedGraphs = new WeakSet<Graph>();
function setObservedGraph(instance: Graph): void { graph = instance; }
const graphObservations: NativeGraphObservation[] = [];
const observationsByGraph = new WeakMap<Graph, NativeGraphObservation>();
function observeGraph(instance: Graph): NativeGraphObservation {
  const previous = observationsByGraph.get(instance);
  if (previous) return previous;
  const observation: NativeGraphObservation = { id: graphObservations.length + 1,
    ticks: 0, positionUploads: 0, renders: 0, topologyUploads: 0,
    nodeCount: null, destroys: 0, callsAfterDestroy: 0 };
  graphObservations.push(observation);
  observationsByGraph.set(instance, observation);
  const onTick = instance.config.onSimulationTick;
  // This native callback follows the complete upstream GPU force pipeline.
  // Forward it unchanged. CPU frames are position uploads, never GPU ticks.
  instance.config.onSimulationTick = (...args) => {
    if (observation.destroys > 0) observation.callsAfterDestroy += 1;
    onTick?.apply(instance.config, args);
    observation.ticks += 1;
    observation.nodeCount = instance.getGpuSimulationFrame()?.nodeCount ?? null;
  };
  return observation;
}
const renderGraph = Graph.prototype.render;
Graph.prototype.render = function (...args) {
  const observation = observeGraph(this);
  if (observation.destroys > 0) observation.callsAfterDestroy += 1;
  observation.renders += 1;
  return renderGraph.apply(this, args);
};
const destroyGraph = Graph.prototype.destroy;
Graph.prototype.destroy = function () {
  const observation = observeGraph(this);
  destroyGraph.call(this);
  observation.destroys += 1;
};
const setPinnedPoints = Graph.prototype.setPinnedPoints;
Graph.prototype.setPinnedPoints = function (...args) {
  const observation = observeGraph(this);
  if (observation.destroys > 0) observation.callsAfterDestroy += 1;
  const result = setPinnedPoints.apply(this, args);
  pinsByGraph.set(this, Array.from(args[0] ?? []));
  return result;
};
const setConfig = Graph.prototype.setConfigPartial;
Graph.prototype.setConfigPartial = function (config) {
  const observation = observeGraph(this);
  if (observation.destroys > 0) observation.callsAfterDestroy += 1;
  setObservedGraph(this);
  highlight = { ...highlight, ...config };
  return setConfig.call(this, config);
};
const setPoints = Graph.prototype.setPointPositions;
Graph.prototype.setPointPositions = function (...args) {
  const observation = observeGraph(this);
  if (observation.destroys > 0) observation.callsAfterDestroy += 1;
  observation.positionUploads += 1;
  observation.nodeCount = args[0].length / 2;
  setObservedGraph(this);
  geometryUploads += 1;
  return setPoints.apply(this, args);
};
const setLinks = Graph.prototype.setLinks;
Graph.prototype.setLinks = function (...args) {
  const observation = observeGraph(this);
  if (observation.destroys > 0) observation.callsAfterDestroy += 1;
  observation.topologyUploads += 1;
  topologyUploads += 1;
  return setLinks.apply(this, args);
};
const setLinkColors = Graph.prototype.setLinkColors;
Graph.prototype.setLinkColors = function (...args) {
  const observation = observeGraph(this);
  if (observation.destroys > 0) observation.callsAfterDestroy += 1;
  const colors = args[0];
  linkAlphas = colors ? Array.from(colors).filter((_value, index) => index % 4 === 3) : [];
  return setLinkColors.apply(this, args);
};
window.__workspaceScreenFocus = {
  scene, queries: [], requests: [], ready: false,
  role: "owner", holdQueries: false,
  subscribeAuthority(listener) {
    authorityListeners.add(listener);
    return () => { authorityListeners.delete(listener); };
  },
  changeRole(role) {
    window.__workspaceScreenFocus.role = role;
    authorityListeners.forEach((listener) => listener());
  },
  readScene(request) {
    const admitted = budgetedScaleScene(visibleScene(window.__workspaceScreenFocus.role), request);
    const focusNode = request?.focus_entity_id
      ? admitted.nodes.find(node => node.grain === "document" && node.entity_id === request.focus_entity_id) : null;
    const result = request?.focus_entity_id ? { ...admitted, focus: focusNode
      ? { status: "document" as const, node_id: focusNode.id }
      : { status: "unavailable" as const, node_id: null } } : admitted;
    if (!isWorkspaceGraphResponse({ data: result })) throw new Error("Invalid authority fixture scene");
    return window.__workspaceScreenFocus.holdQueries
      ? new Promise((resolve) => heldQueries.push(() => resolve(result)))
      : Promise.resolve(result);
  },
  releaseQueries() {
    window.__workspaceScreenFocus.holdQueries = false;
    heldQueries.splice(0).forEach((release) => release());
  },
  pointerHover(index) {
    if (index === null) graph?.config.onPointMouseOut?.(undefined);
    else graph?.config.onPointMouseOver?.(index, [0, 0], undefined, false, false);
  },
  pointClick(index) {
    const positions = graph?.getPointPositions();
    if (positions) graph?.config.onPointClick?.(index, [positions[index * 2], positions[index * 2 + 1]], new MouseEvent("click"));
  },
  pointScreen(index) {
    const positions = graph?.getPointPositions();
    const bounds = document.querySelector(".graph-canvas canvas")?.getBoundingClientRect();
    if (!graph || !positions || !bounds) throw new Error("Native graph is not mounted");
    const point = graph.spaceToScreenPosition([positions[index * 2], positions[index * 2 + 1]]);
    return [bounds.left + point[0], bounds.top + point[1]];
  },
  observeNativePointer() {
    if (!graph || pointerObservedGraphs.has(graph)) return;
    pointerObservedGraphs.add(graph);
    const over = graph.config.onPointMouseOver;
    const out = graph.config.onPointMouseOut;
    graph.setConfigPartial({
      onPointMouseOver(...args) { hoveredIndex = args[0]; over?.(...args); },
      onPointMouseOut(...args) { hoveredIndex = null; out?.(...args); },
    });
  },
  simulationLifecycle: () => graphObservations.map((observation) => ({ ...observation })),
  snapshot: () => {
    const canvas = document.querySelector<HTMLCanvasElement>(".graph-canvas canvas");
    const bounds = canvas?.getBoundingClientRect();
    const dataset = canvas?.closest<HTMLElement>(".graph-canvas")?.dataset;
    return {
      points: highlight.highlightedPointIndices ? Array.from(highlight.highlightedPointIndices) : null,
      links: highlight.highlightedLinkIndices ? Array.from(highlight.highlightedLinkIndices) : null,
      selectedPoint: highlight.focusedPointIndex ?? null,
      selectedLink: highlight.focusedLinkIndex ?? null,
      positions: Array.from(graph?.getPointPositions() ?? []),
      pinned: graph ? [...(pinsByGraph.get(graph) ?? [])] : [],
      running: graph?.isSimulationRunning ?? false,
      layoutRunning: dataset?.mapLayoutRunning === undefined ? null : dataset.mapLayoutRunning === "true",
      layoutTicks: dataset?.mapLayoutTicks === undefined ? null : Number(dataset.mapLayoutTicks),
      physics: dataset?.mapPhysics ?? null,
      hoveredIndex,
      zoom: graph?.getZoomLevel() ?? null,
      center: graph && bounds ? graph.screenToSpacePosition([bounds.width / 2, bounds.height / 2]) : null,
      geometryUploads,
      topologyUploads,
      glError: canvas?.getContext("webgl2")?.getError() ?? null,
      pointGreyoutOpacity: highlight.pointGreyoutOpacity ?? null,
      linkGreyoutOpacity: highlight.linkGreyoutOpacity ?? null,
      focusedPointRingColor: graph?.config.focusedPointRingColor,
      linkAlphas,
    };
  },
};
function FocusFixture() {
  useLayoutEffect(() => { window.__workspaceScreenFocus.ready = true; }, []);
  return createElement(WorkspaceGraphScreen);
}
function UnsupportedDocumentDestination() {
  const location = useLocation();
  return createElement("main", { "data-testid": "unsupported-document-destination",
    "data-document-path": location.pathname }, createElement("h1", null, "Opened document"));
}
async function mountFocusFixture(): Promise<void> {
  const language = new URLSearchParams(window.location.search).get("lang") === "ko" ? "ko" : "en";
  document.documentElement.lang = language;
  document.documentElement.dataset.theme = new URLSearchParams(window.location.search).get("theme") === "dark"
    ? "dark" : "light";
  await initI18n(language).changeLanguage(language);
  const navigationFocus = new URLSearchParams(window.location.search).has("navigationFocus");
  const focusEntityId = "00000000-0000-4000-8000-000000002000";
  if (navigationFocus) {
    const state = freshWorkspaceGraphState();
    state.viewerId = "focus-actor";
    state.viewportEngine = "cosmos";
    state.viewport = { zoom: .3, pan: { x: 39000, y: 25000 } };
    saveWorkspaceGraphState("focus-workspace", "focus-actor", scene.graph_revision, state);
  }
  if (new URLSearchParams(window.location.search).has("liveAuthority")) {
    // A single normal restorable pin, not a persisted layout. This shared
    // document remains visible to the viewer, so role revocation must clear
    // the owner's transient pin even though the document itself stays visible.
    const state = freshWorkspaceGraphState();
    state.viewerId = "focus-actor";
    state.viewportEngine = "cosmos";
    state.pinnedIds = ["context:0000"];
    state.pinnedPositions = { "context:0000": { x: 1234, y: 2345 } };
    saveWorkspaceGraphState("focus-workspace", "focus-actor", scene.graph_revision, state);
  }
  // This opt-in route proves that the real Screen's document link can leave
  // an unsupported graph. The destination is not the production Context API.
  const fixture = new URLSearchParams(window.location.search).has("unsupportedNavigation")
    ? createElement(Routes, null,
      createElement(Route, { path: "/graph", element: createElement(FocusFixture) }),
      createElement(Route, { path: "/context/:id", element: createElement(UnsupportedDocumentDestination) }))
    : createElement(FocusFixture);
  const application = createElement(MemoryRouter,
    { initialEntries: [navigationFocus ? `/graph?focus=${focusEntityId}` : "/graph"] }, fixture);
  createRoot(document.getElementById("root")!).render(navigationFocus ? createElement(StrictMode, null, application) : application);
}
void mountFocusFixture();

import {
  isWorkspaceGraphContainment,
  type WorkspaceGraphDataFilters,
  type WorkspaceGraphAvailableLens,
  type WorkspaceGraphEdge,
  type WorkspaceGraphFacetRule,
  type WorkspaceGraphNode,
  type WorkspaceGraphOverviewRequest,
  type WorkspaceGraphScene,
} from "../../shared/api/workspaceGraph";
import type {
  GraphRenderEdge,
  GraphRenderNode,
  GraphRenderScene,
} from "./shared/rendererTypes";
import { WORKSPACE_MAP_DEFAULT_BUDGET, type WorkspaceMapBudget } from "./shared/workspaceMapBudget";
import {
  cloneWorkspaceGraphFilters,
  DEFAULT_WORKSPACE_GRAPH_STATE,
  type WorkspaceGraphDisplaySettings,
  type WorkspaceGraphRestorableState,
} from "./workspace/workspaceGraphState";

export function workspaceGraphRequest(
  state: Pick<WorkspaceGraphRestorableState, "lens" | "dataFilters">,
  focusEntityId: string | null = null,
  budget: WorkspaceMapBudget = WORKSPACE_MAP_DEFAULT_BUDGET,
): WorkspaceGraphOverviewRequest {
  const includes = state.dataFilters.facet_rules
    .filter((rule) => rule.facet === "folder" && rule.mode !== "exclude")
    .map((rule) => rule.value);
  const excludes = state.dataFilters.facet_rules
    .filter((rule) => rule.facet === "folder" && rule.mode === "exclude")
    .map((rule) => rule.value);
  const only = state.dataFilters.facet_rules
    .filter((rule) => rule.facet === "folder" && rule.mode === "only")
    .map((rule) => rule.value);
  const search = state.dataFilters.search.trim();
  return {
    schema_version: "workspace-graph-overview.v1",
    head_mode: "published",
    lens: state.lens,
    document_kinds: [...state.dataFilters.document_kinds],
    ...(search ? { search } : {}),
    quick_filters: [...state.dataFilters.quick_filters],
    folders: only.length
      ? { include: [], exclude: [], only }
      : { include: includes, exclude: excludes, only: [] },
    time: {
      basis: state.dataFilters.time_basis,
      ...(state.dataFilters.time_from ? { from: state.dataFilters.time_from } : {}),
      ...(state.dataFilters.time_to ? { to: state.dataFilters.time_to } : {}),
    },
    budget: { ...budget },
    // The server reserves a slot for the deep-link target so an aggregated
    // scene still contains it (or names the cluster that does).
    ...(focusEntityId ? { focus_entity_id: focusEntityId } : {}),
  };
}

/** Count pending effective filter dimensions, not draft fields or display state. */
export function countPendingRefineFilters(
  draft: WorkspaceGraphDataFilters,
  active: WorkspaceGraphDataFilters,
): number {
  // Reuse the request projection so Only suppression, trimmed server search,
  // and omitted time bounds cannot drift from what Apply actually sends.
  const draftRequest = workspaceGraphRequest({ lens: DEFAULT_WORKSPACE_GRAPH_STATE.lens, dataFilters: draft });
  const activeRequest = workspaceGraphRequest({ lens: DEFAULT_WORKSPACE_GRAPH_STATE.lens, dataFilters: active });
  let count = Number(draftRequest.search !== activeRequest.search)
    + Number(setDifferenceCount(draftRequest.document_kinds, activeRequest.document_kinds) > 0)
    + Number(draftRequest.time.basis !== activeRequest.time.basis
      || draftRequest.time.from !== activeRequest.time.from
      || draftRequest.time.to !== activeRequest.time.to)
    + setDifferenceCount(draftRequest.quick_filters, activeRequest.quick_filters);
  const draftFolders = effectiveFolderModes(draftRequest.folders);
  const activeFolders = effectiveFolderModes(activeRequest.folders);
  for (const value of new Set([...draftFolders.keys(), ...activeFolders.keys()])) {
    if (draftFolders.get(value) !== activeFolders.get(value)) count += 1;
  }
  return count;
}

export function refineFiltersDiffer(
  draft: WorkspaceGraphDataFilters,
  active: WorkspaceGraphDataFilters,
): boolean {
  return countPendingRefineFilters(draft, active) > 0;
}

function setDifferenceCount(left: readonly string[], right: readonly string[]): number {
  const leftSet = new Set(left);
  const rightSet = new Set(right);
  let count = 0;
  for (const value of leftSet) if (!rightSet.has(value)) count += 1;
  for (const value of rightSet) if (!leftSet.has(value)) count += 1;
  return count;
}

function effectiveFolderModes(folders: WorkspaceGraphOverviewRequest["folders"]): Map<string, number> {
  const modes = new Map<string, number>();
  for (const [mode, flag] of [["include", 1], ["exclude", 2], ["only", 4]] as const) {
    for (const value of folders[mode]) modes.set(value, (modes.get(value) ?? 0) | flag);
  }
  return modes;
}

/** What the map currently shows, before any visible-map search. */
export type WorkspaceGraphDisplayItems = {
  nodes: WorkspaceGraphNode[];
  edges: WorkspaceGraphEdge[];
};

export function workspaceGraphDisplayItems(
  payload: WorkspaceGraphScene,
  display: Pick<WorkspaceGraphDisplaySettings, "show_orphans" | "show_edges">,
): WorkspaceGraphDisplayItems {
  const hiddenOrphans = display.show_orphans ? new Set<string>() : workspaceGraphOrphanIds(payload);
  const nodes = payload.nodes.filter((node) => !hiddenOrphans.has(node.id));
  const ids = new Set(nodes.map((node) => node.id));
  const edges = display.show_edges
    ? payload.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target))
    : [];
  return { nodes, edges };
}

export function normalizeWorkspaceGraphSearch(search: string): string {
  return search.trim().toLocaleLowerCase();
}

/** The one node match rule: map highlight and sidebar results share it. */
export function workspaceGraphNodeMatchesSearch(node: WorkspaceGraphNode, normalizedSearch: string): boolean {
  return !normalizedSearch || [
    node.label,
    node.grain === "document" ? node.primary_folder_label : node.grain === "folder" ? node.path : null,
    node.grain === "document" ? node.entity_kind : node.grain,
  ]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase()
    .includes(normalizedSearch);
}

export function workspaceGraphRenderScene(
  payload: WorkspaceGraphScene,
  display: WorkspaceGraphDisplaySettings,
  search = "",
  lens: WorkspaceGraphAvailableLens = DEFAULT_WORKSPACE_GRAPH_STATE.lens,
): GraphRenderScene {
  const normalizedSearch = normalizeWorkspaceGraphSearch(search);
  const items = workspaceGraphDisplayItems(payload, display);
  const matchingNodes = items.nodes.filter((node) => workspaceGraphNodeMatchesSearch(node, normalizedSearch));
  const matchingIds = new Set(matchingNodes.map((node) => node.id));
  const nodes = matchingNodes.map((node) => renderNode(node, display.show_labels, lens));
  const edges = items.edges
    .filter((edge) => matchingIds.has(edge.source) && matchingIds.has(edge.target))
    .map(renderEdge);
  return { nodes, edges };
}

/** Sidebar relation groups, in display order: both ends, kind name, then one end by direction. */
export const WORKSPACE_GRAPH_RELATION_GROUPS = ["both", "kind", "outgoing", "incoming"] as const;
export type WorkspaceGraphRelationGroup = typeof WORKSPACE_GRAPH_RELATION_GROUPS[number];

export type WorkspaceGraphSearchResults = {
  nodes: WorkspaceGraphNode[];
  relations: Record<WorkspaceGraphRelationGroup, WorkspaceGraphEdge[]>;
  relationCount: number;
};

/**
 * Sidebar results for the visible-map search. A relation is a result when
 * either end matches or its kind names contain the query; `outgoing` and
 * `incoming` are relative to the matched documents. Scene order is kept
 * inside each group; documents whose name starts with the query come first.
 */
export function searchWorkspaceGraphItems(
  items: WorkspaceGraphDisplayItems,
  search: string,
  relationNames: (kind: string) => string,
): WorkspaceGraphSearchResults | null {
  const normalizedSearch = normalizeWorkspaceGraphSearch(search);
  if (!normalizedSearch) return null;
  const matched = items.nodes.filter((node) => workspaceGraphNodeMatchesSearch(node, normalizedSearch));
  const nodes = [
    ...matched.filter((node) => node.label.toLocaleLowerCase().startsWith(normalizedSearch)),
    ...matched.filter((node) => !node.label.toLocaleLowerCase().startsWith(normalizedSearch)),
  ];
  const ids = new Set(nodes.map((node) => node.id));
  const relations: WorkspaceGraphSearchResults["relations"] = { both: [], kind: [], outgoing: [], incoming: [] };
  const kindMatches = new Map<string, boolean>();
  const kindMatch = (kind: string) => {
    let match = kindMatches.get(kind);
    if (match === undefined) {
      match = relationNames(kind).toLocaleLowerCase().includes(normalizedSearch);
      kindMatches.set(kind, match);
    }
    return match;
  };
  for (const edge of items.edges) {
    const source = ids.has(edge.source);
    const target = ids.has(edge.target);
    if (source && target) relations.both.push(edge);
    else if (kindMatch(edge.relation_kind)) relations.kind.push(edge);
    else if (source) relations.outgoing.push(edge);
    else if (target) relations.incoming.push(edge);
  }
  const relationCount = WORKSPACE_GRAPH_RELATION_GROUPS.reduce((count, group) => count + relations[group].length, 0);
  return { nodes, relations, relationCount };
}

export type WorkspaceGraphNodeRelations = {
  incoming: WorkspaceGraphEdge[];
  outgoing: WorkspaceGraphEdge[];
};

/**
 * One node's relations split by direction, each ordered by the other end's
 * label. An undirected relation is listed once, on the side its stored
 * source puts it.
 */
export function workspaceGraphNodeRelations(
  items: WorkspaceGraphDisplayItems,
  nodeId: string,
  labels: ReadonlyMap<string, string>,
): WorkspaceGraphNodeRelations {
  const byOther = (other: (edge: WorkspaceGraphEdge) => string) =>
    (left: WorkspaceGraphEdge, right: WorkspaceGraphEdge) =>
      (labels.get(other(left)) ?? "").localeCompare(labels.get(other(right)) ?? "");
  return {
    incoming: items.edges.filter((edge) => edge.target === nodeId && edge.source !== nodeId)
      .sort(byOther((edge) => edge.source)),
    outgoing: items.edges.filter((edge) => edge.source === nodeId)
      .sort(byOther((edge) => edge.target)),
  };
}

/** A scene's visible edges, skipping dangling and self ends. */
function visibleSceneEdges(payload: WorkspaceGraphScene): WorkspaceGraphEdge[] {
  const nodeIds = new Set(payload.nodes.map((node) => node.id));
  return payload.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target)
    && edge.source !== edge.target);
}

/**
 * Unlinked documents. Only classify them when the authoritative scene is
 * complete: a missing edge in a capped/aggregated response is not evidence of
 * an orphan. Counts are actor-visible metrics; incoming-only counts alone are
 * insufficient, so an outgoing scene relation also keeps its document in the
 * map. Every relation links, containment included.
 */
export function workspaceGraphOrphanIds(payload: WorkspaceGraphScene): Set<string> {
  if (payload.representation !== "exact" || payload.completeness !== "complete" || payload.truncated) {
    return new Set();
  }
  const connected = new Set(visibleSceneEdges(payload).flatMap((edge) => [edge.source, edge.target]));
  return new Set(payload.nodes.filter((node) => node.grain === "document"
    && node.reference_count === 0 && node.evidence_count === 0
    && !connected.has(node.id) && payload.focus?.node_id !== node.id).map((node) => node.id));
}

/** What a container holds among the shown nodes; drawing lines or not does not change it. */
export function workspaceGraphHeldCount(
  payload: WorkspaceGraphScene,
  containerId: string,
  shown: ReadonlySet<string>,
): number {
  return visibleSceneEdges(payload).filter((edge) => edge.source === containerId
    && isWorkspaceGraphContainment(edge) && shown.has(edge.target)).length;
}

export function estimateWorkspaceGraphImpact(
  payload: WorkspaceGraphScene | null,
  filters: WorkspaceGraphDataFilters,
) {
  if (!payload) return null;
  const constrainingCounts: number[] = [];
  for (const rule of filters.facet_rules) {
    if (rule.mode === "exclude") continue;
    const count = workspaceGraphFacetCount(payload, rule.facet, rule.value);
    if (count !== null) constrainingCounts.push(count);
  }
  return Math.min(
    payload.counts.candidate_documents,
    ...(constrainingCounts.length ? constrainingCounts : [payload.counts.candidate_documents]),
  );
}

export function updateFacetRule(
  filters: WorkspaceGraphDataFilters,
  rule: WorkspaceGraphFacetRule,
) {
  const withoutConflicts = filters.facet_rules.filter((entry) => {
    if (entry.facet !== rule.facet) return true;
    if (entry.value === rule.value) return false;
    if (rule.facet !== "folder") return true;
    return rule.mode !== "only" && entry.mode !== "only";
  });
  return {
    ...cloneWorkspaceGraphFilters(filters),
    facet_rules: [
      ...withoutConflicts,
      rule,
    ],
  };
}

export function removeFacetRule(
  filters: WorkspaceGraphDataFilters,
  facet: WorkspaceGraphFacetRule["facet"],
  value: string,
) {
  return {
    ...cloneWorkspaceGraphFilters(filters),
    facet_rules: filters.facet_rules.filter((entry) =>
      entry.facet !== facet || entry.value !== value,
    ),
  };
}

function renderNode(
  node: WorkspaceGraphNode,
  showLabel: boolean,
  lens: WorkspaceGraphAvailableLens,
): GraphRenderNode {
  const family = node.grain === "document" ? "document" : "structural";
  const metricCount = node.grain === "document"
    ? lens === "links"
      ? node.reference_count
      : lens === "evidence"
        ? node.evidence_count
        : lens === "combined"
          ? node.reference_count + node.evidence_count
          : null
    : null;
  const metricSize = metricCount === null ? null : workspaceGraphMetricSize(metricCount);
  return {
    id: node.id,
    label: node.label,
    canvasLabel: showLabel ? node.label : "",
    family,
    status: "",
    is_seed: false,
    entityKind: node.grain === "document"
      ? node.entity_kind === "context" ? "context_page" : node.entity_kind === "agent" ? "agent_page" : "library_item"
      : node.grain,
    grain: node.grain === "cluster" ? node.cluster_kind : node.grain,
    referenceCount: node.grain === "document" ? node.reference_count
      : node.grain === "cluster" ? node.relation_count : undefined,
    metricSized: metricSize !== null,
    nodeWidth: metricSize?.width,
    nodeHeight: metricSize?.height,
  };
}

/** Bounded metric encoding: perceptible at low counts without giant outliers. */
export function workspaceGraphMetricSize(count: number) {
  const normalized = Math.sqrt(Math.max(0, count));
  return {
    width: Math.min(96, 58 + Math.round(normalized * 5)),
    height: Math.min(68, 44 + Math.round(normalized * 3)),
  };
}

function renderEdge(edge: WorkspaceGraphScene["edges"][number]): GraphRenderEdge {
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    label: edge.label ?? edge.relation_kind,
    layer: edge.layer,
    edgeKind: edge.relation_kind,
    actionKind: "",
    actionFamily: "other",
    directed: edge.direction === "directed" ? "true" : "false",
    aggregate: edge.aggregate ? "true" : "false",
    count: edge.relation_count,
    crossFolder: edge.cross_folder ? "true" : "false",
  };
}

function workspaceGraphFacetCount(
  payload: WorkspaceGraphScene,
  facet: WorkspaceGraphFacetRule["facet"],
  value: string,
) {
  const group = facet === "folder"
    ? payload.facet_counts.folders
    : payload.facet_counts.document_kinds;
  return group.find((entry) => entry.value === value)?.count ?? null;
}

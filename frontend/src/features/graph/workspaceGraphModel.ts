import {
  isWorkspaceGraphContainment,
  type WorkspaceGraphDataFilters,
  type WorkspaceGraphAvailableLens,
  type WorkspaceGraphDocumentKind,
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
  GraphViewport,
} from "./shared/rendererTypes";
import { WORKSPACE_MAP_DEFAULT_BUDGET, type WorkspaceMapBudget, type WorkspaceMapScale } from "./shared/workspaceMapBudget";
import {
  clearLegacyWorkspaceGraphStorage,
  clearWorkspaceGraphStorage,
  workspaceGraphStorageScope,
} from "../../shared/lib/workspaceGraphStorage";

export type WorkspaceGraphDisplaySettings = {
  show_edges: boolean;
  show_labels: boolean;
  show_orphans: boolean;
};

export type WorkspaceGraphRestorableState = {
  lens: WorkspaceGraphAvailableLens;
  dataFilters: WorkspaceGraphDataFilters;
  display: WorkspaceGraphDisplaySettings;
  selectedId: string | null;
  pinnedIds: string[];
  pinnedPositions: Record<string, { x: number; y: number }>;
  viewport: GraphViewport | null;
  /** Prevent old Cytoscape camera and another actor's transient state restoring into Cosmos. */
  viewportEngine?: "cosmos";
  viewerId?: string;
};

/**
 * Camera and pins are coordinates of one scale's layout: the 2K CPU worker and
 * the 10K native GPU solver place a document at unrelated points. They persist
 * per scale and are never applied to the other one; selection stays shared.
 */
type WorkspaceGraphScaleView = Pick<WorkspaceGraphRestorableState, "viewport" | "pinnedIds" | "pinnedPositions">;
type VersionedWorkspaceGraphScaleView = WorkspaceGraphScaleView & { coordinateVersion: number };
type WorkspaceGraphScaleViews = Partial<Record<WorkspaceMapScale, VersionedWorkspaceGraphScaleView>>;
const WORKSPACE_MAP_SCALES: readonly WorkspaceMapScale[] = ["2k", "10k"];
// Coordinate compatibility belongs to the selected layout, not the renderer
// or backend graph revision. D3 replaces the previous 2K coordinate frame;
// native 10K keeps its existing frame, including records without a version.
const COORDINATE_VERSIONS: Readonly<Record<WorkspaceMapScale, number>> = { "2k": 2, "10k": 1 };
// Records written before scale keying carry no scale. 2K was the only layout
// then; a wrong guess would put a 2K camera on the 10K scene again.
const LEGACY_VIEW_SCALE: WorkspaceMapScale = "2k";
// Cosmos accepts zooms down to 1e-3. Preserve valid wide-layout fits below the
// legacy 0.05 storage floor rather than restoring them too close.
const MIN_VIEWPORT_ZOOM = 0.001;

export const DEFAULT_WORKSPACE_GRAPH_FILTERS: WorkspaceGraphDataFilters = {
  search: "",
  document_kinds: ["context", "library", "agent"],
  time_basis: "document_updated",
  time_from: null,
  time_to: null,
  quick_filters: [],
  facet_rules: [],
};

export const DEFAULT_WORKSPACE_GRAPH_STATE: WorkspaceGraphRestorableState = {
  lens: "combined",
  dataFilters: DEFAULT_WORKSPACE_GRAPH_FILTERS,
  display: { show_edges: true, show_labels: true, show_orphans: false },
  selectedId: null,
  pinnedIds: [],
  pinnedPositions: {},
  viewport: null,
};

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
  const hiddenOrphans = display.show_orphans ? new Set<string>() : workspaceGraphHiddenUnlinkedIds(payload);
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
 * map. Being placed in a folder does not link a document.
 */
export function workspaceGraphOrphanIds(payload: WorkspaceGraphScene): Set<string> {
  if (payload.representation !== "exact" || payload.completeness !== "complete" || payload.truncated) {
    return new Set();
  }
  const connected = new Set(visibleSceneEdges(payload).filter((edge) => !isWorkspaceGraphContainment(edge))
    .flatMap((edge) => [edge.source, edge.target]));
  return new Set(payload.nodes.filter((node) => node.grain === "document"
    && node.reference_count === 0 && node.evidence_count === 0
    && !connected.has(node.id) && payload.focus?.node_id !== node.id).map((node) => node.id));
}

/**
 * What hiding the unlinked documents hides: those documents and every folder
 * whose held nodes are all hidden, since it has nothing left to show.
 */
export function workspaceGraphHiddenUnlinkedIds(
  payload: WorkspaceGraphScene,
  orphans: ReadonlySet<string> = workspaceGraphOrphanIds(payload),
): Set<string> {
  const hidden = new Set(orphans);
  if (!hidden.size) return hidden;
  const folders = new Set(payload.nodes.filter((node) => node.grain === "folder").map((node) => node.id));
  const held = new Map<string, string[]>();
  for (const edge of visibleSceneEdges(payload)) {
    if (folders.has(edge.source) && isWorkspaceGraphContainment(edge)) {
      held.set(edge.source, [...held.get(edge.source) ?? [], edge.target]);
    }
  }
  for (let grew = true; grew;) {
    grew = false;
    for (const [folder, items] of held) {
      if (hidden.has(folder) || !items.every((item) => hidden.has(item))) continue;
      hidden.add(folder);
      grew = true;
    }
  }
  return hidden;
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
    ...cloneFilters(filters),
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
    ...cloneFilters(filters),
    facet_rules: filters.facet_rules.filter((entry) =>
      entry.facet !== facet || entry.value !== value,
    ),
  };
}

/**
 * With `mapScale`, camera and pins come only from the view that scale captured;
 * the other scale's view stays parked in the record. Without it, the record's
 * top-level view is returned. Incompatible coordinate versions clear only
 * camera and pins, preserving scale-neutral settings and selection.
 */
export function loadWorkspaceGraphState(
  workspaceId: string,
  actorId: string,
  revision?: string | null,
  onInvalidOwnerRestoration?: () => void,
  mapScale?: WorkspaceMapScale,
): WorkspaceGraphRestorableState {
  if (typeof window === "undefined" || !workspaceId || !actorId) return freshWorkspaceGraphState();
  clearLegacyWorkspaceGraphStorage();
  try {
    const exactKey = storageKey(workspaceId, actorId, revision || "latest");
    const stored = window.sessionStorage.getItem(exactKey);
    if (!stored) return freshWorkspaceGraphState();
    const parsed = JSON.parse(stored) as StoredWorkspaceGraphState;
    const normalized = normalizeStoredState(parsed.state ?? parsed);
    if (normalized.viewerId !== actorId) {
      clearWorkspaceGraphState(workspaceId, actorId);
      onInvalidOwnerRestoration?.();
      return freshWorkspaceGraphState();
    }
    if (revision && parsed.revision === revision) {
      const scale = mapScale ?? normalizeScale(parsed.mapScale) ?? LEGACY_VIEW_SCALE;
      return { ...normalized, ...storedScaleView(parsed, normalized, scale) };
    }
    return {
      ...normalized,
      selectedId: null,
      pinnedIds: [],
      pinnedPositions: {},
      viewport: null,
    };
  } catch {
    return freshWorkspaceGraphState();
  }
}

/** Store camera and pins with their scale's coordinate version; default to 2K. */
export function saveWorkspaceGraphState(
  workspaceId: string,
  actorId: string,
  revision: string,
  state: WorkspaceGraphRestorableState,
  mapScale?: WorkspaceMapScale,
) {
  if (typeof window === "undefined" || !workspaceId || !actorId || (state.viewerId && state.viewerId !== actorId)) return;
  try {
    const serialized = JSON.stringify({
      revision,
      state: { ...state, viewerId: actorId },
      ...scaleFields(readStoredRecord(workspaceId, actorId, revision), mapScale ?? LEGACY_VIEW_SCALE),
    });
    window.sessionStorage.setItem(storageKey(workspaceId, actorId, revision), serialized);
    window.sessionStorage.setItem(storageKey(workspaceId, actorId, "latest"), serialized);
  } catch {
    // Persistence is optional when browser storage is unavailable/full.
  }
}

export function clearWorkspaceGraphState(workspaceId: string, actorId: string) {
  clearLegacyWorkspaceGraphStorage();
  clearWorkspaceGraphStorage(workspaceGraphStorageScope(workspaceId, actorId));
}

type StoredWorkspaceGraphState = Partial<WorkspaceGraphRestorableState> & {
  revision?: string;
  state?: Partial<WorkspaceGraphRestorableState>;
  /** Scale that owns the record's own camera and pins. */
  mapScale?: unknown;
  coordinateVersion?: unknown;
  /** The other scale's camera and pins, kept so a visit cannot erase them. */
  scaleViews?: unknown;
};

function emptyScaleView(): WorkspaceGraphScaleView {
  return { viewport: null, pinnedIds: [], pinnedPositions: {} };
}

function ownScaleView(state: WorkspaceGraphScaleView): WorkspaceGraphScaleView {
  return { viewport: state.viewport, pinnedIds: state.pinnedIds, pinnedPositions: state.pinnedPositions };
}

function normalizeScale(value: unknown): WorkspaceMapScale | null {
  return WORKSPACE_MAP_SCALES.find((scale) => scale === value) ?? null;
}

function normalizeScaleView(value: Record<string, unknown>): WorkspaceGraphScaleView {
  const pinnedIds = uniqueStrings(value.pinnedIds, isGraphId).slice(0, 250);
  return {
    viewport: normalizeViewport(value.viewport),
    pinnedIds,
    pinnedPositions: normalizePinnedPositions(value.pinnedPositions, pinnedIds),
  };
}

function normalizeScaleViews(value: unknown): WorkspaceGraphScaleViews {
  const views: WorkspaceGraphScaleViews = {};
  if (!isRecord(value)) return views;
  for (const scale of WORKSPACE_MAP_SCALES) {
    const view = value[scale];
    if (isRecord(view) && compatibleCoordinateVersion(view, scale)) {
      views[scale] = { ...normalizeScaleView(view), coordinateVersion: COORDINATE_VERSIONS[scale] };
    }
  }
  return views;
}

function compatibleCoordinateVersion(value: { coordinateVersion?: unknown }, scale: WorkspaceMapScale): boolean {
  return value.coordinateVersion === COORDINATE_VERSIONS[scale]
    || scale === "10k" && value.coordinateVersion === undefined;
}

function storedScaleView(
  record: StoredWorkspaceGraphState,
  state: WorkspaceGraphRestorableState,
  scale: WorkspaceMapScale,
): WorkspaceGraphScaleView {
  if ((normalizeScale(record.mapScale) ?? LEGACY_VIEW_SCALE) === scale) {
    return compatibleCoordinateVersion(record, scale) ? ownScaleView(state) : emptyScaleView();
  }
  const parked = normalizeScaleViews(record.scaleViews)[scale];
  return parked ? ownScaleView(parked) : emptyScaleView();
}

/** The same-revision record this actor already saved, or null when none is trustworthy. */
function readStoredRecord(workspaceId: string, actorId: string, revision: string) {
  try {
    const stored = window.sessionStorage.getItem(storageKey(workspaceId, actorId, revision));
    if (!stored) return null;
    const record = JSON.parse(stored) as StoredWorkspaceGraphState;
    const state = normalizeStoredState(record.state ?? record);
    return record.revision === revision && state.viewerId === actorId ? { record, state } : null;
  } catch {
    // A damaged record is overwritten by the save that asked for it.
    return null;
  }
}

/** Tag the record's own view with its scale and park the other scale's view beside it. */
function scaleFields(
  previous: ReturnType<typeof readStoredRecord>,
  scale: WorkspaceMapScale,
): { mapScale: WorkspaceMapScale; coordinateVersion: number; scaleViews?: WorkspaceGraphScaleViews } {
  const parked = previous ? normalizeScaleViews(previous.record.scaleViews) : {};
  const previousScale = normalizeScale(previous?.record.mapScale) ?? LEGACY_VIEW_SCALE;
  if (previous && previousScale !== scale) {
    const view = storedScaleView(previous.record, previous.state, previousScale);
    if (view.viewport || view.pinnedIds.length) {
      parked[previousScale] = { ...view, coordinateVersion: COORDINATE_VERSIONS[previousScale] };
    }
  }
  delete parked[scale];
  return { mapScale: scale, coordinateVersion: COORDINATE_VERSIONS[scale],
    ...(Object.keys(parked).length ? { scaleViews: parked } : {}) };
}

export function freshWorkspaceGraphState(): WorkspaceGraphRestorableState {
  return {
    ...DEFAULT_WORKSPACE_GRAPH_STATE,
    dataFilters: cloneFilters(DEFAULT_WORKSPACE_GRAPH_FILTERS),
    display: { ...DEFAULT_WORKSPACE_GRAPH_STATE.display },
    pinnedIds: [],
    pinnedPositions: {},
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

function normalizeStoredState(
  value: unknown,
): WorkspaceGraphRestorableState {
  const state = isRecord(value) ? value : {};
  const allowedLenses: WorkspaceGraphAvailableLens[] = ["combined", "links", "evidence", "directory", "compare"];
  const lens = typeof state.lens === "string"
    && allowedLenses.includes(state.lens as WorkspaceGraphAvailableLens)
    ? state.lens as WorkspaceGraphAvailableLens
    : DEFAULT_WORKSPACE_GRAPH_STATE.lens;
  const storedFilters = isRecord(state.dataFilters) ? state.dataFilters : {};
  const documentKinds = uniqueStrings(storedFilters.document_kinds, isDocumentKind);
  const quickFilters = uniqueStrings(storedFilters.quick_filters, isQuickFilter);
  const facetRules = normalizeFacetRules(storedFilters.facet_rules);
  const from = normalizeTimestamp(storedFilters.time_from);
  const to = normalizeTimestamp(storedFilters.time_to);
  const validWindow = from === null || to === null || from <= to;
  const { viewport, pinnedIds, pinnedPositions } = normalizeScaleView(state);
  return {
    lens,
    dataFilters: {
      search: typeof storedFilters.search === "string"
        ? storedFilters.search.slice(0, 200)
        : "",
      document_kinds: documentKinds.length ? documentKinds : ["context", "library", "agent"],
      time_basis: storedFilters.time_basis === "relation_observed"
        ? "relation_observed"
        : "document_updated",
      time_from: validWindow ? from : null,
      time_to: validWindow ? to : null,
      quick_filters: quickFilters,
      facet_rules: facetRules,
    },
    display: {
      show_edges: !isRecord(state.display) || state.display.show_edges !== false,
      show_labels: !isRecord(state.display) || state.display.show_labels !== false,
      show_orphans: isRecord(state.display) && state.display.show_orphans === true,
    },
    selectedId: isGraphId(state.selectedId) ? state.selectedId : null,
    pinnedIds,
    pinnedPositions,
    viewport,
    ...(state.viewportEngine === "cosmos" ? { viewportEngine: "cosmos" as const } : {}),
    ...(typeof state.viewerId === "string" && state.viewerId.length > 0 && state.viewerId.length <= 128
      ? { viewerId: state.viewerId } : {}),
  };
}

function isDocumentKind(value: unknown): value is WorkspaceGraphDocumentKind {
  return value === "context" || value === "library" || value === "agent";
}

function isQuickFilter(value: unknown): value is WorkspaceGraphDataFilters["quick_filters"][number] {
  return value === "orphans"
    || value === "highly_referenced"
    || value === "cross_folder"
    || value === "recently_connected";
}

function isGraphId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 220;
}

function normalizeFacetRules(value: unknown): WorkspaceGraphFacetRule[] {
  if (!Array.isArray(value)) return [];
  const rules: WorkspaceGraphFacetRule[] = [];
  const seen = new Set<string>();
  for (const candidate of value) {
    if (!isRecord(candidate)
      || (candidate.facet !== "folder" && candidate.facet !== "document_kind")
      || (candidate.mode !== "include" && candidate.mode !== "exclude" && candidate.mode !== "only")
      || typeof candidate.value !== "string") continue;
    if (candidate.facet === "folder" && candidate.value !== "unplaced" && !isUuid(candidate.value)) continue;
    if (candidate.facet === "document_kind" && !isDocumentKind(candidate.value)) continue;
    const key = `${candidate.facet}:${candidate.mode}:${candidate.value}`;
    if (seen.has(key)) continue;
    seen.add(key);
    rules.push({
      facet: candidate.facet,
      mode: candidate.mode,
      value: candidate.value,
    });
    if (rules.length === 64) break;
  }
  const folderOnly = rules.find((rule) => rule.facet === "folder" && rule.mode === "only");
  return folderOnly
    ? [folderOnly, ...rules.filter((rule) => rule.facet !== "folder")]
    : rules;
}

function normalizeTimestamp(value: unknown) {
  if (typeof value !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null;
}

function normalizePinnedPositions(
  value: unknown,
  pinnedIds: string[],
): Record<string, { x: number; y: number }> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(pinnedIds.flatMap((id) => {
    const point = value[id];
    return isPoint(point) ? [[id, point]] : [];
  }));
}

function normalizeViewport(value: unknown): GraphViewport | null {
  if (!isRecord(value) || !isPoint(value.pan) || !isFiniteNumber(value.zoom) || value.zoom <= 0) {
    return null;
  }
  return {
    zoom: Math.min(10, Math.max(MIN_VIEWPORT_ZOOM, value.zoom)),
    pan: value.pan,
  };
}

function isPoint(value: unknown): value is { x: number; y: number } {
  return isRecord(value) && isFiniteNumber(value.x) && isFiniteNumber(value.y);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isUuid(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function uniqueStrings<T extends string>(
  value: unknown,
  predicate: (candidate: unknown) => candidate is T,
): T[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter(predicate))];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function cloneFilters(filters: WorkspaceGraphDataFilters): WorkspaceGraphDataFilters {
  return {
    ...filters,
    document_kinds: [...filters.document_kinds],
    quick_filters: [...filters.quick_filters],
    facet_rules: filters.facet_rules.map((rule) => ({ ...rule })),
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

function storageKey(workspaceId: string, actorId: string, revision: string) {
  return `${workspaceGraphStorageScope(workspaceId, actorId)}${encodeURIComponent(revision)}`;
}

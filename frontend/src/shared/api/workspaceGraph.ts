import { apiRequest } from "./client";
import type { components } from "./types.gen";

// Whether the Knowledge Map is switched on for one workspace, for the caller.
export type WorkspaceGraphCapability = components["schemas"]["WorkspaceGraphCapability"];

export async function getWorkspaceGraphCapability(
  workspaceId: string,
  init: Pick<RequestInit, "signal"> = {},
): Promise<WorkspaceGraphCapability> {
  const response = await apiRequest<{ data: WorkspaceGraphCapability }>(
    `/api/v1/workspaces/${workspaceId}/graph/capability`,
    { signal: init.signal },
  );
  return response.data;
}

export type WorkspaceGraphLens =
  | "combined"
  | "links"
  | "evidence"
  | "directory"
  | "compare"
  | "growth"
  | "provenance";

export type WorkspaceGraphAvailableLens = Exclude<
  WorkspaceGraphLens,
  "growth" | "provenance"
>;
export type WorkspaceGraphDocumentKind = "context" | "library";
export type WorkspaceGraphQuickFilter =
  | "orphans"
  | "highly_referenced"
  | "cross_folder"
  | "recently_connected";
export type WorkspaceGraphFacetMode = "include" | "exclude" | "only";

/** Rich client-side grammar. It is translated to the narrower v1 wire shape on Apply. */
export type WorkspaceGraphFacetRule = {
  facet: "document_kind" | "folder";
  mode: WorkspaceGraphFacetMode;
  value: string;
};

export type WorkspaceGraphDataFilters = {
  search: string;
  document_kinds: WorkspaceGraphDocumentKind[];
  time_basis: "document_updated" | "relation_observed";
  time_from: string | null;
  time_to: string | null;
  quick_filters: WorkspaceGraphQuickFilter[];
  facet_rules: WorkspaceGraphFacetRule[];
};

export type WorkspaceGraphOverviewRequest = {
  schema_version: "workspace-graph-overview.v1";
  head_mode: "published";
  lens: WorkspaceGraphLens;
  document_kinds: WorkspaceGraphDocumentKind[];
  search?: string;
  quick_filters: WorkspaceGraphQuickFilter[];
  folders: {
    include: string[];
    exclude: string[];
    only: string[];
  };
  time: {
    basis: "document_updated" | "relation_observed";
    from?: string;
    to?: string;
  };
  budget: {
    documents: number;
    relations: number;
  };
  expansion?: {
    cluster_id: string;
    cursor: string | null;
    pinned?: boolean;
  };
  /** Earlier expansions replayed before `expansion`; pinned ones fold last. */
  retained_expansions?: WorkspaceGraphRetainedExpansion[];
  focus_entity_id?: string;
};

export type WorkspaceGraphRetainedExpansion = { cluster_id: string; pinned: boolean; offset?: number };

export type WorkspaceGraphExpansionOutcome = {
  cluster_id: string;
  label: string | null;
  member_count: number;
  offset: number;
  revealed_count: number;
  visible_count: number;
  folded_count: number;
  pinned: boolean;
  status: "applied" | "partial" | "folded" | "missing" | "no_capacity";
};

export type WorkspaceGraphFocus =
  | { status: "document" | "cluster"; node_id: string }
  | { status: "unavailable"; node_id: null };

export type WorkspaceGraphDocumentNode = {
  grain: "document";
  id: string;
  entity_kind: WorkspaceGraphDocumentKind;
  entity_id: string;
  label: string;
  path: string;
  primary_folder_id: string | null;
  primary_folder_label: string;
  reference_count: number;
  evidence_count: number;
  also_appears_in: Array<{
    tree_node_id: string;
    label: string;
    path: string;
  }>;
};

export type WorkspaceGraphClusterNode = {
  grain: "cluster";
  id: string;
  cluster_kind: "folder_cluster" | "link_cluster" | "other_cluster";
  label: string;
  member_count: number;
  relation_count: number;
};

export type WorkspaceGraphNode =
  | WorkspaceGraphDocumentNode
  | WorkspaceGraphClusterNode;

export type WorkspaceGraphEdge = {
  id: string;
  source: string;
  target: string;
  relation_kind: string;
  label: string | null;
  layer: "knowledge" | "evidence" | "structure" | "provenance";
  direction: "directed" | "undirected";
  aggregate: boolean;
  relation_count: number;
  /** Repeated occurrences are layout weight, not the distinct relation count. */
  occurrence_weight?: number | null;
  cross_folder: boolean;
};

export type WorkspaceGraphFolder = {
  id: string;
  label: string;
  parent_folder_id: string | null;
  direct_member_count: number;
};

export type WorkspaceGraphFolderMembership = {
  node_id: string;
  folder_id: string | null;
  entity_kind: WorkspaceGraphDocumentKind;
  member_count: number;
};

export type WorkspaceGraphFolderEdgeSlice = {
  edge_id: string;
  source_folder_id: string | null;
  source_entity_kind: WorkspaceGraphDocumentKind;
  target_folder_id: string | null;
  target_entity_kind: WorkspaceGraphDocumentKind;
  relation_count: number;
  occurrence_weight: number;
};

/** Only visible primary placements and their visible ancestor closure. */
export type WorkspaceGraphFolderTopology = {
  schema_version: "workspace-graph-folders.v1";
  folders: WorkspaceGraphFolder[];
  memberships: WorkspaceGraphFolderMembership[];
  edge_slices: WorkspaceGraphFolderEdgeSlice[];
};

export const WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT = 200_000;

export type WorkspaceGraphFacetOption = {
  value: string;
  label: string;
  count: number;
};

export type WorkspaceGraphCounts = {
  candidate_documents: number;
  candidate_relations: number;
  rendered_documents: number;
  rendered_clusters: number;
  rendered_relations: number;
};

export type WorkspaceGraphScene = {
  schema_version: "workspace-graph-scene.v1";
  workspace_scope_considered: true;
  head_mode: "published";
  graph_revision: string;
  generated_at: string;
  algorithm_version: string;
  metric_basis: string;
  representation: "exact" | "aggregated" | "grouped";
  completeness: "complete" | "capped" | "partial" | "privacy_suppressed";
  truncated: boolean;
  counts: WorkspaceGraphCounts;
  omission_reasons: Array<
    | "document_budget"
    | "relation_budget"
    | "projection_partial"
    | "privacy_policy"
    | "renderer_fallback"
  >;
  nodes: WorkspaceGraphNode[];
  edges: WorkspaceGraphEdge[];
  facet_counts: {
    document_kinds: WorkspaceGraphFacetOption[];
    relation_kinds: WorkspaceGraphFacetOption[];
    folders: WorkspaceGraphFacetOption[];
    quick_filters: WorkspaceGraphFacetOption[];
  };
  expansion_cursor: string | null;
  expansions?: WorkspaceGraphExpansionOutcome[];
  warnings: string[];
  capabilities: {
    expand: boolean;
    evidence: boolean;
    export: boolean;
    directory: boolean;
    compare: boolean;
    growth: boolean;
    provenance: boolean;
  };
  focus?: WorkspaceGraphFocus | null;
  folder_topology?: WorkspaceGraphFolderTopology | null;
};

export async function queryWorkspaceGraph(
  workspaceId: string,
  request: WorkspaceGraphOverviewRequest,
  init: Pick<RequestInit, "signal"> = {},
) {
  const response = await apiRequest<unknown>(
    `/api/v1/workspaces/${workspaceId}/graph/overview/query`,
    {
      method: "POST",
      body: JSON.stringify(request),
      signal: init.signal,
    },
  );
  if (!isWorkspaceGraphResponse(response)) {
    throw new Error("Workspace graph returned a malformed or unsupported scene contract.");
  }
  return response.data;
}

export function isWorkspaceGraphResponse(value: unknown): value is { data: WorkspaceGraphScene } {
  if (!isRecord(value) || !isRecord(value.data)) return false;
  const scene = value.data;
  return scene.schema_version === "workspace-graph-scene.v1"
    && scene.workspace_scope_considered === true
    && scene.head_mode === "published"
    && typeof scene.graph_revision === "string"
    && typeof scene.generated_at === "string"
    && typeof scene.algorithm_version === "string"
    && typeof scene.metric_basis === "string"
    && ["exact", "aggregated", "grouped"].includes(String(scene.representation))
    && ["complete", "capped", "partial", "privacy_suppressed"].includes(
      String(scene.completeness),
    )
    && typeof scene.truncated === "boolean"
    && isGraphCounts(scene.counts)
    && Array.isArray(scene.omission_reasons)
    && scene.omission_reasons.every((reason) => [
      "document_budget",
      "relation_budget",
      "projection_partial",
      "privacy_policy",
      "renderer_fallback",
    ].includes(String(reason)))
    && Array.isArray(scene.nodes)
    && scene.nodes.every(isGraphNode)
    && Array.isArray(scene.edges)
    && scene.edges.every(isGraphEdge)
    && isFacetCounts(scene.facet_counts)
    && (scene.expansion_cursor === null || typeof scene.expansion_cursor === "string")
    && (scene.expansions === undefined
      || (Array.isArray(scene.expansions) && scene.expansions.every(isExpansionOutcome)))
    && Array.isArray(scene.warnings)
    && scene.warnings.every((warning) => typeof warning === "string")
    && isCapabilities(scene.capabilities)
    && (scene.focus === undefined || scene.focus === null || isFocus(scene.focus))
    && (scene.folder_topology === undefined || scene.folder_topology === null
      || isWorkspaceGraphFolderTopology(
        scene.folder_topology, scene.nodes as WorkspaceGraphNode[],
        scene.edges as WorkspaceGraphEdge[],
      ));
}

function isExpansionOutcome(value: unknown): value is WorkspaceGraphExpansionOutcome {
  return isRecord(value)
    && typeof value.cluster_id === "string"
    && (value.label === null || typeof value.label === "string")
    && ["member_count", "offset", "revealed_count", "visible_count", "folded_count"].every(
      (key) => isNonnegativeCount(value[key]),
    )
    && typeof value.pinned === "boolean"
    && typeof value.status === "string"
    && ["applied", "partial", "folded", "missing", "no_capacity"].includes(value.status);
}

function isFocus(value: unknown): value is WorkspaceGraphFocus {
  if (!isRecord(value)) return false;
  if (value.status === "unavailable") return value.node_id === null;
  return (value.status === "document" || value.status === "cluster")
    && typeof value.node_id === "string";
}

function isGraphCounts(value: unknown): value is WorkspaceGraphCounts {
  return isRecord(value)
    && [
      "candidate_documents",
      "candidate_relations",
      "rendered_documents",
      "rendered_clusters",
      "rendered_relations",
    ].every((key) => isNonnegativeCount(value[key]));
}

function isGraphNode(value: unknown): value is WorkspaceGraphNode {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.label !== "string") {
    return false;
  }
  if (value.grain === "cluster") {
    return ["folder_cluster", "link_cluster", "other_cluster"].includes(
      String(value.cluster_kind),
    )
      && isPositiveCount(value.member_count)
      && isNonnegativeCount(value.relation_count);
  }
  return value.grain === "document"
    && (value.entity_kind === "context" || value.entity_kind === "library")
    && typeof value.entity_id === "string"
    && typeof value.path === "string"
    && (value.primary_folder_id === null || typeof value.primary_folder_id === "string")
    && typeof value.primary_folder_label === "string"
    && isNonnegativeCount(value.reference_count)
    && isNonnegativeCount(value.evidence_count)
    && Array.isArray(value.also_appears_in)
    && value.also_appears_in.every(isPlacementRef);
}

function isPlacementRef(value: unknown) {
  return isRecord(value)
    && typeof value.tree_node_id === "string"
    && typeof value.label === "string"
    && typeof value.path === "string";
}

function isGraphEdge(value: unknown): value is WorkspaceGraphEdge {
  return isRecord(value)
    && ["id", "source", "target", "relation_kind", "layer", "direction"]
      .every((key) => typeof value[key] === "string")
    && (value.label === null || typeof value.label === "string")
    && ["knowledge", "evidence", "structure", "provenance"].includes(
      String(value.layer),
    )
    && (value.direction === "directed" || value.direction === "undirected")
    && typeof value.aggregate === "boolean"
    && isPositiveCount(value.relation_count)
    && (value.occurrence_weight === undefined || value.occurrence_weight === null
      || isPositiveCount(value.occurrence_weight))
    && typeof value.cross_folder === "boolean";
}

function isNonnegativeCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isPositiveCount(value: unknown): value is number {
  return isNonnegativeCount(value) && value > 0;
}

function isFolderId(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && value.length > 0);
}

function isDocumentKind(value: unknown): value is WorkspaceGraphDocumentKind {
  return value === "context" || value === "library";
}

function membershipKey(nodeId: string, folderId: string | null, kind: string): string {
  return JSON.stringify([nodeId, folderId, kind]);
}

function hasOnlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}

/** Validate correlated rows before any renderer can infer folder membership. */
export function isWorkspaceGraphFolderTopology(
  value: unknown,
  nodes: readonly WorkspaceGraphNode[],
  edges: readonly WorkspaceGraphEdge[],
): value is WorkspaceGraphFolderTopology {
  if (!isRecord(value) || value.schema_version !== "workspace-graph-folders.v1"
    || !hasOnlyKeys(value, ["schema_version", "folders", "memberships", "edge_slices"])
    || !Array.isArray(value.folders) || !Array.isArray(value.memberships)
    || !Array.isArray(value.edge_slices)
    || value.folders.length + value.memberships.length + value.edge_slices.length
      > WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT) return false;

  const folders = new Map<string, WorkspaceGraphFolder>();
  for (const folder of value.folders) {
    if (!isRecord(folder) || typeof folder.id !== "string" || !folder.id
      || !hasOnlyKeys(folder, ["id", "label", "parent_folder_id", "direct_member_count"])
      || folders.has(folder.id) || typeof folder.label !== "string"
      || folder.label.length === 0 || Array.from(folder.label).length > 200
      || !isFolderId(folder.parent_folder_id)
      || !isNonnegativeCount(folder.direct_member_count)) return false;
    folders.set(folder.id, folder as WorkspaceGraphFolder);
  }
  // A three-state walk is linear even for a deeply nested forest.
  const visited = new Set<string>();
  for (const folder of folders.values()) {
    const path = new Set<string>();
    let current: string | null = folder.id;
    while (current !== null && !visited.has(current)) {
      if (path.has(current)) return false;
      const ancestor = folders.get(current);
      if (!ancestor) return false;
      path.add(current);
      current = ancestor.parent_folder_id;
    }
    for (const id of path) visited.add(id);
  }

  const nodeById = new Map<string, WorkspaceGraphNode>();
  const entityKeys = new Set<string>();
  for (const node of nodes) {
    if (!isGraphNode(node) || !node.id || nodeById.has(node.id)) return false;
    if (node.grain === "document") {
      const key = JSON.stringify([node.entity_kind, node.entity_id]);
      if (!node.entity_id || entityKeys.has(key)
        || !isNonnegativeCount(node.reference_count)
        || !isNonnegativeCount(node.evidence_count)) return false;
      entityKeys.add(key);
    } else if (!isPositiveCount(node.member_count)
      || !isNonnegativeCount(node.relation_count)) return false;
    nodeById.set(node.id, node);
  }

  const memberships = new Set<string>();
  const membersByNode = new Map<string, number>();
  const rowsByNode = new Map<string, number>();
  const membersByFolder = new Map<string, number>();
  for (const row of value.memberships) {
    if (!isRecord(row) || typeof row.node_id !== "string"
      || !hasOnlyKeys(row, ["node_id", "folder_id", "entity_kind", "member_count"])
      || !isFolderId(row.folder_id) || !isDocumentKind(row.entity_kind)
      || !isPositiveCount(row.member_count)) return false;
    const node = nodeById.get(row.node_id);
    if (!node || (row.folder_id !== null && !folders.has(row.folder_id))) return false;
    const key = membershipKey(row.node_id, row.folder_id, row.entity_kind);
    if (memberships.has(key)) return false;
    memberships.add(key);
    if (node.grain === "document" && (row.entity_kind !== node.entity_kind
      || row.folder_id !== node.primary_folder_id || row.member_count !== 1)) return false;
    const count = (membersByNode.get(row.node_id) ?? 0) + row.member_count;
    if (!isPositiveCount(count)) return false;
    membersByNode.set(row.node_id, count);
    rowsByNode.set(row.node_id, (rowsByNode.get(row.node_id) ?? 0) + 1);
    if (row.folder_id !== null) {
      const folderCount = (membersByFolder.get(row.folder_id) ?? 0) + row.member_count;
      if (!isPositiveCount(folderCount)) return false;
      membersByFolder.set(row.folder_id, folderCount);
    }
  }
  for (const node of nodes) {
    if (membersByNode.get(node.id) !== (node.grain === "document" ? 1 : node.member_count)
      || (node.grain === "document" && rowsByNode.get(node.id) !== 1)) return false;
  }
  // Empty ancestor rows are allowed; unrelated folders (including secondary
  // placements) must not become islands simply because a facet names them.
  const requiredFolders = new Set<string>();
  for (const id of membersByFolder.keys()) {
    let current: string | null = id;
    while (current !== null && !requiredFolders.has(current)) {
      requiredFolders.add(current);
      current = folders.get(current)!.parent_folder_id;
    }
  }
  for (const folder of folders.values()) {
    if (!requiredFolders.has(folder.id)
      || folder.direct_member_count !== (membersByFolder.get(folder.id) ?? 0)) return false;
  }

  const edgeById = new Map<string, WorkspaceGraphEdge>();
  for (const edge of edges) {
    if (!isGraphEdge(edge) || !edge.id || edgeById.has(edge.id)
      || !isPositiveCount(edge.relation_count)
      || edge.occurrence_weight === undefined || edge.occurrence_weight === null
      || !nodeById.has(edge.source) || !nodeById.has(edge.target)) return false;
    edgeById.set(edge.id, edge);
  }
  const sliceKeys = new Set<string>();
  const countsByEdge = new Map<string, number>();
  const weightsByEdge = new Map<string, number>();
  for (const row of value.edge_slices) {
    if (!isRecord(row) || typeof row.edge_id !== "string"
      || !hasOnlyKeys(row, ["edge_id", "source_folder_id", "source_entity_kind",
        "target_folder_id", "target_entity_kind", "relation_count", "occurrence_weight"])
      || !isFolderId(row.source_folder_id) || !isFolderId(row.target_folder_id)
      || !isDocumentKind(row.source_entity_kind) || !isDocumentKind(row.target_entity_kind)
      || !isPositiveCount(row.relation_count) || !isPositiveCount(row.occurrence_weight)) {
      return false;
    }
    const edge = edgeById.get(row.edge_id);
    if (!edge
      || !memberships.has(membershipKey(edge.source, row.source_folder_id, row.source_entity_kind))
      || !memberships.has(membershipKey(edge.target, row.target_folder_id, row.target_entity_kind))) {
      return false;
    }
    const key = JSON.stringify([row.edge_id, row.source_folder_id, row.source_entity_kind,
      row.target_folder_id, row.target_entity_kind]);
    if (sliceKeys.has(key)) return false;
    sliceKeys.add(key);
    const count = (countsByEdge.get(row.edge_id) ?? 0) + row.relation_count;
    const weight = (weightsByEdge.get(row.edge_id) ?? 0) + row.occurrence_weight;
    if (!isPositiveCount(count) || !isPositiveCount(weight)) return false;
    countsByEdge.set(row.edge_id, count);
    weightsByEdge.set(row.edge_id, weight);
  }
  return edges.every((edge) => countsByEdge.get(edge.id) === edge.relation_count
    && weightsByEdge.get(edge.id) === edge.occurrence_weight);
}

function isFacetCounts(value: unknown): value is WorkspaceGraphScene["facet_counts"] {
  return isRecord(value)
    && ["document_kinds", "relation_kinds", "folders", "quick_filters"].every(
      (key) => Array.isArray(value[key]) && value[key].every(isFacetOption),
    );
}

function isFacetOption(value: unknown): value is WorkspaceGraphFacetOption {
  return isRecord(value)
    && typeof value.value === "string"
    && typeof value.label === "string"
    && typeof value.count === "number";
}

function isCapabilities(value: unknown): value is WorkspaceGraphScene["capabilities"] {
  return isRecord(value)
    && ["expand", "evidence", "export", "directory", "compare", "growth", "provenance"].every(
      (key) => typeof value[key] === "boolean",
    );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

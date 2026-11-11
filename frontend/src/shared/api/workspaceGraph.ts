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
export type WorkspaceGraphDocumentKind = "context" | "library" | "agent";
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

/** A TreeRail folder. What it holds is its outgoing `tree` relations. */
export type WorkspaceGraphFolderNode = {
  grain: "folder";
  id: string;
  folder_id: string;
  label: string;
  path: string;
};

export type WorkspaceGraphNode =
  | WorkspaceGraphDocumentNode
  | WorkspaceGraphClusterNode
  | WorkspaceGraphFolderNode;

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
  /** On `tree` containment: the target's one placement that starts the layout. */
  primary?: boolean;
};

/** TreeRail containment. Budgets and the unlinked-document rule do not count it. */
export const WORKSPACE_GRAPH_CONTAINMENT_KIND = "tree";

export function isWorkspaceGraphContainment(edge: Pick<WorkspaceGraphEdge, "relation_kind">): boolean {
  return edge.relation_kind === WORKSPACE_GRAPH_CONTAINMENT_KIND;
}

/** Documents a node stands for: one, a cluster's members, or none for a folder. */
export function workspaceGraphNodeDocuments(node: WorkspaceGraphNode): number {
  return node.grain === "document" ? 1 : node.grain === "cluster" ? node.member_count : 0;
}

/** What the 2K/10K budgets count: documents, clusters and relations, never folders or containment. */
export function workspaceGraphAdmissionCounts(scene: Pick<WorkspaceGraphScene, "nodes" | "edges">): {
  nodeCount: number;
  edgeCount: number;
} {
  return {
    nodeCount: scene.nodes.filter((node) => node.grain !== "folder").length,
    edgeCount: scene.edges.filter((edge) => !isWorkspaceGraphContainment(edge)).length,
  };
}

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
    && (scene.focus === undefined || scene.focus === null || isFocus(scene.focus));
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
  if (value.grain === "folder") {
    return typeof value.folder_id === "string" && value.folder_id.length > 0
      && typeof value.path === "string";
  }
  return value.grain === "document"
    && (value.entity_kind === "context" || value.entity_kind === "library" || value.entity_kind === "agent")
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
    && typeof value.cross_folder === "boolean"
    && (value.primary === undefined || typeof value.primary === "boolean");
}

function isNonnegativeCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isPositiveCount(value: unknown): value is number {
  return isNonnegativeCount(value) && value > 0;
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

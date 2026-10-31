import type { WorkspaceGraphEdge, WorkspaceGraphNode } from "../../../../shared/api/workspaceGraph";
import { buildWorkspaceMapColumns, type WorkspaceMapColumns } from "../../map/sceneColumns";

/** The 20K corpus shape: one dominant link component, an unlinked residue, a small one, and a pair. */
export const AGGREGATED_CLUSTER_MEMBERS = [17_499, 492, 12, 2] as const;

export const aggregatedClusterId = (index: number) => `cluster-${index}`;

/**
 * An aggregated Workspace Map scene made the way the server makes one, reaching the renderer only through
 * the product's own `buildWorkspaceMapColumns`: link clusters of very different sizes next to ordinary
 * documents, which form one chain and link once into the first cluster. Clusters come first, so the node
 * count is `documents + clusters.length`.
 */
export function aggregatedWorkspaceColumns(
  { documents, clusters }: { documents: number; clusters: readonly number[] },
): WorkspaceMapColumns {
  const documentId = (index: number) => `doc-${String(index).padStart(5, "0")}`;
  const nodes: WorkspaceGraphNode[] = [
    ...clusters.map((members, index): WorkspaceGraphNode => ({
      grain: "cluster", id: aggregatedClusterId(index), cluster_kind: index === 1 ? "other_cluster" : "link_cluster",
      label: `Cluster ${index} · ${members}`, member_count: members, relation_count: 0,
    })),
    ...Array.from({ length: documents }, (_, index): WorkspaceGraphNode => ({
      grain: "document", id: documentId(index), entity_kind: index % 9 === 0 ? "library" : "context",
      entity_id: `page-${index}`, label: `Document ${index}`, path: `/pages/${index}`,
      primary_folder_id: null, primary_folder_label: "None", reference_count: 1, evidence_count: 0, also_appears_in: [],
    })),
  ];
  const link = (id: string, source: string, target: string, aggregate: boolean): WorkspaceGraphEdge => ({
    id, source, target, relation_kind: "wikilink", label: "links to", layer: "knowledge",
    direction: "directed", aggregate, relation_count: 1, cross_folder: false,
  });
  const edges = [
    ...Array.from({ length: documents - 1 }, (_, index) => link(`chain-${index}`, documentId(index), documentId(index + 1), false)),
    link("to-hub", documentId(0), aggregatedClusterId(0), true),
  ];
  const grouped = clusters.reduce((sum, members) => sum + members, 0);
  return buildWorkspaceMapColumns({
    schema_version: "workspace-graph-scene.v1", workspace_scope_considered: true, head_mode: "published",
    graph_revision: "aggregated-fixture-0000000000000001", generated_at: "2026-10-18T00:00:00+00:00",
    algorithm_version: "aggregated-fixture.v1", metric_basis: "visible_published_links_and_citations.pre_quick.v1",
    representation: "aggregated", completeness: "capped", truncated: true,
    counts: { candidate_documents: documents + grouped, candidate_relations: edges.length,
      rendered_documents: documents, rendered_clusters: clusters.length, rendered_relations: edges.length },
    omission_reasons: ["document_budget"], warnings: [], expansion_cursor: "cursor", nodes, edges,
    facet_counts: { document_kinds: [], relation_kinds: [], folders: [], quick_filters: [] },
    capabilities: { expand: true, evidence: false, export: true, directory: true, compare: false, growth: false, provenance: false },
  }, { workspaceId: "aggregated-workspace", actorId: "aggregated-actor", selectionKey: "combined" });
}

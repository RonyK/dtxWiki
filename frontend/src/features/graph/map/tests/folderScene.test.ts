import { describe, expect, it } from "vitest";

import type { WorkspaceGraphScene } from "../../../../shared/api/workspaceGraph";
import { prepareWorkspaceMapFolderScene, type WorkspaceMapPreparedFolderScene } from "../folderScene";

function folderScene(): WorkspaceGraphScene {
  return {
    schema_version: "workspace-graph-scene.v1", workspace_scope_considered: true,
    head_mode: "published", graph_revision: "revision", generated_at: "2026-10-04",
    algorithm_version: "server-algorithm", metric_basis: "visible", representation: "aggregated",
    completeness: "complete", truncated: false, omission_reasons: [],
    counts: { candidate_documents: 6, candidate_relations: 10, rendered_documents: 2,
      rendered_clusters: 1, rendered_relations: 3 },
    nodes: [
      { grain: "document", id: "document-a", entity_kind: "context", entity_id: "page-a",
        label: "Alpha", path: "/alpha", primary_folder_id: "child", primary_folder_label: "Child",
        reference_count: 3, evidence_count: 1,
        also_appears_in: [{ tree_node_id: "secondary", label: "Secondary", path: "/secondary" }] },
      { grain: "document", id: "document-l", entity_kind: "library", entity_id: "item-l",
        label: "Library", path: "/library", primary_folder_id: null, primary_folder_label: "Unplaced",
        reference_count: 2, evidence_count: 0, also_appears_in: [] },
      { grain: "cluster", id: "cluster:mixed", cluster_kind: "link_cluster", label: "Mixed",
        member_count: 4, relation_count: 6 },
    ],
    edges: [
      { id: "exact", source: "document-a", target: "document-l", relation_kind: "wikilink",
        label: null, layer: "knowledge", direction: "directed", aggregate: false,
        relation_count: 2, occurrence_weight: 5, cross_folder: true },
      { id: "self", source: "cluster:mixed", target: "cluster:mixed", relation_kind: "wikilink",
        label: null, layer: "knowledge", direction: "directed", aggregate: true,
        relation_count: 6, occurrence_weight: 11, cross_folder: true },
      { id: "mixed", source: "document-a", target: "cluster:mixed", relation_kind: "citation_page",
        label: null, layer: "evidence", direction: "directed", aggregate: true,
        relation_count: 2, occurrence_weight: 4, cross_folder: true },
    ],
    facet_counts: { document_kinds: [], relation_kinds: [], folders: [], quick_filters: [] },
    expansion_cursor: null, warnings: [], capabilities: { expand: true, evidence: true,
      export: true, directory: true, compare: true, growth: false, provenance: false },
    folder_topology: {
      schema_version: "workspace-graph-folders.v1",
      folders: [
        { id: "root", label: "Root", parent_folder_id: null, direct_member_count: 1 },
        { id: "child", label: "Child", parent_folder_id: "root", direct_member_count: 3 },
      ],
      memberships: [
        { node_id: "document-a", folder_id: "child", entity_kind: "context", member_count: 1 },
        { node_id: "document-l", folder_id: null, entity_kind: "library", member_count: 1 },
        { node_id: "cluster:mixed", folder_id: "root", entity_kind: "context", member_count: 1 },
        { node_id: "cluster:mixed", folder_id: "child", entity_kind: "library", member_count: 2 },
        { node_id: "cluster:mixed", folder_id: null, entity_kind: "context", member_count: 1 },
      ],
      edge_slices: [
        { edge_id: "exact", source_folder_id: "child", source_entity_kind: "context",
          target_folder_id: null, target_entity_kind: "library", relation_count: 2, occurrence_weight: 5 },
        { edge_id: "self", source_folder_id: "root", source_entity_kind: "context",
          target_folder_id: "child", target_entity_kind: "library", relation_count: 2, occurrence_weight: 3 },
        { edge_id: "self", source_folder_id: "child", source_entity_kind: "library",
          target_folder_id: "child", target_entity_kind: "library", relation_count: 3, occurrence_weight: 6 },
        { edge_id: "self", source_folder_id: null, source_entity_kind: "context",
          target_folder_id: "root", target_entity_kind: "context", relation_count: 1, occurrence_weight: 2 },
        { edge_id: "mixed", source_folder_id: "child", source_entity_kind: "context",
          target_folder_id: "child", target_entity_kind: "library", relation_count: 1, occurrence_weight: 1 },
        { edge_id: "mixed", source_folder_id: "child", source_entity_kind: "context",
          target_folder_id: "root", target_entity_kind: "context", relation_count: 1, occurrence_weight: 3 },
      ],
    },
  };
}

function prepared(scene = folderScene()): WorkspaceMapPreparedFolderScene {
  const result = prepareWorkspaceMapFolderScene(scene);
  if (!result.supported) throw new Error(`Unexpected unsupported fixture: ${result.reason}`);
  return result;
}

describe("Workspace Map primary-folder scene preparation", () => {
  it("preserves document identities, separates mixed folder/kind fragments, and conserves counts", () => {
    const result = prepared();
    expect(result.points.find((point) => point.id === "document-a")).toMatchObject({
      entityKey: "context:page-a", grain: "document", entityKind: "context", folderId: "child",
      memberCount: 1, referenceCount: 3, evidenceCount: 1,
    });
    expect(result.sourceNodeFragments["document-a"]).toEqual(["document-a"]);
    const fragments = result.points.filter((point) => point.grain === "fragment");
    expect(fragments).toHaveLength(3);
    expect(fragments.every((point) => point.referenceCount === null && point.evidenceCount === null)).toBe(true);
    expect(result.sourceNodeFragments["cluster:mixed"]).toHaveLength(3);
    expect(result.totals).toEqual({ memberCount: 6, relationCount: 10, occurrenceWeight: 20,
      loopRelationCount: 3, loopOccurrenceWeight: 6 });
    expect(result.requiredPoints).toBe(7);
    expect(result.requiredRelations).toBe(7);
  });

  it("expands aggregate self-edges before excluding only same-fragment physics loops", () => {
    const result = prepared();
    const self = result.relations.filter((relation) => relation.sourceEdgeId === "self");
    expect(self).toHaveLength(3);
    expect(self.filter((relation) => relation.source !== relation.target)).toHaveLength(2);
    expect(result.physicsRelations).toHaveLength(5);
    expect(result.physicsRelations.every((relation) => relation.source !== relation.target)).toBe(true);
    expect(result.folderBundles).toEqual([
      { sourceFolderId: null, targetFolderId: "child", relationCount: 2, occurrenceWeight: 5 },
      { sourceFolderId: null, targetFolderId: "root", relationCount: 1, occurrenceWeight: 2 },
      { sourceFolderId: "child", targetFolderId: "root", relationCount: 3, occurrenceWeight: 6 },
    ]);
  });

  it("builds the visible forest and unplaced partition without secondary/facet islands", () => {
    const scene = folderScene();
    scene.facet_counts.folders = [{ value: "hidden", label: "Hidden", count: 99 }];
    const result = prepared(scene);
    expect(result.folders.map((folder) => folder.id)).toEqual(["child", "root"]);
    expect(result.folders.find((folder) => folder.id === "root")!.childFolderIds).toEqual(["child"]);
    expect(result.folders.find((folder) => folder.id === "child")!.pointIds).toContain("document-a");
    expect(result.unplacedPointIds).toHaveLength(2);
    expect(result.unplacedPointIds).toContain("document-l");
    expect(result.folders.some((folder) => folder.id === "secondary" || folder.id === "hidden")).toBe(false);
  });

  it("does not invent a non-visible ancestor or heal malformed rows", () => {
    const scene = folderScene();
    scene.folder_topology!.folders[0].parent_folder_id = "hidden";
    expect(prepareWorkspaceMapFolderScene(scene)).toEqual({ supported: false, reason: "invalid_topology" });
  });

  it("accepts a wholly unplaced scene without creating a synthetic folder", () => {
    const scene = folderScene();
    scene.nodes = [scene.nodes[1]];
    scene.edges = [];
    scene.counts = { candidate_documents: 1, candidate_relations: 0, rendered_documents: 1,
      rendered_clusters: 0, rendered_relations: 0 };
    scene.folder_topology = { schema_version: "workspace-graph-folders.v1", folders: [],
      memberships: [{ node_id: "document-l", folder_id: null, entity_kind: "library", member_count: 1 }],
      edge_slices: [] };
    const result = prepared(scene);
    expect(result.folders).toEqual([]);
    expect(result.unplacedPointIds).toEqual(["document-l"]);
    expect(result.folderBundles).toEqual([]);
    expect(result.totals).toEqual({ memberCount: 1, relationCount: 0, occurrenceWeight: 0,
      loopRelationCount: 0, loopOccurrenceWeight: 0 });
  });

  it("does not carry server-provided coordinates into folder preparation", () => {
    const scene = folderScene();
    Object.assign(scene.folder_topology!.folders[0], { x: 1, y: 2 });
    expect(prepareWorkspaceMapFolderScene(scene)).toEqual({ supported: false, reason: "invalid_topology" });
  });

  it("is deterministic under all authoritative row reorderings and leaves input untouched", () => {
    const scene = folderScene();
    const original = structuredClone(scene);
    const before = prepared(scene);
    expect(scene).toEqual(original);
    scene.nodes.reverse();
    scene.edges.reverse();
    scene.folder_topology!.folders.reverse();
    scene.folder_topology!.memberships.reverse();
    scene.folder_topology!.edge_slices.reverse();
    expect(prepared(scene)).toEqual(before);
  });

  it("ignores labels, response revisions, facets, timestamps and secondary placements in geometry", () => {
    const scene = folderScene();
    const before = prepared(scene);
    scene.graph_revision = "other-actor-change";
    scene.generated_at = "2030-01-01";
    scene.algorithm_version = "server-label-only-change";
    scene.metric_basis = "renamed explanation";
    scene.counts.candidate_documents = 99_999;
    scene.facet_counts.folders = [{ value: "never-visible", label: "Hidden", count: 20_000 }];
    scene.nodes = scene.nodes.map((node) => ({ ...node, label: `${node.label} renamed` }));
    for (const node of scene.nodes) {
      if (node.grain === "document") {
        node.path = "/renamed";
        node.also_appears_in = [{ tree_node_id: "other", label: "Other", path: "/other" }];
      }
    }
    for (const folder of scene.folder_topology!.folders) folder.label += " renamed";
    for (const edge of scene.edges) edge.label = "Renamed";
    expect(prepared(scene).geometryFingerprint).toBe(before.geometryFingerprint);
  });

  it("ignores replaceable document row and edge projection IDs in the fingerprint", () => {
    const scene = folderScene();
    const before = prepared(scene).geometryFingerprint;
    const replacements = new Map([["document-a", "new-a"], ["document-l", "new-l"]]);
    for (const node of scene.nodes) node.id = replacements.get(node.id) ?? node.id;
    for (const edge of scene.edges) {
      edge.source = replacements.get(edge.source) ?? edge.source;
      edge.target = replacements.get(edge.target) ?? edge.target;
      edge.id += "-new";
    }
    for (const membership of scene.folder_topology!.memberships) {
      membership.node_id = replacements.get(membership.node_id) ?? membership.node_id;
    }
    for (const slice of scene.folder_topology!.edge_slices) slice.edge_id += "-new";
    expect(prepared(scene).geometryFingerprint).toBe(before);
  });

  it("changes geometry identity when folder hierarchy or primary membership changes", () => {
    const scene = folderScene();
    const before = prepared(scene).geometryFingerprint;
    scene.folder_topology!.folders[1].parent_folder_id = null;
    expect(prepared(scene).geometryFingerprint).not.toBe(before);
    const primary = folderScene();
    const document = primary.nodes[0];
    if (document.grain !== "document") throw new Error("Expected document.");
    document.primary_folder_id = "root";
    primary.folder_topology!.memberships[0].folder_id = "root";
    primary.folder_topology!.folders[0].direct_member_count += 1;
    primary.folder_topology!.folders[1].direct_member_count -= 1;
    for (const slice of primary.folder_topology!.edge_slices) {
      if (slice.edge_id === "exact" || slice.edge_id === "mixed") slice.source_folder_id = "root";
    }
    expect(prepared(primary).geometryFingerprint).not.toBe(before);
  });

  it("changes occurrence weight independently of distinct relation totals", () => {
    const scene = folderScene();
    const before = prepared(scene);
    scene.edges[0].occurrence_weight! += 5;
    scene.folder_topology!.edge_slices[0].occurrence_weight += 5;
    const after = prepared(scene);
    expect(after.totals.relationCount).toBe(before.totals.relationCount);
    expect(after.totals.occurrenceWeight).toBe(25);
    expect(after.geometryFingerprint).not.toBe(before.geometryFingerprint);
  });

  it("declares legacy/topology row budget unavailability instead of fabricating membership", () => {
    const scene = folderScene();
    delete scene.folder_topology;
    expect(prepareWorkspaceMapFolderScene(scene)).toEqual({ supported: false, reason: "legacy_topology" });
    scene.folder_topology = null;
    scene.warnings = ["folder_topology_row_budget_exceeded"];
    expect(prepareWorkspaceMapFolderScene(scene)).toEqual({ supported: false, reason: "topology_budget" });
  });

  it("budgets structural folder points and hierarchy strokes explicitly without partial fake output", () => {
    expect(prepareWorkspaceMapFolderScene(folderScene(), { budgets: { points: 6 } })).toEqual({
      supported: false, reason: "point_budget", requiredPoints: 7, requiredRelations: 7, limit: 6,
    });
    expect(prepareWorkspaceMapFolderScene(folderScene(), { budgets: { relations: 6 } })).toEqual({
      supported: false, reason: "relation_budget", requiredPoints: 7, requiredRelations: 7, limit: 6,
    });
    expect(prepareWorkspaceMapFolderScene(folderScene(), { budgets: { points: 7, relations: 7 } }).supported).toBe(true);
    expect(() => prepareWorkspaceMapFolderScene(folderScene(), { budgets: { points: 1.5 } })).toThrow("safe integers");
  });

  it("safely maps malicious prototype-shaped source IDs as own record keys", () => {
    const scene = folderScene();
    scene.nodes[2].id = "__proto__";
    for (const edge of scene.edges) {
      if (edge.source === "cluster:mixed") edge.source = "__proto__";
      if (edge.target === "cluster:mixed") edge.target = "__proto__";
    }
    for (const membership of scene.folder_topology!.memberships) {
      if (membership.node_id === "cluster:mixed") membership.node_id = "__proto__";
    }
    const result = prepared(scene);
    expect(Object.hasOwn(result.sourceNodeFragments, "__proto__")).toBe(true);
    expect(result.sourceNodeFragments["__proto__"]).toHaveLength(3);
    expect(Object.getPrototypeOf(result.sourceNodeFragments)).toBe(Object.prototype);
  });
});

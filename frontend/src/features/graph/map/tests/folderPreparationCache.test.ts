import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceGraphScene } from "../../../../shared/api/workspaceGraph";
import * as folderScene from "../folderScene";
import { prepareWorkspaceMapFolderSceneOnce } from "../folderPreparationCache";

function scene(): WorkspaceGraphScene {
  return {
    schema_version: "workspace-graph-scene.v1", workspace_scope_considered: true,
    head_mode: "published", graph_revision: "revision", generated_at: "2026-10-04",
    algorithm_version: "server-algorithm", metric_basis: "visible", representation: "exact",
    completeness: "complete", truncated: false, omission_reasons: [],
    counts: { candidate_documents: 2, candidate_relations: 1, rendered_documents: 2,
      rendered_clusters: 0, rendered_relations: 1 },
    nodes: [
      { grain: "document", id: "document-a", entity_kind: "context", entity_id: "page-a",
        label: "Alpha", path: "/alpha", primary_folder_id: "folder", primary_folder_label: "Folder",
        reference_count: 1, evidence_count: 0, also_appears_in: [] },
      { grain: "document", id: "document-b", entity_kind: "context", entity_id: "page-b",
        label: "Beta", path: "/beta", primary_folder_id: "folder", primary_folder_label: "Folder",
        reference_count: 1, evidence_count: 0, also_appears_in: [] },
    ],
    edges: [{ id: "edge", source: "document-a", target: "document-b", relation_kind: "wikilink",
      label: null, layer: "knowledge", direction: "directed", aggregate: false,
      relation_count: 1, occurrence_weight: 2, cross_folder: false }],
    facet_counts: { document_kinds: [], relation_kinds: [], folders: [], quick_filters: [] },
    expansion_cursor: null, warnings: [], capabilities: { expand: false, evidence: false,
      export: true, directory: true, compare: false, growth: false, provenance: false },
    folder_topology: {
      schema_version: "workspace-graph-folders.v1",
      folders: [{ id: "folder", label: "Folder", parent_folder_id: null, direct_member_count: 2 }],
      memberships: [
        { node_id: "document-a", folder_id: "folder", entity_kind: "context", member_count: 1 },
        { node_id: "document-b", folder_id: "folder", entity_kind: "context", member_count: 1 },
      ],
      edge_slices: [{ edge_id: "edge", source_folder_id: "folder", source_entity_kind: "context",
        target_folder_id: "folder", target_entity_kind: "context", relation_count: 1, occurrence_weight: 2 }],
    },
  };
}

const BUDGETS = { points: 25_000, relations: 100_000 } as const;

describe("Workspace Map folder preparation cache", () => {
  afterEach(() => vi.restoreAllMocks());

  it("prepares a scene object once, another object again, and keeps an unsupported result for its scene", () => {
    const prepare = vi.spyOn(folderScene, "prepareWorkspaceMapFolderScene");
    const first = scene();
    const prepared = prepareWorkspaceMapFolderSceneOnce(first, { budgets: BUDGETS });
    expect(prepared.supported).toBe(true);
    expect(prepareWorkspaceMapFolderSceneOnce(first, { budgets: BUDGETS })).toBe(prepared);
    expect(prepare).toHaveBeenCalledTimes(1);

    // The key is the object: a scene with identical content is a new request's response.
    expect(prepareWorkspaceMapFolderSceneOnce(scene(), { budgets: BUDGETS })).not.toBe(prepared);
    expect(prepare).toHaveBeenCalledTimes(2);

    const legacy = scene();
    delete legacy.folder_topology;
    const unsupported = prepareWorkspaceMapFolderSceneOnce(legacy, { budgets: BUDGETS });
    expect(unsupported).toEqual({ supported: false, reason: "legacy_topology" });
    expect(prepareWorkspaceMapFolderSceneOnce(legacy, { budgets: BUDGETS })).toBe(unsupported);
    expect(prepare).toHaveBeenCalledTimes(3);
  });
});

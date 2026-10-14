import { describe, expect, it } from "vitest";

import { isWorkspaceGraphResponse, type WorkspaceGraphScene } from "../workspaceGraph";

const validResponse = {
  data: {
    schema_version: "workspace-graph-scene.v1",
    workspace_scope_considered: true,
    head_mode: "published",
    graph_revision: "revision-0000000000000001",
    generated_at: "2026-08-05T00:00:00+00:00",
    algorithm_version: "workspace-graph.v1",
    metric_basis: "Referenced by N visible documents",
    representation: "exact",
    completeness: "complete",
    truncated: false,
    counts: {
      candidate_documents: 1,
      candidate_relations: 0,
      rendered_documents: 1,
      rendered_clusters: 0,
      rendered_relations: 0,
    },
    omission_reasons: [],
    nodes: [{
      grain: "document",
      id: "context:one",
      entity_kind: "context",
      entity_id: "00000000-0000-4000-8000-000000000001",
      label: "Alpha",
      path: "/pages/alpha",
      primary_folder_id: null,
      primary_folder_label: "No visible placement",
      reference_count: 0,
      evidence_count: 0,
      also_appears_in: [],
    }],
    edges: [],
    facet_counts: {
      document_kinds: [],
      relation_kinds: [],
      folders: [],
      quick_filters: [],
    },
    expansion_cursor: null,
    warnings: [],
    capabilities: {
      expand: false,
      evidence: false,
      export: true,
      directory: true,
      compare: false,
      growth: false,
      provenance: false,
    },
  },
};

describe("Workspace Graph response guard", () => {
  it("accepts the frozen scene contract", () => {
    expect(isWorkspaceGraphResponse(validResponse)).toBe(true);
  });

  it.each([
    ["document", { status: "document", node_id: "context:one" }],
    ["cluster", { status: "cluster", node_id: "cluster:one" }],
    ["unavailable", { status: "unavailable", node_id: null }],
  ])("accepts a %s focus result", (_label, focus) => {
    expect(isWorkspaceGraphResponse({
      data: { ...validResponse.data, focus },
    })).toBe(true);
  });

  it.each([
    ["unsupported omission", { omission_reasons: ["hidden_total"] }],
    ["invalid cursor", { expansion_cursor: 42 }],
    ["missing capability", {
      capabilities: { ...validResponse.data.capabilities, export: undefined },
    }],
    ["edge without actor-visible cross-folder truth", {
      edges: [{
        id: "edge:one",
        source: "context:one",
        target: "context:one",
        relation_kind: "wikilink",
        label: null,
        layer: "knowledge",
        direction: "directed",
        aggregate: false,
        relation_count: 1,
      }],
    }],
    ["unavailable focus that names a node", {
      focus: { status: "unavailable", node_id: "context:one" },
    }],
    ["located focus without a node", { focus: { status: "document", node_id: null } }],
    ["unknown focus status", { focus: { status: "hidden", node_id: null } }],
    ["malformed secondary placement", {
      nodes: [{
        ...validResponse.data.nodes[0],
        also_appears_in: [null],
      }],
    }],
  ])("rejects a malformed payload: %s", (_label, replacement) => {
    expect(isWorkspaceGraphResponse({
      data: { ...validResponse.data, ...replacement },
    })).toBe(false);
  });
});

function folderResponse(): { data: WorkspaceGraphScene } {
  const response = structuredClone(validResponse) as { data: WorkspaceGraphScene };
  const document = response.data.nodes[0];
  if (document.grain !== "document") throw new Error("Expected fixture document.");
  document.primary_folder_id = "child";
  response.data.nodes.push({ grain: "cluster", id: "mixed", label: "Mixed",
    cluster_kind: "link_cluster", member_count: 2, relation_count: 2 });
  response.data.edges.push({ id: "loop", source: "mixed", target: "mixed",
    relation_kind: "wikilink", label: null, layer: "knowledge", direction: "directed",
    aggregate: true, relation_count: 2, occurrence_weight: 3, cross_folder: true });
  response.data.counts = { candidate_documents: 3, candidate_relations: 2,
    rendered_documents: 1, rendered_clusters: 1, rendered_relations: 1 };
  response.data.folder_topology = {
    schema_version: "workspace-graph-folders.v1",
    folders: [
      { id: "root", label: "Root", parent_folder_id: null, direct_member_count: 1 },
      { id: "child", label: "Child", parent_folder_id: "root", direct_member_count: 2 },
    ],
    memberships: [
      { node_id: document.id, folder_id: "child", entity_kind: "context", member_count: 1 },
      { node_id: "mixed", folder_id: "root", entity_kind: "context", member_count: 1 },
      { node_id: "mixed", folder_id: "child", entity_kind: "library", member_count: 1 },
    ],
    edge_slices: [
      { edge_id: "loop", source_folder_id: "root", source_entity_kind: "context",
        target_folder_id: "child", target_entity_kind: "library", relation_count: 1,
        occurrence_weight: 2 },
      { edge_id: "loop", source_folder_id: "child", source_entity_kind: "library",
        target_folder_id: "child", target_entity_kind: "library", relation_count: 1,
        occurrence_weight: 1 },
    ],
  };
  return response;
}

describe("Workspace Graph visible folder topology guard", () => {
  it("accepts correlated mixed-cluster loops and visible ancestor rows", () => {
    expect(isWorkspaceGraphResponse(folderResponse())).toBe(true);
    const ancestorOnly = folderResponse();
    const topology = ancestorOnly.data.folder_topology!;
    topology.folders.unshift({ id: "grandparent", label: "Ancestor", parent_folder_id: null,
      direct_member_count: 0 });
    topology.folders[1].parent_folder_id = "grandparent";
    expect(isWorkspaceGraphResponse(ancestorOnly)).toBe(true);
  });

  it("keeps absent/null topology and absent/null edge weight legacy-compatible", () => {
    for (const folder_topology of [undefined, null]) {
      const response = folderResponse();
      response.data.folder_topology = folder_topology;
      for (const occurrence_weight of [undefined, null, 3]) {
        response.data.edges[0].occurrence_weight = occurrence_weight;
        expect(isWorkspaceGraphResponse(response)).toBe(true);
      }
    }
  });

  it.each(["😀".repeat(101), `한${"😀".repeat(199)}`])(
    "accepts server-valid Unicode folder labels by code points, not UTF-16 units",
    (label) => {
      const response = folderResponse();
      response.data.folder_topology!.folders[0].label = label;
      expect(isWorkspaceGraphResponse(response)).toBe(true);
    },
  );

  it("rejects folder labels longer than the server's 200 code-point contract", () => {
    const response = folderResponse();
    response.data.folder_topology!.folders[0].label = "😀".repeat(201);
    expect(isWorkspaceGraphResponse(response)).toBe(false);
  });

  const invalid: Array<[string, (scene: WorkspaceGraphScene) => void]> = [
    ["unsupported version", (scene) => Object.assign(scene.folder_topology!, { schema_version: "future" })],
    ["server-computed coordinates", (scene) => Object.assign(scene.folder_topology!, { positions: [1, 2] })],
    ["folder coordinate", (scene) => Object.assign(scene.folder_topology!.folders[0], { x: 1 })],
    ["membership coordinate", (scene) => Object.assign(scene.folder_topology!.memberships[0], { y: 1 })],
    ["slice community hint", (scene) => Object.assign(scene.folder_topology!.edge_slices[0], { community: 1 })],
    ["duplicate folder", (scene) => scene.folder_topology!.folders.push(scene.folder_topology!.folders[0])],
    ["dangling ancestor", (scene) => { scene.folder_topology!.folders[0].parent_folder_id = "hidden"; }],
    ["ancestry cycle", (scene) => { scene.folder_topology!.folders[0].parent_folder_id = "child"; }],
    ["empty label", (scene) => { scene.folder_topology!.folders[0].label = ""; }],
    ["unrelated empty folder", (scene) => scene.folder_topology!.folders.push({ id: "secondary",
      label: "Secondary", parent_folder_id: null, direct_member_count: 0 })],
    ["negative folder count", (scene) => { scene.folder_topology!.folders[0].direct_member_count = -1; }],
    ["fractional member count", (scene) => { scene.folder_topology!.memberships[1].member_count = 0.5; }],
    ["unsafe member count", (scene) => { scene.folder_topology!.memberships[1].member_count = 2 ** 53; }],
    ["duplicate membership", (scene) => scene.folder_topology!.memberships.push(scene.folder_topology!.memberships[0])],
    ["missing membership node", (scene) => { scene.folder_topology!.memberships[0].node_id = "hidden"; }],
    ["missing primary folder", (scene) => { scene.folder_topology!.memberships[0].folder_id = "hidden"; }],
    ["non-primary membership", (scene) => { scene.folder_topology!.memberships[0].folder_id = "root"; }],
    ["wrong document kind", (scene) => { scene.folder_topology!.memberships[0].entity_kind = "library"; }],
    ["document represents two members", (scene) => { scene.folder_topology!.memberships[0].member_count = 2; }],
    ["missing node membership", (scene) => { scene.folder_topology!.memberships.shift(); }],
    ["cluster member total differs", (scene) => {
      const node = scene.nodes[1];
      if (node.grain === "cluster") node.member_count = 3;
    }],
    ["direct folder count differs", (scene) => { scene.folder_topology!.folders[0].direct_member_count = 2; }],
    ["duplicate edge slice", (scene) => scene.folder_topology!.edge_slices.push(scene.folder_topology!.edge_slices[0])],
    ["slice missing edge", (scene) => { scene.folder_topology!.edge_slices[0].edge_id = "missing"; }],
    ["slice missing endpoint partition", (scene) => { scene.folder_topology!.edge_slices[0].source_entity_kind = "library"; }],
    ["relation total differs", (scene) => { scene.folder_topology!.edge_slices[0].relation_count = 2; }],
    ["occurrence total differs", (scene) => { scene.folder_topology!.edge_slices[0].occurrence_weight = 3; }],
    ["nonpositive occurrence weight", (scene) => { scene.edges[0].occurrence_weight = 0; }],
    ["unsafe occurrence weight", (scene) => { scene.edges[0].occurrence_weight = 2 ** 53; }],
    ["topology with legacy missing weight", (scene) => { delete scene.edges[0].occurrence_weight; }],
    ["topology with legacy null weight", (scene) => { scene.edges[0].occurrence_weight = null; }],
    ["duplicate node ID", (scene) => scene.nodes.push(scene.nodes[0])],
    ["duplicate entity key", (scene) => scene.nodes.push({ ...scene.nodes[0], id: "new-row" })],
    ["duplicate edge ID", (scene) => scene.edges.push(scene.edges[0])],
    ["dangling edge endpoint", (scene) => { scene.edges[0].target = "hidden"; }],
    ["row budget exceeded", (scene) => {
      scene.folder_topology!.folders = Array.from({ length: 200_001 }, () => scene.folder_topology!.folders[0]);
    }],
  ];
  it.each(invalid)("rejects malformed present topology: %s", (_label, mutate) => {
    const response = folderResponse();
    mutate(response.data);
    expect(isWorkspaceGraphResponse(response)).toBe(false);
  });

  it.each([NaN, Infinity, -1, 0.5, 2 ** 53])("rejects unsafe numeric scene counts: %s", (count) => {
    const response = folderResponse();
    response.data.counts.candidate_documents = count;
    expect(isWorkspaceGraphResponse(response)).toBe(false);
  });
});

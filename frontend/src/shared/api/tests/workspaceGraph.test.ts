import { describe, expect, it } from "vitest";

import { isWorkspaceGraphResponse } from "../workspaceGraph";

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
    }, {
      grain: "folder",
      id: "folder:root",
      folder_id: "00000000-0000-4000-8000-0000000000f1",
      label: "Root",
      path: "Root",
    }],
    edges: [{
      id: "contains:folder:root|context:one",
      source: "folder:root",
      target: "context:one",
      relation_kind: "tree",
      label: null,
      layer: "knowledge",
      direction: "directed",
      aggregate: false,
      relation_count: 1,
      occurrence_weight: 1,
      cross_folder: false,
      primary: true,
    }],
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
    ["folder without its folder", {
      nodes: [validResponse.data.nodes[0], { ...validResponse.data.nodes[1], folder_id: "" }],
    }],
    ["nonpositive occurrence weight", { edges: [{ ...validResponse.data.edges[0], occurrence_weight: 0 }] }],
    ["unsafe occurrence weight", { edges: [{ ...validResponse.data.edges[0], occurrence_weight: 2 ** 53 }] }],
  ])("rejects a malformed payload: %s", (_label, replacement) => {
    expect(isWorkspaceGraphResponse({
      data: { ...validResponse.data, ...replacement },
    })).toBe(false);
  });

  it.each([NaN, Infinity, -1, 0.5, 2 ** 53])("rejects unsafe numeric scene counts: %s", (count) => {
    const response = structuredClone(validResponse);
    response.data.counts.candidate_documents = count;
    expect(isWorkspaceGraphResponse(response)).toBe(false);
  });
});

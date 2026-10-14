import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import type { WorkspaceGraphScene } from "../../../../shared/api/workspaceGraph";
import {
  buildWorkspaceMapColumns,
  sha256Hex,
  WORKSPACE_MAP_EDGE_KIND,
  WORKSPACE_MAP_NODE_KIND,
} from "../sceneColumns";

const options = {
  workspaceId: "workspace-a",
  actorId: "actor-a",
  selectionKey: "links:all-visible",
};

function visibleScene(): WorkspaceGraphScene {
  return {
    schema_version: "workspace-graph-scene.v1",
    workspace_scope_considered: true,
    head_mode: "published",
    graph_revision: "revision-a",
    generated_at: "2026-09-28T00:00:00Z",
    algorithm_version: "workspace-graph.v1",
    metric_basis: "visible references",
    representation: "exact",
    completeness: "complete",
    truncated: false,
    counts: {
      candidate_documents: 3,
      candidate_relations: 2,
      rendered_documents: 3,
      rendered_clusters: 0,
      rendered_relations: 2,
    },
    omission_reasons: [],
    nodes: [
      {
        grain: "document",
        id: "row-z",
        entity_kind: "context",
        entity_id: "page-z",
        label: "Zeta",
        path: "/pages/zeta",
        primary_folder_id: "folder-visible",
        primary_folder_label: "Research",
        reference_count: 9,
        evidence_count: 0,
        also_appears_in: [],
      },
      {
        grain: "document",
        id: "row-a",
        entity_kind: "library",
        entity_id: "item-a",
        label: "Alpha",
        path: "/library/alpha",
        primary_folder_id: null,
        primary_folder_label: "No visible placement",
        reference_count: 1,
        evidence_count: 1,
        also_appears_in: [],
      },
      {
        grain: "document",
        id: "row-b",
        entity_kind: "context",
        entity_id: "page-b",
        label: "Beta",
        path: "/pages/beta",
        primary_folder_id: "folder-visible",
        primary_folder_label: "Research",
        reference_count: 3,
        evidence_count: 2,
        also_appears_in: [],
      },
    ],
    edges: [
      {
        id: "projection-edge-z",
        source: "row-z",
        target: "row-a",
        relation_kind: "wikilink",
        label: "links to",
        layer: "knowledge",
        direction: "directed",
        aggregate: false,
        relation_count: 2,
        cross_folder: true,
      },
      {
        id: "projection-edge-b",
        source: "row-b",
        target: "row-z",
        relation_kind: "citation_page",
        label: "cites",
        layer: "evidence",
        direction: "directed",
        aggregate: false,
        relation_count: 1,
        cross_folder: false,
      },
    ],
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
      evidence: true,
      export: true,
      directory: true,
      compare: true,
      growth: false,
      provenance: false,
    },
  };
}

describe("Workspace Map visible columns", () => {
  it("keeps authored relation counts separate from occurrence weights and their layout identity", () => {
    const scene = visibleScene();
    scene.edges[0].occurrence_weight = 17;
    scene.edges[1].occurrence_weight = 0x1_0000_0001;
    const columns = buildWorkspaceMapColumns(scene, options);
    expect(columns.edgeCounts).toEqual(Uint32Array.from([1, 2]));
    expect(columns.edgeWeights).toEqual(Float64Array.from([0x1_0000_0001, 17]));
    const changed = structuredClone(scene);
    changed.edges[0].occurrence_weight = 18;
    const after = buildWorkspaceMapColumns(changed, options);
    expect(after.edgeCounts).toEqual(columns.edgeCounts);
    expect(after.nodeSizes).toEqual(columns.nodeSizes);
    expect(after.identity.visibleFingerprint).not.toBe(columns.identity.visibleFingerprint);
    expect(buildWorkspaceMapColumns(visibleScene(), options).edgeWeights)
      .toEqual(Float64Array.from([1, 2]));
  });

  it.each([0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])(
    "rejects an explicit invalid occurrence weight %s instead of silently using counts",
    (weight) => {
      const scene = visibleScene();
      scene.edges[0].occurrence_weight = weight;
      expect(() => buildWorkspaceMapColumns(scene, options)).toThrow(/occurrence weight/);
    },
  );

  it("excludes explicitly hidden unlinked document geometry and scopes layout identity to displayed nodes", () => {
    const scene = visibleScene();
    const original = buildWorkspaceMapColumns(scene, options);
    const without = buildWorkspaceMapColumns(scene, { ...options, excludedNodeIds: new Set(["row-b"]) });
    expect(without.nodeIds).toEqual(["row-z", "row-a"]);
    expect(without.edgeIds).toEqual(["projection-edge-z"]);
    expect(without.identity.visibleFingerprint).not.toBe(original.identity.visibleFingerprint);
    expect(without.edgeSources[0]).toBe(0);
    expect(without.edgeTargets[0]).toBe(1);
    expect(buildWorkspaceMapColumns(scene, options)).toEqual(original);
  });
  it("hashes UTF-8 and multi-block sources as SHA-256", () => {
    for (const source of ["", "abc", "한글 graph", "x".repeat(2_000)]) {
      expect(sha256Hex(source)).toBe(createHash("sha256").update(source).digest("hex"));
    }
  });

  it("indexes stable visible nodes and relation columns for WebGL", () => {
    const columns = buildWorkspaceMapColumns(visibleScene(), options);

    expect(columns.nodeKeys).toEqual(["context:page-b", "context:page-z", "library:item-a"]);
    expect(columns.nodeIds).toEqual(["row-b", "row-z", "row-a"]);
    expect(columns.nodeKinds).toEqual(Uint8Array.from([
      WORKSPACE_MAP_NODE_KIND.context,
      WORKSPACE_MAP_NODE_KIND.context,
      WORKSPACE_MAP_NODE_KIND.library,
    ]));
    expect(columns.nodeFolderKeys).toEqual(["folder-visible", "folder-visible", null]);
    expect(columns.nodeSizes.every((size) => size >= 1.5 && size <= 8.5)).toBe(true);
    expect(columns.edgeIds).toEqual(["projection-edge-b", "projection-edge-z"]);
    expect(columns.edgeSources).toEqual(Uint32Array.from([0, 1]));
    expect(columns.edgeTargets).toEqual(Uint32Array.from([1, 2]));
    expect(columns.edgeKinds).toEqual(Uint8Array.from([
      WORKSPACE_MAP_EDGE_KIND.citation,
      WORKSPACE_MAP_EDGE_KIND.link,
    ]));
    expect(columns.edgeCounts).toEqual(Uint32Array.from([1, 2]));
    expect(columns.edgeCrossFolder).toEqual(Uint8Array.from([0, 1]));
    expect(columns.identity).toMatchObject({
      workspaceId: "workspace-a",
      actorId: "actor-a",
      surface: "workspace",
    });
    expect(columns.identity.visibleFingerprint).toMatch(/^[0-9a-f]{64}$/);
  });

  it("ignores response order, projection IDs and non-layout metadata in the fingerprint", () => {
    const original = visibleScene();
    const changed = visibleScene();
    changed.nodes.reverse();
    changed.edges.reverse();
    changed.nodes = changed.nodes.map((node) => ({
      ...node,
      id: `${node.id}-new`,
      label: `${node.label} renamed`,
    }));
    changed.edges = changed.edges.map((edge) => ({
      ...edge,
      id: `${edge.id}-new`,
      source: `${edge.source}-new`,
      target: `${edge.target}-new`,
    }));
    changed.graph_revision = "revision-with-unseen-decoy";
    changed.generated_at = "2026-09-29T00:00:00Z";
    changed.counts.candidate_documents = 1000;
    changed.facet_counts.folders = [{ value: "hidden", label: "Hidden", count: 1000 }];

    const before = buildWorkspaceMapColumns(original, options);
    const after = buildWorkspaceMapColumns(changed, options);
    expect(after.identity).toEqual(before.identity);
    expect(after.nodeKeys).toEqual(before.nodeKeys);
    expect(after.edgeSources).toEqual(before.edgeSources);
    expect(after.edgeTargets).toEqual(before.edgeTargets);
  });

  it("omits dangling, self and unclassified relations without adding hidden points", () => {
    const scene = visibleScene();
    const baseline = buildWorkspaceMapColumns(scene, options);
    scene.edges.push(
      {
        ...scene.edges[0],
        id: "hidden-endpoint",
        target: "row-hidden",
      },
      {
        ...scene.edges[0],
        id: "self-edge",
        target: "row-z",
      },
      {
        ...scene.edges[0],
        id: "co-access",
        relation_kind: "co_access",
      },
      {
        ...scene.edges[0],
        id: "unknown",
        relation_kind: "future_secret_relation",
        layer: "knowledge",
      },
    );

    const columns = buildWorkspaceMapColumns(scene, options);
    expect(columns.nodeIds).toEqual(baseline.nodeIds);
    expect(columns.edgeIds).toEqual(baseline.edgeIds);
    expect(columns.identity.visibleFingerprint).toBe(baseline.identity.visibleFingerprint);
  });

  it("keeps a semantic fingerprint when projection IDs swap across parallel relations", () => {
    const first = visibleScene();
    first.edges.push({
      ...first.edges[0],
      id: "parallel-a",
      relation_count: 3,
    });
    const second = visibleScene();
    second.edges[0].id = "parallel-a";
    second.edges.push({
      ...second.edges[0],
      id: "projection-edge-z",
      relation_count: 3,
    });

    expect(buildWorkspaceMapColumns(second, options).identity.visibleFingerprint)
      .toBe(buildWorkspaceMapColumns(first, options).identity.visibleFingerprint);
  });

  it("changes fingerprint for visible relation counts, not folder metadata or actor identity", () => {
    const base = buildWorkspaceMapColumns(visibleScene(), options);
    const changedEdge = visibleScene();
    changedEdge.edges[0].relation_count += 1;
    const changedFolder = visibleScene();
    if (changedFolder.nodes[0].grain === "document") {
      changedFolder.nodes[0].primary_folder_id = "another-visible-folder";
    }

    expect(buildWorkspaceMapColumns(changedEdge, options).identity.visibleFingerprint)
      .not.toBe(base.identity.visibleFingerprint);
    expect(buildWorkspaceMapColumns(changedFolder, options).identity.visibleFingerprint)
      .toBe(base.identity.visibleFingerprint);
    const otherActor = buildWorkspaceMapColumns(visibleScene(), { ...options, actorId: "actor-b" });
    expect(otherActor.identity.visibleFingerprint).toBe(base.identity.visibleFingerprint);
    expect(otherActor.identity).not.toEqual(base.identity);
  });

  it("keeps relation geometry and parallel-edge ordering independent of directory assignment", () => {
    const original = visibleScene();
    original.edges.push({ ...original.edges[0], id: "parallel-a", cross_folder: false });
    const baseline = buildWorkspaceMapColumns(original, options);
    const reassigned = visibleScene();
    reassigned.edges.push({ ...reassigned.edges[0], id: "parallel-a", cross_folder: false });
    reassigned.nodes = reassigned.nodes.map((node) => node.grain === "document"
      ? { ...node, primary_folder_id: "different-directory", primary_folder_label: "Changed" }
      : node);
    reassigned.edges = reassigned.edges.map((edge) => ({ ...edge, cross_folder: !edge.cross_folder }));

    const after = buildWorkspaceMapColumns(reassigned, options);
    expect(after.identity).toEqual(baseline.identity);
    expect(after.nodeFolderKeys).not.toEqual(baseline.nodeFolderKeys);
    expect(after.edgeCrossFolder).not.toEqual(baseline.edgeCrossFolder);
    expect(after.edgeIds).toEqual(baseline.edgeIds);
    expect(after.edgeSources).toEqual(baseline.edgeSources);
    expect(after.edgeTargets).toEqual(baseline.edgeTargets);
    expect(after.edgeWeights).toEqual(baseline.edgeWeights);
    expect(after.nodeSizes).toEqual(baseline.nodeSizes);
  });

  it("keeps presentation search out of authoritative columns and scopes server filters and actor identity", () => {
    const base = buildWorkspaceMapColumns(visibleScene(), options);
    for (const search of ["alpha", "no matching document", ""]) {
      const presentationOptions = { ...options, search };
      expect(buildWorkspaceMapColumns(visibleScene(), presentationOptions)).toEqual(base);
    }
    const filtered = buildWorkspaceMapColumns(visibleScene(), {
      ...options, selectionKey: "links:server-filtered",
    });
    expect(filtered.identity.selectionKey).not.toBe(base.identity.selectionKey);
    expect(filtered.identity.visibleFingerprint).toBe(base.identity.visibleFingerprint);
    expect(() => buildWorkspaceMapColumns(visibleScene(), { ...options, actorId: "" }))
      .toThrow(/actor/);
  });
});

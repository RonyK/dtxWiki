import { describe, expect, it } from "vitest";

import type { WorkspaceGraphScene } from "../../../../shared/api/workspaceGraph";
import type { WorkspaceMapColumns } from "../sceneColumns";
import { buildWorkspaceMapColumns } from "../sceneColumns";
import {
  computeWorkspaceMapCommunities,
  workspaceCommunitySeed,
} from "../communityLayout";

function columns(edges: Array<[number, number, number]>): WorkspaceMapColumns {
  const nodeKeys = ["context:a", "context:b", "context:c", "context:d", "context:e", "context:f"];
  return {
    identity: {
      workspaceId: "workspace-visible",
      actorId: "actor-visible",
      surface: "workspace",
      selectionKey: "links",
      visibleFingerprint: "visible-fingerprint",
    },
    nodeKeys,
    nodeIds: [...nodeKeys],
    nodeLabels: [...nodeKeys],
    nodeFolderKeys: nodeKeys.map(() => null),
    nodeKinds: new Uint8Array(nodeKeys.length),
    nodeSizes: new Float32Array(nodeKeys.length).fill(3),
    edgeIds: edges.map((_edge, index) => `edge-${index}`),
    edgeSources: Uint32Array.from(edges.map((edge) => edge[0])),
    edgeTargets: Uint32Array.from(edges.map((edge) => edge[1])),
    edgeCounts: Uint32Array.from(edges.map((edge) => edge[2])),
    edgeWeights: Float64Array.from(edges.map((edge) => edge[2])),
    edgeKinds: new Uint8Array(edges.length),
    edgeCrossFolder: new Uint8Array(edges.length),
  };
}

const twoCliques: Array<[number, number, number]> = [
  [0, 1, 4], [1, 2, 4], [0, 2, 4],
  [3, 4, 4], [4, 5, 4], [3, 5, 4],
  [2, 3, 1],
];

describe("Workspace Map visible-only Louvain", () => {
  it.each([-1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])(
    "rejects invalid occurrence weight %s before Louvain",
    (weight) => {
      expect(() => computeWorkspaceMapCommunities({
        workspaceId: "workspace-visible",
        nodeKeys: ["context:a", "context:b"],
        edgeSources: Uint32Array.from([0]),
        edgeTargets: Uint32Array.from([1]),
        edgeWeights: Float64Array.from([weight]),
      })).toThrow(/occurrence weight/);
    },
  );

  it("rejects overflowing reciprocal weights instead of changing layout through numeric rounding", () => {
    expect(() => computeWorkspaceMapCommunities({
      workspaceId: "workspace-visible",
      nodeKeys: ["context:a", "context:b"],
      edgeSources: Uint32Array.from([0, 1]),
      edgeTargets: Uint32Array.from([1, 0]),
      edgeWeights: Float64Array.from([Number.MAX_SAFE_INTEGER, 1]),
    })).toThrow(/safe integer range/);
  });

  it("uses occurrence weights, not distinct relation counts, to separate visible communities", () => {
    const base = {
      workspaceId: "workspace-visible",
      nodeKeys: ["context:a", "context:b", "context:c", "context:d"],
      edgeSources: Uint32Array.from([0, 1, 2, 3]),
      edgeTargets: Uint32Array.from([1, 2, 3, 0]),
    };
    const first = computeWorkspaceMapCommunities({
      ...base, edgeWeights: Float64Array.from([1000, 1, 1000, 1]),
    });
    const second = computeWorkspaceMapCommunities({
      ...base, edgeWeights: Float64Array.from([1, 1000, 1, 1000]),
    });
    expect(first.communityIds).toEqual(Uint32Array.from([0, 0, 1, 1]));
    expect(second.communityIds).toEqual(Uint32Array.from([0, 1, 1, 0]));
  });

  it("normalizes IDs by minimum entity key and ignores input edge order/direction", () => {
    const first = columns(twoCliques);
    const reversed = columns([...twoCliques].reverse().map(([source, target, count]) => [target, source, count]));
    const a = computeWorkspaceMapCommunities({
      workspaceId: first.identity.workspaceId,
      nodeKeys: first.nodeKeys,
      edgeSources: first.edgeSources,
      edgeTargets: first.edgeTargets,
      edgeWeights: first.edgeWeights,
    });
    const b = computeWorkspaceMapCommunities({
      workspaceId: reversed.identity.workspaceId,
      nodeKeys: reversed.nodeKeys,
      edgeSources: reversed.edgeSources,
      edgeTargets: reversed.edgeTargets,
      edgeWeights: reversed.edgeWeights,
    });
    expect(a).toEqual(b);
    expect(a.communityIds).toEqual(Uint32Array.from([0, 0, 0, 1, 1, 1]));
    expect(a.communityCount).toBe(2);
    expect(a.initialPositions).toHaveLength(12);
    const center = (indices: number[]) => ({
      x: indices.reduce((sum, index) => sum + a.initialPositions[index * 2], 0) / indices.length,
      y: indices.reduce((sum, index) => sum + a.initialPositions[index * 2 + 1], 0) / indices.length,
    });
    const leftCenter = center([0, 1, 2]);
    const rightCenter = center([3, 4, 5]);
    expect(Math.hypot(leftCenter.x - rightCenter.x, leftCenter.y - rightCenter.y))
      .toBeGreaterThan(100);
    for (const position of a.initialPositions) {
      expect(Number.isFinite(position)).toBe(true);
      expect(position).toBeCloseTo(Math.round(position * 1000) / 1000, 2);
    }
  });

  it("combines reciprocal visible relations as one weighted undirected edge", () => {
    const aggregate = columns(twoCliques);
    const split = columns(twoCliques.flatMap(([source, target, count]) => [
      [source, target, count - 1] as [number, number, number],
      [target, source, 1] as [number, number, number],
    ]));
    const run = (input: WorkspaceMapColumns) => computeWorkspaceMapCommunities({
      workspaceId: input.identity.workspaceId,
      nodeKeys: input.nodeKeys,
      edgeSources: input.edgeSources,
      edgeTargets: input.edgeTargets,
      edgeWeights: input.edgeWeights,
    });
    expect(run(split)).toEqual(run(aggregate));
  });

  it("keeps communities and coordinates identical when only a hidden decoy changes", () => {
    const scene = {
      schema_version: "workspace-graph-scene.v1",
      workspace_scope_considered: true,
      head_mode: "published",
      graph_revision: "before",
      generated_at: "2026-09-28T00:00:00Z",
      algorithm_version: "workspace-graph.v1",
      metric_basis: "visible references",
      representation: "exact",
      completeness: "complete",
      truncated: false,
      counts: {
        candidate_documents: 6,
        candidate_relations: 7,
        rendered_documents: 6,
        rendered_clusters: 0,
        rendered_relations: 7,
      },
      omission_reasons: [],
      nodes: columns(twoCliques).nodeKeys.map((key) => ({
        grain: "document" as const,
        id: key,
        entity_kind: "context" as const,
        entity_id: key,
        label: key,
        path: `/pages/${key}`,
        primary_folder_id: null,
        primary_folder_label: "Unplaced",
        reference_count: 0,
        evidence_count: 0,
        also_appears_in: [],
      })),
      edges: twoCliques.map(([source, target, count], index) => ({
        id: `edge-${index}`,
        source: `context:${"abcdef"[source]}`,
        target: `context:${"abcdef"[target]}`,
        relation_kind: "wikilink" as const,
        label: "links to",
        layer: "knowledge" as const,
        direction: "directed" as const,
        aggregate: false,
        relation_count: count,
        cross_folder: false,
      })),
      facet_counts: { document_kinds: [], relation_kinds: [], folders: [], quick_filters: [] },
      expansion_cursor: null,
      warnings: [],
      capabilities: {
        expand: false, evidence: true, export: true, directory: true,
        compare: true, growth: false, provenance: false,
      },
    } satisfies WorkspaceGraphScene;
    const withDecoy = structuredClone(scene) as WorkspaceGraphScene;
    withDecoy.graph_revision = "after-hidden-decoy";
    withDecoy.counts.candidate_documents = 7;
    withDecoy.edges.push({
      ...withDecoy.edges[0], id: "hidden-target-edge", target: "hidden-decoy",
    });
    const options = { workspaceId: "workspace-visible", actorId: "actor-visible", selectionKey: "links" };
    const before = buildWorkspaceMapColumns(scene, options);
    const after = buildWorkspaceMapColumns(withDecoy, options);
    const run = (input: WorkspaceMapColumns) => computeWorkspaceMapCommunities({
      workspaceId: input.identity.workspaceId,
      nodeKeys: input.nodeKeys,
      edgeSources: input.edgeSources,
      edgeTargets: input.edgeTargets,
      edgeWeights: input.edgeWeights,
    });
    expect(after.identity).toEqual(before.identity);
    expect(run(after)).toEqual(run(before));
    expect(workspaceCommunitySeed("workspace-visible"))
      .toBe(workspaceCommunitySeed("workspace-visible"));
  });

  it("rejects absent visible endpoints and noncanonical keys", () => {
    const visible = columns([[0, 6, 1]]);
    expect(() => computeWorkspaceMapCommunities({
      workspaceId: visible.identity.workspaceId,
      nodeKeys: visible.nodeKeys,
      edgeSources: visible.edgeSources,
      edgeTargets: visible.edgeTargets,
      edgeWeights: visible.edgeWeights,
    })).toThrow(/missing visible endpoint/);
    expect(() => computeWorkspaceMapCommunities({
      workspaceId: visible.identity.workspaceId,
      nodeKeys: ["b", "a"],
      edgeSources: new Uint32Array(0),
      edgeTargets: new Uint32Array(0),
      edgeWeights: new Float64Array(0),
    })).toThrow(/unique and sorted/);
  });

  it("handles zero or one community without divide-by-zero or nonfinite coordinates", () => {
    const empty = computeWorkspaceMapCommunities({
      workspaceId: "workspace-visible",
      nodeKeys: [],
      edgeSources: new Uint32Array(0),
      edgeTargets: new Uint32Array(0),
      edgeWeights: new Float64Array(0),
    });
    expect(empty).toEqual({
      communityIds: new Uint32Array(0),
      communityCount: 0,
      initialPositions: new Float32Array(0),
    });
    const one = computeWorkspaceMapCommunities({
      workspaceId: "workspace-visible",
      nodeKeys: ["context:a"],
      edgeSources: new Uint32Array(0),
      edgeTargets: new Uint32Array(0),
      edgeWeights: new Float64Array(0),
    });
    expect(one.communityIds).toEqual(Uint32Array.from([0]));
    expect(one.initialPositions).toEqual(Float32Array.from([2048, 2048]));
  });

  it("packs 20k visible isolates inside Cosmos's nonnegative 4096-unit space", () => {
    const count = 20_000;
    const result = computeWorkspaceMapCommunities({
      workspaceId: "workspace-isolates",
      nodeKeys: Array.from({ length: count }, (_unused, index) => `key-${String(index).padStart(5, "0")}`),
      edgeSources: new Uint32Array(0),
      edgeTargets: new Uint32Array(0),
      edgeWeights: new Float64Array(0),
    });
    expect(result.communityCount).toBe(count);
    expect(result.communityIds[0]).toBe(0);
    expect(result.communityIds[count - 1]).toBe(count - 1);
    expect(result.initialPositions).toHaveLength(count * 2);
    let lowest = Infinity;
    let highest = -Infinity;
    for (const position of result.initialPositions) {
      lowest = Math.min(lowest, position);
      highest = Math.max(highest, position);
    }
    expect(Number.isFinite(lowest)).toBe(true);
    expect(Number.isFinite(highest)).toBe(true);
    expect(lowest).toBeGreaterThanOrEqual(248);
    expect(highest).toBeLessThanOrEqual(3848);
  }, 10_000);

  it("uses multiple rows rather than shrinking dozens of groups onto one ring", () => {
    const count = 49;
    const result = computeWorkspaceMapCommunities({
      workspaceId: "workspace-many-groups",
      nodeKeys: Array.from({ length: count }, (_unused, index) => `key-${String(index).padStart(3, "0")}`),
      edgeSources: new Uint32Array(0),
      edgeTargets: new Uint32Array(0),
      edgeWeights: new Float64Array(0),
    });
    expect(result.communityCount).toBe(count);
    expect(new Set(Array.from({ length: count }, (_unused, index) =>
      result.initialPositions[index * 2 + 1])).size).toBeLessThan(15);
    expect(result.initialPositions.every((position) => position >= 248 && position <= 3848))
      .toBe(true);
  });

  it("places strongly related communities nearer without folder hubs or synthetic document relations", () => {
    const nodeCount = 32;
    const edges: Array<[number, number, number]> = [];
    for (let group = 0; group < 4; group++) {
      for (let source = 0; source < 8; source++) {
        for (let target = source + 1; target < 8; target++) {
          edges.push([group * 8 + source, group * 8 + target, 1_024]);
        }
      }
    }
    // Opposite IDs on the previous ring/grid are the strongest neighbours.
    edges.push([0, 16, 128], [8, 24, 128]);
    const run = (rows: typeof edges) => computeWorkspaceMapCommunities({
      workspaceId: "workspace-topology",
      nodeKeys: Array.from({ length: nodeCount }, (_, index) => `key-${String(index).padStart(3, "0")}`),
      edgeSources: Uint32Array.from(rows.map(([source]) => source)),
      edgeTargets: Uint32Array.from(rows.map(([, target]) => target)),
      edgeWeights: Float64Array.from(rows.map(([, , weight]) => weight)),
    });
    const result = run(edges);
    expect(result.communityCount).toBe(4);
    const center = (group: number) => {
      const members = Array.from({ length: nodeCount }, (_, index) => index)
        .filter((index) => result.communityIds[index] === group);
      return [0, 1].map((axis) => members.reduce((sum, index) => sum + result.initialPositions[index * 2 + axis], 0) / members.length);
    };
    const distance = (a: number, b: number) => Math.hypot(center(a)[0] - center(b)[0], center(a)[1] - center(b)[1]);
    expect(distance(0, 2)).toBeLessThan(distance(0, 1));
    expect(distance(1, 3)).toBeLessThan(distance(1, 2));
    expect(result.initialPositions).toHaveLength(nodeCount * 2);
    expect(run([...edges].reverse().map(([s, t, w]) => [t, s, w]))).toEqual(result);
  });

  it("computes a full 20k-node/47k-edge visible graph without invalid indices", () => {
    const nodeCount = 20_000;
    const edgeCount = 47_000;
    const edgeSources = Uint32Array.from(
      { length: edgeCount }, (_unused, index) => index % nodeCount,
    );
    const edgeTargets = Uint32Array.from(
      { length: edgeCount }, (_unused, index) =>
        (index % nodeCount + 1 + Math.floor(index / nodeCount)) % nodeCount,
    );
    const result = computeWorkspaceMapCommunities({
      workspaceId: "workspace-large-visible",
      nodeKeys: Array.from({ length: nodeCount }, (_unused, index) =>
        `key-${String(index).padStart(5, "0")}`),
      edgeSources,
      edgeTargets,
      edgeWeights: new Float64Array(edgeCount).fill(1),
    });
    expect(result.communityIds).toHaveLength(nodeCount);
    expect(result.initialPositions).toHaveLength(nodeCount * 2);
    expect(result.communityCount).toBeGreaterThan(0);
    expect(result.communityCount).toBeLessThanOrEqual(nodeCount);
    expect(result.communityIds.every((id) => id < result.communityCount)).toBe(true);
    expect(result.initialPositions.every((position) => Number.isFinite(position)
      && position >= 248 && position <= 3848)).toBe(true);
  }, 30_000);
});

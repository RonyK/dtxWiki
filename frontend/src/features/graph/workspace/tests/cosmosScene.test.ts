import { describe, expect, it } from "vitest";

import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import type { GraphVisualTokens } from "../../shared/graphTokens";
import { WORKSPACE_MAP_PC_10K_BUDGET } from "../../shared/workspaceMapBudget";
import { buildCosmosRelationColors, buildCosmosSceneBuffers, incidentStar, isDenseWorkspaceMapOverview, isReadableWorkspaceMapScene } from "../cosmosScene";

const tokens: GraphVisualTokens = {
  surface: "#101827",
  node: { context: "#8b5cf6", library: "#0d9488", conversation: "#ea580c", folder: "#94a3b8" },
  edge: { link: "#64748b", citation: "#0d9488", coAccess: "#ea580c", membership: "#64748b", action: "#a78bfa" },
};

function columns(): WorkspaceMapColumns {
  return {
    identity: { workspaceId: "w", actorId: "a", surface: "workspace", selectionKey: "combined", visibleFingerprint: "f" },
    nodeIds: ["a", "b", "c"], nodeKeys: ["context:a", "library:b", "context:c"],
    nodeLabels: ["A", "B", "C"], nodeFolderKeys: ["folder", null, "folder"],
    nodeKinds: new Uint8Array([0, 1, 2]), nodeSizes: new Float32Array([2, 3, 4]),
    edgeIds: ["ab", "bc", "ac"],
    edgeSources: new Uint32Array([0, 1, 0]), edgeTargets: new Uint32Array([1, 2, 2]),
    edgeKinds: new Uint8Array([0, 1, 0]), edgeCounts: new Uint32Array([1, 1, 1]),
    edgeWeights: new Float64Array([1, 1, 1]),
    edgeCrossFolder: new Uint8Array([1, 1, 0]),
  };
}

function sizedColumns(nodeCount: number, edgeCount: number): WorkspaceMapColumns {
  return { ...columns(),
    nodeIds: Array.from({ length: nodeCount }, (_, index) => `n${index}`),
    nodeKeys: Array.from({ length: nodeCount }, (_, index) => `context:${index}`),
    nodeLabels: Array.from({ length: nodeCount }, (_, index) => `Document ${index}`),
    nodeFolderKeys: new Array<null>(nodeCount).fill(null),
    nodeKinds: new Uint8Array(nodeCount), nodeSizes: new Float32Array(nodeCount).fill(3),
    edgeIds: Array.from({ length: edgeCount }, (_, index) => `e${index}`),
    edgeSources: Uint32Array.from({ length: edgeCount }, (_, index) => index % nodeCount),
    edgeTargets: Uint32Array.from({ length: edgeCount }, (_, index) => (index + 1) % nodeCount),
    edgeKinds: new Uint8Array(edgeCount), edgeCounts: new Uint32Array(edgeCount).fill(1),
    edgeWeights: new Float64Array(edgeCount).fill(1), edgeCrossFolder: new Uint8Array(edgeCount),
  };
}

describe("Workspace Cosmos typed scene", () => {
  it("uses explicit PC 10K presentation bounds without admitting those counts to the default style", () => {
    expect(isReadableWorkspaceMapScene(10_000, 100_000, WORKSPACE_MAP_PC_10K_BUDGET)).toBe(true);
    expect(isReadableWorkspaceMapScene(10_001, 100_000, WORKSPACE_MAP_PC_10K_BUDGET)).toBe(false);
    expect(isReadableWorkspaceMapScene(10_000, 100_001, WORKSPACE_MAP_PC_10K_BUDGET)).toBe(false);
    expect(isReadableWorkspaceMapScene(10_000, 100_000)).toBe(false);
    expect(isDenseWorkspaceMapOverview(sizedColumns(2_001, 2), WORKSPACE_MAP_PC_10K_BUDGET)).toBe(true);
    expect(isDenseWorkspaceMapOverview(sizedColumns(2_001, 2))).toBe(false);
  });

  it("keeps every 10K/100K point and relation in typed buffers and selected-star ink", () => {
    const input = sizedColumns(10_000, 100_000);
    const buffers = buildCosmosSceneBuffers(input, tokens, WORKSPACE_MAP_PC_10K_BUDGET);
    expect(buffers.positions).toHaveLength(20_000);
    expect(buffers.links).toHaveLength(200_000);
    expect(buffers.adjacencyEdges).toHaveLength(200_000);
    expect(buffers.linkColors[3]).toBeCloseTo(.32);
    const communities = new Uint32Array(10_000); communities[1] = 1;
    const normal = buildCosmosRelationColors(input, buffers.linkColors, communities, [], WORKSPACE_MAP_PC_10K_BUDGET);
    expect(normal[3]).toBeCloseTo(.18);
    const selected = buildCosmosRelationColors(input, buffers.linkColors, communities, [0], WORKSPACE_MAP_PC_10K_BUDGET);
    expect(selected[3]).toBeCloseTo(.95);
    expect(selected[7]).toBeGreaterThan(0);
    expect(input.nodeIds).toHaveLength(10_000);
    expect(input.edgeIds).toHaveLength(100_000);
  });

  it("does not alter any 2K ink or positions when the explicit PC budget is supplied", () => {
    const input = sizedColumns(2_000, 6_000);
    expect(buildCosmosSceneBuffers(input, tokens, WORKSPACE_MAP_PC_10K_BUDGET))
      .toEqual(buildCosmosSceneBuffers(input, tokens));
    const colors = buildCosmosSceneBuffers(input, tokens).linkColors;
    const communities = new Uint32Array(2_000);
    expect(buildCosmosRelationColors(input, colors, communities, [0], WORKSPACE_MAP_PC_10K_BUDGET))
      .toEqual(buildCosmosRelationColors(input, colors, communities, [0]));
  });

  it("rejects retired synthetic folder native points even with old presentation metadata", () => {
    const input = {
      ...columns(),
      nodeKinds: new Uint8Array([0, 1, 3]),
      preparedScene: { supported: true, points: [{}, {}], folders: [{}], physicsRelations: [] },
      nodeRoles: ["document", "document", "folder"],
      sourceNodeIds: ["a", "b", null],
      nodeMemberCounts: new Float64Array([1, 1, 0]),
      folderNodeIndices: new Uint32Array([2]),
      edgeRoles: ["authored", "authored", "authored"],
      sourceEdgeIds: ["ab", "bc", "ac"],
      edgeDefaultVisible: new Uint8Array([1, 1, 1]),
    };
    expect(() => buildCosmosSceneBuffers(input, tokens)).toThrow("Unknown Workspace Map node kind");
  });

  it("preserves original document and relation counts, silhouettes and visible weighted cross-folder links", () => {
    const input = columns();
    input.edgeWeights = new Float64Array([1, 4, 3]);
    const originalNodeIds = [...input.nodeIds];
    const originalEdgeIds = [...input.edgeIds];
    const buffers = buildCosmosSceneBuffers(input, tokens);
    expect(input.nodeIds).toEqual(originalNodeIds);
    expect(input.edgeIds).toEqual(originalEdgeIds);
    expect(buffers.positions).toHaveLength(originalNodeIds.length * 2);
    expect(buffers.links).toHaveLength(originalEdgeIds.length * 2);
    expect(buffers.pointShapes).toEqual(Float32Array.from([0, 1, 0]));
    expect(buffers.pointColors.slice(0, 4)).not.toEqual(buffers.pointColors.slice(4, 8));
    expect(buffers).not.toHaveProperty("pointImageIndices");
    expect(buffers).not.toHaveProperty("pointImageSizes");
    expect(buffers).not.toHaveProperty("linkWidths");
    input.edgeCrossFolder.forEach((crossFolder, index) => {
      if (crossFolder) expect(buffers.linkColors[index * 4 + 3]).toBeGreaterThan(0);
    });
    expect(buffers.linkStrength[1]).toBeGreaterThan(buffers.linkStrength[0]);
    expect(buffers.linkStrength[2]).toBeGreaterThan(buffers.linkStrength[0]);
  });
  it("keeps the full 2k/6k API budget in the readable link style", () => {
    expect(isReadableWorkspaceMapScene(2_000, 5_057)).toBe(true);
    expect(isReadableWorkspaceMapScene(2_000, 6_000)).toBe(true);
    expect(isReadableWorkspaceMapScene(2_000, 6_001)).toBe(false);
    expect(isReadableWorkspaceMapScene(2_001, 5_057)).toBe(false);

    const input = columns();
    input.nodeIds = Array.from({ length: 2_000 }, (_, index) => `n${index}`);
    input.nodeKeys = input.nodeIds.map((id) => `context:${id}`);
    input.nodeLabels = input.nodeIds;
    input.nodeFolderKeys = input.nodeIds.map(() => "folder");
    input.nodeKinds = new Uint8Array(2_000);
    input.nodeSizes = new Float32Array(2_000).fill(2);
    input.edgeIds = Array.from({ length: 5_057 }, (_, index) => `e${index}`);
    input.edgeSources = Uint32Array.from({ length: 5_057 }, (_, index) => index % 2_000);
    input.edgeTargets = Uint32Array.from({ length: 5_057 }, (_, index) => (index + 1) % 2_000);
    input.edgeKinds = new Uint8Array(5_057);
    input.edgeCounts = new Uint32Array(5_057).fill(1);
    input.edgeWeights = new Float64Array(5_057).fill(1);
    input.edgeCrossFolder = new Uint8Array(5_057);
    const buffers = buildCosmosSceneBuffers(input, tokens);
    expect(buffers.linkColors[3]).toBeCloseTo(0.32);
  });

  it("separates 2K relation bands and the selected incident star without hiding or changing original relations", () => {
    const input = columns(), count = 2_000;
    input.nodeIds = Array.from({ length: count }, (_, index) => `n${index}`);
    input.nodeKeys = [...input.nodeIds]; input.nodeLabels = [...input.nodeIds];
    input.nodeFolderKeys = input.nodeIds.map(() => "same-folder");
    input.nodeKinds = new Uint8Array(count); input.nodeSizes = new Float32Array(count).fill(2);
    const base = buildCosmosSceneBuffers(input, tokens).linkColors;
    const saved = base.slice();
    const communities = new Uint32Array(count); communities[2] = 1;
    const normal = buildCosmosRelationColors(input, base, communities);
    expect([normal[3], normal[7], normal[11]]).toEqual([expect.closeTo(.32), expect.closeTo(.2), expect.closeTo(.18)]);
    const selected = buildCosmosRelationColors(input, base, communities, [0, 2]);
    expect([selected[3], selected[11]]).toEqual([expect.closeTo(.95), expect.closeTo(.95)]);
    expect(selected[7]).toBeGreaterThan(0); expect(selected[7]).toBeLessThan(selected[3]);
    for (let edge = 0; edge < input.edgeIds.length; edge++) {
      expect(selected.slice(edge * 4, edge * 4 + 3)).toEqual(base.slice(edge * 4, edge * 4 + 3));
    }
    expect(base).toEqual(saved);
    expect(buildCosmosRelationColors(input, base, communities)).toEqual(normal);
    expect(buildCosmosRelationColors({ ...input, nodeFolderKeys: input.nodeIds.map(() => null), edgeCrossFolder: new Uint8Array(3) }, base, communities)).toEqual(normal);
    expect(() => buildCosmosRelationColors(input, base, new Uint32Array(3))).toThrow("mismatched");
  });

  it("encodes native colors, shapes, widths and direct-incident hover stars", () => {
    const buffers = buildCosmosSceneBuffers(columns(), tokens);
    expect(Array.from(buffers.pointShapes)).toEqual([0, 1, 0]);
    expect(Array.from(buffers.pointSizes)).toEqual([4, 6, 8]);
    expect(Array.from(buffers.linkStyles)).toEqual([0, 1, 0]);
    expect(Array.from(buffers.links)).toEqual([0, 1, 1, 2, 0, 2]);
    expect(buffers.linkColors[3]).toBeCloseTo(0.85);
    expect(buffers.linkColors[7]).toBeCloseTo(0.7);
    expect(incidentStar(buffers, 1)).toEqual({ points: [1, 0, 2], links: [0, 1] });
    expect(incidentStar(buffers, -1)).toEqual({ points: [], links: [] });
  });

  it("uses deterministic directory-independent browser-only starting positions", () => {
    const first = buildCosmosSceneBuffers(columns(), tokens);
    const second = buildCosmosSceneBuffers(columns(), tokens);
    expect(first.positions).toEqual(second.positions);
    expect([...first.positions].every(Number.isFinite)).toBe(true);
    expect([...first.positions].every((position) => position > 0 && position < 4096)).toBe(true);
    expect(first.positions[0]).not.toBe(first.positions[2]);
    const permuted = columns();
    permuted.nodeFolderKeys = [null, "different-folder", "another-folder"];
    expect(buildCosmosSceneBuffers(permuted, tokens).positions).toEqual(first.positions);
  });

  it("builds the full 20k/47k path without creating node elements or CPU physics state", () => {
    const count = 20_000;
    const edgeCount = 47_000;
    const input = columns();
    input.nodeIds = Array.from({ length: count }, (_, index) => `n${index}`);
    input.nodeKeys = input.nodeIds.map((id) => `context:${id}`);
    input.nodeLabels = input.nodeIds;
    input.nodeFolderKeys = input.nodeIds.map((_, index) => `folder-${index % 100}`);
    input.nodeKinds = new Uint8Array(count);
    input.nodeSizes = new Float32Array(count).fill(2);
    input.edgeIds = Array.from({ length: edgeCount }, (_, index) => `e${index}`);
    input.edgeSources = Uint32Array.from({ length: edgeCount }, (_, index) => index % count);
    input.edgeTargets = Uint32Array.from({ length: edgeCount }, (_, index) => (index * 7 + 1) % count);
    input.edgeKinds = new Uint8Array(edgeCount);
    input.edgeCounts = new Uint32Array(edgeCount).fill(1);
    input.edgeWeights = new Float64Array(edgeCount).fill(1);
    input.edgeCrossFolder = new Uint8Array(edgeCount);
    const before = document.querySelectorAll("*").length;
    const buffers = buildCosmosSceneBuffers(input, tokens);
    expect(buffers.positions).toHaveLength(count * 2);
    expect(buffers.links).toHaveLength(edgeCount * 2);
    expect(buffers.adjacencyNodes).toHaveLength(edgeCount * 2);
    expect(buffers.pointSizes[0]).toBeCloseTo(2);
    expect(document.querySelectorAll("*").length).toBe(before);
  });

  it("keeps a 20k all-distinct-folder fallback inside Cosmos's positive space", () => {
    const input = columns();
    input.nodeIds = Array.from({ length: 20_000 }, (_, index) => `n${index}`);
    input.nodeKeys = input.nodeIds;
    input.nodeLabels = input.nodeIds;
    input.nodeFolderKeys = input.nodeIds.map((_, index) => `folder-${index}`);
    input.nodeKinds = new Uint8Array(20_000);
    input.nodeSizes = new Float32Array(20_000).fill(2);
    input.edgeIds = [];
    input.edgeSources = new Uint32Array(0);
    input.edgeTargets = new Uint32Array(0);
    input.edgeKinds = new Uint8Array(0);
    input.edgeCounts = new Uint32Array(0);
    input.edgeWeights = new Float64Array(0);
    input.edgeCrossFolder = new Uint8Array(0);
    const { positions } = buildCosmosSceneBuffers(input, tokens);
    expect([...positions].every((position) => position >= 248 && position <= 3848)).toBe(true);
  });

  it("rejects malformed or unbounded GPU input instead of silently truncating", () => {
    const input = columns();
    input.edgeTargets = new Uint32Array([1, 3, 2]);
    expect(() => buildCosmosSceneBuffers(input, tokens)).toThrow("Invalid Workspace Map edge");
    input.edgeTargets = new Uint32Array([1, 2]);
    expect(() => buildCosmosSceneBuffers(input, tokens)).toThrow("column budget or shape");
  });
});

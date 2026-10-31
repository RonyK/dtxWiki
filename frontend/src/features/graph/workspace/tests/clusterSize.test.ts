import { describe, expect, it } from "vitest";

import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import type { GraphVisualTokens } from "../../shared/graphTokens";
import { WORKSPACE_MAP_PC_10K_BUDGET } from "../../shared/workspaceMapBudget";
import { buildCosmosSceneBuffers } from "../cosmosScene";

const tokens: GraphVisualTokens = {
  surface: "#ffffff",
  node: { context: "#7c3aed", library: "#0f9f8f", conversation: "#ea580c", folder: "#64748b" },
  edge: { link: "#64748b", citation: "#0f9f8f", coAccess: "#ea580c", membership: "#94a3b8", action: "#7c3aed" },
};
const CLUSTER = 2;

function bytes(values: Float32Array): number[] {
  return Array.from(new Uint8Array(values.buffer, values.byteOffset, values.byteLength));
}

/** The same columns as a caller from before cluster emphasis built them: no document counts. */
function withoutCounts(columns: WorkspaceMapColumns): WorkspaceMapColumns {
  const bare = { ...columns } as Partial<WorkspaceMapColumns>;
  delete bare.nodeDocumentCounts;
  return bare as WorkspaceMapColumns;
}

/** Hand-built columns: `kinds(i)` is the node kind, `documentCounts(i)` the documents it stands for. */
function columnsOf(
  count: number,
  options: { kinds?: (index: number) => number; sizes?: (index: number) => number;
    documentCounts?: (index: number) => number } = {},
): WorkspaceMapColumns {
  const kinds = options.kinds ?? (() => 0);
  const sizes = options.sizes ?? (() => 3);
  const edgeCount = Math.max(0, count - 1);
  return {
    identity: { workspaceId: "w", actorId: "a", surface: "workspace", selectionKey: "combined", visibleFingerprint: "f" },
    nodeIds: Array.from({ length: count }, (_, index) => `n${index}`),
    nodeKeys: Array.from({ length: count }, (_, index) => `${kinds(index) === CLUSTER ? "cluster" : "context"}:${index}`),
    nodeLabels: Array.from({ length: count }, (_, index) => `Node ${index}`),
    nodeFolderKeys: new Array<null>(count).fill(null),
    nodeKinds: Uint8Array.from({ length: count }, (_, index) => kinds(index)),
    nodeSizes: Float32Array.from({ length: count }, (_, index) => sizes(index)),
    ...(options.documentCounts ? { nodeDocumentCounts: Uint32Array.from({ length: count }, (_, index) =>
      options.documentCounts!(index)) } : {}),
    edgeIds: Array.from({ length: edgeCount }, (_, index) => `e${index}`),
    edgeSources: Uint32Array.from({ length: edgeCount }, (_, index) => index),
    edgeTargets: Uint32Array.from({ length: edgeCount }, (_, index) => index + 1),
    edgeKinds: new Uint8Array(edgeCount), edgeCounts: new Uint32Array(edgeCount).fill(1),
    edgeWeights: new Float64Array(edgeCount).fill(1), edgeCrossFolder: new Uint8Array(edgeCount),
  };
}

/** One cluster per requested member count in front of ordinary documents (cluster keys sort first). */
function sceneWithClusters(memberCounts: readonly number[], total = 2_000): WorkspaceMapColumns {
  return columnsOf(total, {
    kinds: (index) => index < memberCounts.length ? CLUSTER : index % 7 === 0 ? 1 : 0,
    sizes: (index) => index < memberCounts.length ? 1.5 : 1.5 + (index % 8),
    documentCounts: (index) => index < memberCounts.length ? memberCounts[index] : 1,
  });
}

describe("Workspace Map cluster glyph size", () => {
  it.each([
    ["clusters but no document-count column", () => columnsOf(2_000, { kinds: (i) => i < 5 ? CLUSTER : 0, sizes: () => 8.5 })],
    ["clusters of nine documents or fewer", () => sceneWithClusters([2, 3, 9])],
    ["documents that claim many members", () => columnsOf(2_000, { kinds: (i) => i % 3 === 0 ? 1 : 0, sizes: () => 2.5,
      documentCounts: () => 50_000 })],
  ])("draws a scene with %s with the very array the layout reads", (_name, build) => {
    const buffers = buildCosmosSceneBuffers(build(), tokens);
    expect(buffers.drawPointSizes).toBe(buffers.pointSizes);
  });

  // `pointSizes` is the one array layout, FA2 units and collision read. The emphasis must never reach it.
  it("keeps pointSizes bit for bit what the same scene gets without document counts", () => {
    for (const [total, budget] of [[40, undefined], [2_500, WORKSPACE_MAP_PC_10K_BUDGET]] as const) {
      const input = sceneWithClusters([9_244, 747, 2], total);
      const emphasised = buildCosmosSceneBuffers(input, tokens, budget);
      const ordinary = buildCosmosSceneBuffers(withoutCounts(input), tokens, budget);
      expect(bytes(emphasised.pointSizes), `${total} nodes`).toEqual(bytes(ordinary.pointSizes));
      expect(emphasised.drawPointSizes, `${total} nodes`).not.toBe(emphasised.pointSizes);
    }
  });

  it("leaves clusters under ten documents ordinary and grows larger ones monotonically up to a bound", () => {
    const counts = [2, 3, 9, 10, 11, 100, 1_000, 10_000, 17_499, 0xffffffff];
    const { pointSizes, drawPointSizes } = buildCosmosSceneBuffers(sceneWithClusters(counts), tokens);
    const drawn = counts.map((_, row) => drawPointSizes[row]);
    for (const row of [0, 1, 2]) expect(drawn[row], `${counts[row]} members`).toBe(pointSizes[row]);
    expect(drawn[3]).toBeGreaterThan(pointSizes[3]);
    for (let row = 1; row < drawn.length; row += 1) expect(drawn[row], `${counts[row]} members`).toBeGreaterThanOrEqual(drawn[row - 1]);
    // Each order of magnitude is visibly larger than the one below.
    for (const [larger, smaller] of [[5, 3], [6, 5], [7, 6]]) expect(drawn[larger]).toBeGreaterThan(drawn[smaller]);
    // Bounded: finite for any count, and inside the native picker's 12 CSS-pixel accessible target (a 24 px body).
    expect(Math.max(...drawn)).toBeLessThanOrEqual(24);
    expect(Number.isFinite(drawn[drawn.length - 1])).toBe(true);
    // The defect: a cluster of thousands drawn no larger than a well-linked note.
    let largestDocument = 0;
    for (let row = counts.length; row < drawPointSizes.length; row += 1) largestDocument = Math.max(largestDocument, drawPointSizes[row]);
    expect(drawn[7]).toBeGreaterThan(largestDocument);
  });

  it("never draws a cluster smaller than its relation-based size", () => {
    // Below 1,000 nodes a well-linked cluster is already 17 px, above the 10-member emphasis.
    const input = columnsOf(40, { kinds: (index) => index < 3 ? CLUSTER : 0, sizes: () => 8.5,
      documentCounts: (index) => [10, 10_000, 2][index] ?? 1 });
    const { pointSizes, drawPointSizes } = buildCosmosSceneBuffers(input, tokens);
    expect(pointSizes[0]).toBe(17);
    expect([drawPointSizes[0], drawPointSizes[2]]).toEqual([17, 17]);
    expect(drawPointSizes[1]).toBeGreaterThan(17);
  });

  it("rejects a document-count column that does not describe the node rows", () => {
    const input = sceneWithClusters([40]);
    input.nodeDocumentCounts = new Uint32Array(input.nodeIds.length - 1).fill(1);
    expect(() => buildCosmosSceneBuffers(input, tokens)).toThrow("column budget or shape");
  });
});

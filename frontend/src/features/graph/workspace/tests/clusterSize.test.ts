import { describe, expect, it } from "vitest";

import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import type { GraphVisualTokens } from "../../shared/graphTokens";
import { WORKSPACE_MAP_PC_10K_BUDGET } from "../../shared/workspaceMapBudget";
import { buildCosmosSceneBuffers } from "../cosmosScene";
import { withActionEdgeColors } from "../../shared/graphTokens";

const tokens: GraphVisualTokens = {
  surface: "#ffffff",
  node: { context: "#7c3aed", library: "#0f9f8f", agent: "#2563eb", conversation: "#ea580c", folder: "#64748b" },
  edge: withActionEdgeColors({ link: "#64748b", citation: "#0f9f8f", coAccess: "#ea580c", membership: "#94a3b8", action: "#7c3aed" }),
  highlight: "#7c3aed", selection: "#0f172a", ink: "#0f172a",
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

const en = (count: number) => new Intl.NumberFormat("en-US").format(count);

describe("Workspace Map cluster count disc", () => {
  it("leaves documents at the visibility floor, however many members they claim, and gives a count-less cluster the base disc", () => {
    const documents = buildCosmosSceneBuffers(columnsOf(2_000, { kinds: (i) => i % 3 === 0 ? 1 : 0, sizes: () => 2.5,
      documentCounts: () => 50_000 }), tokens);
    expect(documents.drawPointSizes).toEqual(Float32Array.from(documents.pointSizes, size => Math.max(3, size)));
    expect(documents.collisionPointSizes).toBe(documents.pointSizes);
    expect(documents.countImages).toEqual([]);
    const countless = buildCosmosSceneBuffers(columnsOf(40, { kinds: (i) => i < 2 ? CLUSTER : 0, sizes: () => 8.5 }), tokens);
    expect([countless.drawPointSizes[0], countless.drawPointSizes[1]]).toEqual([18, 18]);
  });

  // `pointSizes` is what forces read. The count disc reaches only drawing and collision.
  it("keeps pointSizes bit for bit, and collision sizes equal to it except each cluster's drawn disc", () => {
    for (const [total, budget] of [[40, undefined], [2_500, WORKSPACE_MAP_PC_10K_BUDGET]] as const) {
      const input = sceneWithClusters([9_244, 747, 2], total);
      const counted = buildCosmosSceneBuffers(input, tokens, budget, { countText: en });
      const ordinary = buildCosmosSceneBuffers(withoutCounts(input), tokens, budget, { countText: en });
      expect(bytes(counted.pointSizes), `${total} nodes`).toEqual(bytes(ordinary.pointSizes));
      expect(Array.from(counted.collisionPointSizes.slice(0, 3)), `${total} nodes`).toEqual(Array.from(counted.drawPointSizes.slice(0, 3)));
      expect(bytes(counted.collisionPointSizes.slice(3)), `${total} nodes`).toEqual(bytes(counted.pointSizes.slice(3)));
    }
  });

  it("sizes the disc by its count text: an 18 px base to two digits, wider from three, one image per distinct disc", () => {
    const counts = [1, 9, 99, 100, 999, 2_048, 12_345, 100, 17_499];
    const { drawPointSizes, pointImageIndices, pointImageSizes, countImages, pointShapes, pointColors } =
      buildCosmosSceneBuffers(sceneWithClusters(counts), tokens, undefined, { countText: en });
    // Fallback measure: 7 px a digit, 3 px a separator, plus 2 px padding each side.
    expect(counts.map((_, row) => drawPointSizes[row])).toEqual([18, 18, 18, 25, 25, 35, 42, 25, 42]);
    expect(countImages.map(({ text }) => text)).toEqual(["1", "9", "99", "100", "999", "2,048", "12,345", "17,499"]);
    // Image 0 is the forced-colours Agent outline; the repeated "100" shares its image.
    expect(pointImageIndices[7]).toBe(pointImageIndices[3]);
    expect(Array.from(pointImageIndices.slice(0, 7))).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(Array.from(pointImageSizes.slice(0, 9))).toEqual(counts.map((_, row) => drawPointSizes[row]));
    // A plain grey disc in the grouping ink under its count; documents get no image.
    expect(pointShapes[0]).toBe(0);
    [100, 116, 139].forEach((channel, index) => expect(pointColors[index]).toBeCloseTo(channel / 255));
    expect(pointImageIndices[counts.length]).toBe(-1);
  });

  it("rejects a document-count column that does not describe the node rows", () => {
    const input = sceneWithClusters([40]);
    input.nodeDocumentCounts = new Uint32Array(input.nodeIds.length - 1).fill(1);
    expect(() => buildCosmosSceneBuffers(input, tokens)).toThrow("column budget or shape");
  });
});

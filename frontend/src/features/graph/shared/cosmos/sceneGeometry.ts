import type { CosmosSceneColumns } from "./sceneTypes";

function sameIndexedValues(left: ArrayLike<unknown>, right: ArrayLike<unknown>): boolean {
  if (left.length !== right.length) return false;
  for (let index = 0; index < left.length; index += 1) if (left[index] !== right[index]) return false;
  return true;
}

/** A missing column is one document per node, which is how the renderer reads it. */
function sameDocumentCounts(left: CosmosSceneColumns, right: CosmosSceneColumns): boolean {
  for (let index = 0; index < left.nodeKeys.length; index += 1) {
    if ((left.nodeDocumentCounts?.[index] ?? 1) !== (right.nodeDocumentCounts?.[index] ?? 1)) return false;
  }
  return true;
}

/**
 * Compare indexed layout rows, never labels, raw relation metadata, projection
 * IDs or grouping metadata. Callers separately fence coordinate reuse with
 * sameGraphVisibleIdentity; matching semantic fingerprints alone cannot prove
 * indexed topology is unchanged. Presentation-only edits can repaint buffers
 * without replacing the layout, pins or camera.
 */
export function sameCosmosIndexedGeometry(left: CosmosSceneColumns, right: CosmosSceneColumns): boolean {
  const fields = ["nodeKeys", "nodeKinds", "nodeSizes", "edgeSources",
    "edgeTargets", "edgeKinds", "edgeCounts", "edgeWeights"] as const;
  return fields.every((field) => sameIndexedValues(left[field], right[field])) && sameDocumentCounts(left, right);
}

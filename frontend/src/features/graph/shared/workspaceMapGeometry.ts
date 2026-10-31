import type { WorkspaceMapColumns } from "../map/sceneColumns";

function sameIndexedValues(left: ArrayLike<unknown>, right: ArrayLike<unknown>): boolean {
  if (left.length !== right.length) return false;
  for (let index = 0; index < left.length; index += 1) if (left[index] !== right[index]) return false;
  return true;
}

/** A missing column is one document per node, which is how the renderer reads it. */
function sameDocumentCounts(left: WorkspaceMapColumns, right: WorkspaceMapColumns): boolean {
  for (let index = 0; index < left.nodeKeys.length; index += 1) {
    if ((left.nodeDocumentCounts?.[index] ?? 1) !== (right.nodeDocumentCounts?.[index] ?? 1)) return false;
  }
  return true;
}

/**
 * Compare actual GPU rows, never labels, projection IDs or directory metadata.
 * Callers must separately fence reuse with sameGraphVisibleIdentity. Matching
 * semantic fingerprints alone cannot prove the indexed topology is unchanged.
 * This leaf intentionally does not import the renderer or its native SDK.
 */
export function sameWorkspaceMapIndexedGeometry(left: WorkspaceMapColumns, right: WorkspaceMapColumns): boolean {
  const fields = ["nodeKeys", "nodeKinds", "nodeSizes", "edgeSources",
    "edgeTargets", "edgeKinds", "edgeCounts", "edgeWeights"] as const;
  return fields.every((field) => sameIndexedValues(left[field], right[field])) && sameDocumentCounts(left, right);
}

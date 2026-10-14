import type { WorkspaceMapColumns } from "../map/sceneColumns";

function sameIndexedValues(left: ArrayLike<unknown>, right: ArrayLike<unknown>): boolean {
  if (left.length !== right.length) return false;
  for (let index = 0; index < left.length; index += 1) if (left[index] !== right[index]) return false;
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
  return fields.every((field) => sameIndexedValues(left[field], right[field]));
}

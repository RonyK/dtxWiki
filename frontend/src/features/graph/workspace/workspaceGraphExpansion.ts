import type { WorkspaceGraphRetainedExpansion } from "../../../shared/api/workspaceGraph";

/** One expansion that shaped the map, in replay order. */
export type WorkspaceGraphExpansionStep = {
  clusterId: string;
  label: string;
  pinned: boolean;
  offset: number;
  visible: number;
  total: number;
};

/** The server's WORKSPACE_GRAPH_RETAINED_EXPANSION_LIMIT: earlier steps replayed before a new one. */
export const RETAINED_EXPANSION_LIMIT = 7;

/**
 * `null` when the list is full. No step leaves on its own: removing one can
 * regroup every later step, so the viewer collapses one explicitly instead.
 */
export function retainedExpansions(
  steps: ReadonlyArray<Pick<WorkspaceGraphExpansionStep, "clusterId" | "pinned" | "offset">>,
): WorkspaceGraphRetainedExpansion[] | null {
  if (steps.length > RETAINED_EXPANSION_LIMIT) return null;
  // The offset replays the page a step showed, so later steps find their clusters.
  return steps.map((step) => ({ cluster_id: step.clusterId, pinned: step.pinned, offset: step.offset }));
}

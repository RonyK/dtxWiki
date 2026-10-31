import { isWorkspaceMapWithinBudget, WORKSPACE_MAP_DEFAULT_BUDGET, type WorkspaceMapBudget } from "./workspaceMapBudget";

/** Default live-physics budget; explicit PC expansion supplies its own budget. */
export const WORKSPACE_MAP_LIVE_NODE_LIMIT = WORKSPACE_MAP_DEFAULT_BUDGET.documents;

export function canAnimateWorkspaceRelationLayout(
  pointCount: number,
  budget: WorkspaceMapBudget = WORKSPACE_MAP_DEFAULT_BUDGET,
): boolean {
  return isWorkspaceMapWithinBudget({ nodeCount: pointCount, edgeCount: 0 }, budget);
}

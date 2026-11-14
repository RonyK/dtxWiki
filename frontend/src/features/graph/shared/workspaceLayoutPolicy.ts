import { isWorkspaceMapWithinBudget, WORKSPACE_MAP_DEFAULT_BUDGET, type WorkspaceMapBudget } from "./workspaceMapBudget";

export function canAnimateWorkspaceRelationLayout(
  pointCount: number,
  budget: WorkspaceMapBudget = WORKSPACE_MAP_DEFAULT_BUDGET,
): boolean {
  return isWorkspaceMapWithinBudget({ nodeCount: pointCount, edgeCount: 0 }, budget);
}

import { describe, expect, it } from "vitest";
import { canAnimateWorkspaceRelationLayout, WORKSPACE_MAP_LIVE_NODE_LIMIT } from "../workspaceLayoutPolicy";
import { WORKSPACE_MAP_PC_10K_BUDGET } from "../workspaceMapBudget";

describe("incremental workspace relation physics budget", () => {
  it("qualifies document physics through exactly the current 2K milestone", () => {
    expect(WORKSPACE_MAP_LIVE_NODE_LIMIT).toBe(2_000);
    for (const count of [0, 1, 1_001, 1_928, 2_000]) expect(canAnimateWorkspaceRelationLayout(count)).toBe(true);
    for (const count of [2_001, 10_000, 20_000, -1, 1.5, NaN, Infinity]) {
      expect(canAnimateWorkspaceRelationLayout(count)).toBe(false);
    }
  });

  it("requires the explicit PC budget to enable document physics through exactly 10K", () => {
    for (const count of [0, 1, 2_000, 2_001, 10_000]) {
      expect(canAnimateWorkspaceRelationLayout(count, WORKSPACE_MAP_PC_10K_BUDGET)).toBe(true);
    }
    for (const count of [10_001, 20_000, -1, 1.5, NaN, Infinity]) {
      expect(canAnimateWorkspaceRelationLayout(count, WORKSPACE_MAP_PC_10K_BUDGET)).toBe(false);
    }
    expect(canAnimateWorkspaceRelationLayout(2_001)).toBe(false);
  });
});

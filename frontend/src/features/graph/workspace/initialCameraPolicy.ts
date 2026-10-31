import {
  isWorkspaceMapWithinBudget, WORKSPACE_MAP_DEFAULT_BUDGET, WORKSPACE_MAP_PC_10K_BUDGET,
  type WorkspaceMapBudget,
} from "../shared/workspaceMapBudget";
import {
  CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY,
  INITIAL_WORKSPACE_FRAMING_INTERVAL_MS, INITIAL_WORKSPACE_FRAMING_WINDOW_MS,
  type InitialWorkspaceFramingPolicy, type InitialWorkspaceTerminalFitPolicy,
} from "./initialFraming";

// PC GPU framing starts its finite lifetime on the first simulation observation,
// even when an initial native transition temporarily prevents a camera claim.
const PC_WORKSPACE_FRAMING_POLICY: InitialWorkspaceFramingPolicy = Object.freeze({
  windowMs: INITIAL_WORKSPACE_FRAMING_WINDOW_MS,
  intervalMs: INITIAL_WORKSPACE_FRAMING_INTERVAL_MS,
});

// The native layout keeps contracting after the four-second window. Its alpha
// starts at .35 and halves in ~500 ticks (about 8 s at 60 Hz), where the 10K and
// 20K extents are within a few percent of final: fit once there.
const PC_WORKSPACE_TERMINAL_FIT_POLICY: InitialWorkspaceTerminalFitPolicy = Object.freeze({
  cooledAlpha: .175,
  windowMs: 30_000,
});

/** Scale admission is fixed by the screen; camera work never grants a budget. */
export function workspaceInitialCameraPolicy(nodeCount: number, budget: WorkspaceMapBudget, cpuLayout: boolean): {
  enabled: boolean;
  framingPolicy: InitialWorkspaceFramingPolicy | undefined;
  terminalFit: InitialWorkspaceTerminalFitPolicy | undefined;
} {
  return {
    enabled: isWorkspaceMapWithinBudget({ nodeCount, edgeCount: 0 }, budget)
      && nodeCount <= WORKSPACE_MAP_PC_10K_BUDGET.documents,
    // Preserve the accepted default 2K CPU and native diagnostic policies.
    framingPolicy: cpuLayout ? CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY
      : nodeCount > WORKSPACE_MAP_DEFAULT_BUDGET.documents ? PC_WORKSPACE_FRAMING_POLICY : undefined,
    // The CPU path ends with its own structure Fit; only the native PC path needs this one.
    terminalFit: !cpuLayout && nodeCount > WORKSPACE_MAP_DEFAULT_BUDGET.documents
      ? PC_WORKSPACE_TERMINAL_FIT_POLICY : undefined,
  };
}

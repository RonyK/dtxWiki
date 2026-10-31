/** A device capability profile, never a viewport breakpoint or authorization. */
export type WorkspaceMapDeviceProfile = "desktop" | "mobile" | "unknown";
export type WorkspaceMapScale = "2k" | "10k";
export type WorkspaceMapBudget = { documents: number; relations: number };

export const WORKSPACE_MAP_DEFAULT_BUDGET: Readonly<WorkspaceMapBudget> = Object.freeze({ documents: 2_000, relations: 6_000 });
export const WORKSPACE_MAP_PC_10K_BUDGET: Readonly<WorkspaceMapBudget> = Object.freeze({ documents: 10_000, relations: 100_000 });

export type WorkspaceMapDeviceSignals = {
  userAgent: string;
  platform: string;
  maxTouchPoints: number;
  mobileHint?: boolean;
  finePointer: boolean;
  hover: boolean;
};

export function classifyWorkspaceMapDevice(signals: WorkspaceMapDeviceSignals): WorkspaceMapDeviceProfile {
  // iPad desktop mode deliberately resembles macOS. Width cannot disambiguate it.
  if (signals.mobileHint === true || /Android|Mobile|iPhone|iPad|iPod/i.test(signals.userAgent)
    || (signals.platform === "MacIntel" && signals.maxTouchPoints > 1)) return "mobile";
  if (!Number.isSafeInteger(signals.maxTouchPoints) || signals.maxTouchPoints < 0) return "unknown";
  // Windows tablets and Android desktop mode can advertise a desktop UA plus
  // fine/hover input from an attached mouse. These hints cannot distinguish
  // them from touch PCs/hybrids, so all remaining touch-capable devices retain
  // the 2K limit deliberately, including otherwise genuine touch PCs.
  if (signals.maxTouchPoints > 0) return "unknown";
  return /Windows NT|Macintosh|X11/i.test(signals.userAgent) && signals.finePointer && signals.hover
    ? "desktop" : "unknown";
}

/** Read once per screen mount. Resizing/rotating does not authorize a larger query. */
export function readWorkspaceMapDeviceProfile(): WorkspaceMapDeviceProfile {
  if (typeof window === "undefined" || typeof navigator === "undefined") return "unknown";
  try {
    const agent = navigator as Navigator & { userAgentData?: { mobile?: boolean } };
    return classifyWorkspaceMapDevice({
      userAgent: agent.userAgent,
      platform: agent.platform,
      maxTouchPoints: agent.maxTouchPoints,
      mobileHint: agent.userAgentData?.mobile,
      finePointer: window.matchMedia("(pointer: fine)").matches,
      hover: window.matchMedia("(hover: hover)").matches,
    });
  } catch {
    return "unknown";
  }
}

export function workspaceMapBudget(profile: WorkspaceMapDeviceProfile, scale: WorkspaceMapScale): WorkspaceMapBudget {
  return { ...(profile === "desktop" && scale === "10k" ? WORKSPACE_MAP_PC_10K_BUDGET : WORKSPACE_MAP_DEFAULT_BUDGET) };
}

/** Count actual rendered documents and clusters, not represented member totals. */
export function isWorkspaceMapWithinBudget(
  counts: { nodeCount: number; edgeCount: number },
  budget: WorkspaceMapBudget,
): boolean {
  return Number.isSafeInteger(counts.nodeCount) && counts.nodeCount >= 0
    && Number.isSafeInteger(counts.edgeCount) && counts.edgeCount >= 0
    && Number.isSafeInteger(budget.documents) && budget.documents > 0
    && Number.isSafeInteger(budget.relations) && budget.relations > 0
    && counts.nodeCount <= budget.documents && counts.edgeCount <= budget.relations;
}

export function assertWorkspaceMapWithinBudget(
  counts: { nodeCount: number; edgeCount: number },
  budget: WorkspaceMapBudget,
): void {
  if (!isWorkspaceMapWithinBudget(counts, budget)) throw new Error("Workspace Map response exceeds its requested budget.");
}

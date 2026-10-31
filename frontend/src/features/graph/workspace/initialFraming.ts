export const INITIAL_WORKSPACE_FRAMING_WINDOW_MS = 4_000;
export const INITIAL_WORKSPACE_FRAMING_INTERVAL_MS = 200;

export interface InitialWorkspaceFramingPolicy {
  readonly windowMs: number;
  readonly intervalMs: number;
}

export const CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY: InitialWorkspaceFramingPolicy = Object.freeze({
  windowMs: 30_000,
  intervalMs: 500,
});

/** One extra Fit for a native layout that keeps contracting after the progressive window. */
export interface InitialWorkspaceTerminalFitPolicy {
  /** Native alpha at or below which the contraction that moves the extent is over. */
  readonly cooledAlpha: number;
  /** Absolute lifetime from the first observed tick; never above the 30 s framing bound. */
  readonly windowMs: number;
}

/** One scene's transient camera permission; callers own visibility and motion. */
export function createInitialWorkspaceFramingGate(eligible: boolean, policy?: InitialWorkspaceFramingPolicy): {
  claim(now: number): boolean;
  cancel(): void;
  isActive(now?: number): boolean;
} {
  const windowMs = policy?.windowMs ?? INITIAL_WORKSPACE_FRAMING_WINDOW_MS;
  const intervalMs = policy?.intervalMs ?? INITIAL_WORKSPACE_FRAMING_INTERVAL_MS;
  if (!Number.isFinite(windowMs) || windowMs <= 0 || windowMs > 30_000
    || !Number.isFinite(intervalMs) || intervalMs <= 0 || Math.ceil(windowMs / intervalMs) > 60) {
    throw new Error("Initial framing policy requires a bounded lifetime and at most 60 claims");
  }
  // The optional CPU policy starts at the first observed Worker tick, even if
  // an initial native Fit temporarily blocks a claim. Default GPU behavior
  // remains lazy at its first claim, including existing visibility contracts.
  const startOnObservation = policy !== undefined;
  let active = eligible;
  let startedAt: number | null = null;
  let lastObservedAt: number | null = null;
  let lastClaimAt: number | null = null;

  function validateClock(now: number): boolean {
    if (!active) return false;
    if (!Number.isFinite(now)) {
      // Invalid input cannot start a window. After it starts, clock failure
      // consumes permission rather than allowing an unbounded retry window.
      if (startedAt !== null) active = false;
      return false;
    }
    if (lastObservedAt !== null && now < lastObservedAt) {
      active = false;
      return false;
    }
    lastObservedAt = now;
    if (startOnObservation) startedAt ??= now;
    if (startedAt !== null) {
      const elapsed = now - startedAt;
      if (!Number.isFinite(elapsed) || elapsed >= windowMs) {
        active = false;
        return false;
      }
    }
    return true;
  }

  return {
    claim(now) {
      if (!validateClock(now)) return false;
      startedAt ??= now;
      if (lastClaimAt !== null && now - lastClaimAt < intervalMs) return false;
      lastClaimAt = now;
      return true;
    },
    cancel() { active = false; },
    isActive(now) { return now === undefined ? active : validateClock(now); },
  };
}

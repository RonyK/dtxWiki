import {
  createInitialWorkspaceFramingGate, type InitialWorkspaceFramingPolicy, type InitialWorkspaceTerminalFitPolicy,
} from "./initialFraming";

export type InitialWorkspaceCamera = ReturnType<typeof createInitialWorkspaceCamera>;

/** Own automatic camera work separately from coordinates, pins and physics. */
export function createInitialWorkspaceCamera(options: {
  eligible: boolean;
  canFrame(): boolean;
  readPositions(): Float32Array;
  applyFit(positions: Float32Array | null, enableSimulation: boolean): void;
  interruptFit(): void;
  framingPolicy?: InitialWorkspaceFramingPolicy;
  terminalFit?: InitialWorkspaceTerminalFitPolicy;
}) {
  const gate = createInitialWorkspaceFramingGate(options.eligible, options.framingPolicy);
  // One claim in one bounded lifetime of its own: the progressive window ending
  // or cooling below .02 must not spend the Fit that follows the contraction.
  const terminal = options.terminalFit;
  const terminalGate = createInitialWorkspaceFramingGate(options.eligible && terminal !== undefined,
    terminal && { windowMs: terminal.windowMs, intervalMs: terminal.windowMs });
  let frame: number | null = null;
  let terminalFrame: number | null = null;
  let nativePending: "canonical" | "progressive" | null = null;
  let interrupting = false;
  let labelPositions: Float32Array | null = null;

  function validRows(positions: Float32Array): boolean {
    return positions instanceof Float32Array && positions.length > 0
      && positions.length % 2 === 0 && positions.every(Number.isFinite);
  }

  function interruptSubmittedFit() {
    if (!nativePending) return;
    // Clear ownership before the synchronous SDK interrupt reenters Zoom.
    nativePending = null;
    labelPositions = null;
    interrupting = true;
    try { options.interruptFit(); } finally { interrupting = false; }
  }

  function fit(positions: Float32Array | null, kind: "canonical" | "progressive") {
    // SDK duration=0 Fit still queues a native D3 transition. Never stack it
    // with an older automatic Fit whose end could clear the newer owner.
    interruptSubmittedFit();
    nativePending = kind;
    labelPositions = kind === "progressive" ? positions : null;
    options.applyFit(positions, kind === "progressive");
  }

  function finishProgressive() {
    gate.cancel();
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    // Expiry must not revoke the existing canonical Worker Fit contract on
    // an explicit Resume. Only user/identity/lifecycle cancellation owns both.
    if (nativePending === "progressive") interruptSubmittedFit();
  }

  function considerTerminalFit(alpha: number, now: number) {
    // Spent, cancelled and expired permissions all read as inactive.
    if (!terminal || terminalFrame !== null || !terminalGate.isActive(now) || !(alpha <= terminal.cooledAlpha)) return;
    // Like progressive framing, never read the GPU inside the simulation tick.
    terminalFrame = requestAnimationFrame(applyTerminalFit);
  }

  function applyTerminalFit() {
    terminalFrame = null;
    // A delayed frame must not outlive the window; a later tick retries until it closes.
    if (!terminalGate.isActive(performance.now())) return;
    // canFrame excludes a pending native Fit: interrupt only this controller's own first.
    interruptSubmittedFit();
    if (!terminalGate.isActive(performance.now()) || !options.canFrame()) return;
    const positions = options.readPositions();
    // The read is the bounded GPU cost; whatever it returned spends the permission.
    if (!validRows(positions) || !terminalGate.isActive(performance.now()) || !options.canFrame()) {
      terminalGate.cancel();
      return;
    }
    // Consume before applyFit: synchronous native callbacks must not submit a duplicate.
    terminalGate.cancel();
    fit(positions, "canonical");
  }

  function cancel(interruptNative = true) {
    gate.cancel();
    terminalGate.cancel();
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    if (terminalFrame !== null) cancelAnimationFrame(terminalFrame);
    terminalFrame = null;
    if (interruptNative) interruptSubmittedFit();
    else { nativePending = null; labelPositions = null; }
  }

  return {
    /** Canonical initial/worker Fits share native cancellation ownership. */
    fitInitial(positions: Float32Array | null) { fit(positions, "canonical"); },
    /** One terminal layout Fit within the existing, never-rearmed permission. */
    finishLayout() {
      if (!gate.isActive(performance.now())) { finishProgressive(); return; }
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      // canFrame deliberately excludes a native Fit that is still pending.
      // Interrupt only this controller's prior operation before checking it;
      // an unowned user/scene camera operation is never touched here.
      interruptSubmittedFit();
      if (!gate.isActive(performance.now()) || !options.canFrame()
        || !gate.isActive(performance.now())) { finishProgressive(); return; }
      const positions = options.readPositions();
      if (!validRows(positions) || !gate.isActive(performance.now()) || !options.canFrame()
        || !gate.isActive(performance.now())) { finishProgressive(); return; }
      // Consume before applyFit: synchronous native callbacks must not submit
      // a duplicate terminal Fit. Canonical native ownership remains pending
      // until nativeEnd or user/lifecycle cancellation, not gate expiry.
      gate.cancel();
      fit(positions, "canonical");
    },
    tick(alpha: number) {
      const now = performance.now();
      considerTerminalFit(alpha, now);
      if (alpha < .02 || !gate.isActive(now)) { finishProgressive(); return; }
      if (frame !== null || nativePending || !options.canFrame() || !gate.claim(now)) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        // A delayed RAF must not extend the absolute lifetime or escape a
        // visibility/identity/motion fence checked again at application time.
        if (!gate.isActive(performance.now()) || !options.canFrame()) return;
        const positions = options.readPositions();
        if (!validRows(positions)) { finishProgressive(); return; }
        if (!gate.isActive(performance.now())) { finishProgressive(); return; }
        fit(positions, "progressive");
      });
    },
    cancel,
    /**
     * True until the terminal Fit is spent, cancelled or seen to have expired. The expiry is only seen by a tick that
     * reads the clock, so this stays true after the window has elapsed until the next tick: a camera read now may be
     * final even when this says it is not, but never the reverse.
     */
    isProvisional() { return terminalGate.isActive(); },
    isInterrupting() { return interrupting; },
    positionsForLabels() { return labelPositions; },
    nativeEnd() {
      if (interrupting) return;
      nativePending = null;
      labelPositions = null;
    },
  };
}

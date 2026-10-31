import { afterEach, describe, expect, it, vi } from "vitest";

import { createInitialWorkspaceCamera } from "../initialCamera";
import { CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY, type InitialWorkspaceFramingPolicy } from "../initialFraming";

function setup(eligible = true, framingPolicy?: InitialWorkspaceFramingPolicy) {
  let now = 0;
  let nextFrame = 1;
  const frames = new Map<number, FrameRequestCallback>();
  vi.spyOn(performance, "now").mockImplementation(() => now);
  const requestFrame = vi.fn((callback: FrameRequestCallback) => {
    const id = nextFrame++;
    frames.set(id, callback);
    return id;
  });
  const cancelFrame = vi.fn((id: number) => { frames.delete(id); });
  vi.stubGlobal("requestAnimationFrame", requestFrame);
  vi.stubGlobal("cancelAnimationFrame", cancelFrame);
  const positions = new Float32Array([10, 20, 30, 40]);
  const options = {
    eligible,
    canFrame: vi.fn(() => true),
    readPositions: vi.fn(() => positions),
    applyFit: vi.fn<(value: Float32Array | null, enableSimulation: boolean) => void>(),
    interruptFit: vi.fn<() => void>(),
    framingPolicy,
  };
  const camera = createInitialWorkspaceCamera(options);
  return {
    camera, options, positions, frames, requestFrame, cancelFrame,
    time(value: number) { now = value; },
    flush() {
      const next = frames.entries().next().value as [number, FrameRequestCallback] | undefined;
      if (!next) throw new Error("No pending camera frame");
      frames.delete(next[0]);
      next[1](now);
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("initial Workspace camera ownership", () => {
  it("claims one RAF without reading positions on the simulation tick", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.camera.tick(.4);
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
    expect(harness.options.canFrame).toHaveBeenCalledTimes(1);
    expect(harness.options.readPositions).not.toHaveBeenCalled();
    expect(harness.options.applyFit).not.toHaveBeenCalled();

    harness.flush();
    expect(harness.frames.size).toBe(0);
    expect(harness.options.readPositions).toHaveBeenCalledTimes(1);
    expect(harness.options.applyFit).toHaveBeenCalledWith(harness.positions, true);
  });

  it("keeps the claim cadence at 200 ms even after native Fit ends immediately", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.flush();
    harness.camera.nativeEnd();
    harness.time(199.999);
    harness.camera.tick(.5);
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
    harness.time(200);
    harness.camera.tick(.5);
    harness.flush();
    expect(harness.requestFrame).toHaveBeenCalledTimes(2);
    expect(harness.options.applyFit).toHaveBeenCalledTimes(2);
  });

  it("permits at most twenty progressive Fits in the four-second window", () => {
    const harness = setup();
    for (let now = 0; now <= 4_000; now += 10) {
      harness.time(now);
      harness.camera.tick(.5);
      if (harness.frames.size) {
        harness.flush();
        harness.camera.nativeEnd();
      }
    }
    expect(harness.options.applyFit).toHaveBeenCalledTimes(20);
    harness.time(10_000);
    harness.camera.tick(.5);
    expect(harness.requestFrame).toHaveBeenCalledTimes(20);
  });

  it("does not begin the lifetime while framing permission is unavailable", () => {
    const harness = setup();
    harness.options.canFrame.mockReturnValue(false);
    harness.camera.tick(.5);
    harness.time(100_000);
    harness.options.canFrame.mockReturnValue(true);
    harness.camera.tick(.5);
    harness.flush();
    expect(harness.options.applyFit).toHaveBeenCalledTimes(1);
    harness.camera.nativeEnd();
    harness.time(103_999);
    harness.camera.tick(.5);
    expect(harness.frames.size).toBe(1);
  });

  it("rechecks the exclusive deadline before a delayed RAF reads positions", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.flush();
    harness.camera.nativeEnd();
    harness.time(3_999);
    harness.camera.tick(.5);
    harness.time(4_000);
    harness.flush();
    expect(harness.options.readPositions).toHaveBeenCalledTimes(1);
    expect(harness.options.applyFit).toHaveBeenCalledTimes(1);
    harness.time(4_200);
    harness.camera.tick(.5);
    expect(harness.frames.size).toBe(0);
  });

  it("rechecks the deadline after the position read consumes the remaining budget", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.options.readPositions.mockImplementation(() => {
      harness.time(4_000);
      return harness.positions;
    });
    harness.flush();
    expect(harness.options.readPositions).toHaveBeenCalledTimes(1);
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    harness.time(4_200);
    harness.camera.tick(.5);
    expect(harness.frames.size).toBe(0);
  });

  it("rechecks visibility and identity permission before readback", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.options.canFrame.mockReturnValue(false);
    harness.flush();
    expect(harness.options.readPositions).not.toHaveBeenCalled();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    harness.options.canFrame.mockReturnValue(true);
    harness.time(200);
    harness.camera.tick(.5);
    harness.flush();
    expect(harness.options.applyFit).toHaveBeenCalledTimes(1);
  });

  it("does not stack progressive Fits while an owned native Fit remains pending", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.flush();
    for (const now of [200, 400, 600]) {
      harness.time(now);
      harness.camera.tick(.5);
    }
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
    expect(harness.options.applyFit).toHaveBeenCalledTimes(1);
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    harness.camera.nativeEnd();
    harness.camera.tick(.5);
    expect(harness.requestFrame).toHaveBeenCalledTimes(2);
  });

  it("clears a pending RAF before its callback reenters cancellation", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.options.canFrame.mockImplementation(() => {
      harness.camera.cancel();
      return false;
    });
    harness.flush();
    expect(harness.cancelFrame).not.toHaveBeenCalled();
    expect(harness.options.readPositions).not.toHaveBeenCalled();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    harness.time(200);
    harness.camera.tick(.5);
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
  });

  it("does not fit if readback reentrantly consumes permission", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.options.readPositions.mockImplementation(() => {
      harness.camera.cancel();
      return harness.positions;
    });
    harness.flush();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    harness.time(200);
    harness.camera.tick(.5);
    expect(harness.frames.size).toBe(0);
  });

  it("cancels queued RAF work permanently without interrupting an unsubmitted Fit", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.camera.cancel();
    expect(harness.cancelFrame).toHaveBeenCalledWith(1);
    expect(harness.frames.size).toBe(0);
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    harness.time(200);
    harness.camera.tick(.5);
    harness.time(10_000);
    harness.camera.tick(.5);
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
  });

  it("interrupts only the controller's submitted Fit and only once", () => {
    const harness = setup();
    harness.camera.cancel();
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    harness.camera.fitInitial(null);
    harness.camera.cancel();
    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
    expect(harness.camera.positionsForLabels()).toBeNull();
    harness.camera.nativeEnd();
    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
  });

  it("does not interrupt a Fit whose native end has already released ownership", () => {
    const harness = setup();
    harness.camera.fitInitial(harness.positions);
    harness.camera.nativeEnd();
    harness.camera.cancel();
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
  });

  it("clears ownership and labels before synchronous SDK interruption reenters cancellation", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.flush();
    harness.options.interruptFit.mockImplementation(() => {
      expect(harness.camera.positionsForLabels()).toBeNull();
      expect(harness.camera.isInterrupting()).toBe(true);
      harness.camera.cancel();
      harness.camera.nativeEnd();
      expect(harness.camera.isInterrupting()).toBe(true);
    });
    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
    expect(harness.camera.isInterrupting()).toBe(false);
  });

  it("suppresses interrupted end callbacks without falsely releasing the replacement Fit", () => {
    const harness = setup();
    const next = new Float32Array([50, 60, 70, 80]);
    harness.camera.fitInitial(null);
    harness.options.interruptFit.mockImplementation(() => {
      expect(harness.camera.isInterrupting()).toBe(true);
      harness.camera.nativeEnd();
      expect(harness.camera.isInterrupting()).toBe(true);
    });
    harness.camera.fitInitial(next);
    expect(harness.options.applyFit.mock.calls).toEqual([[null, false], [next, false]]);
    expect(harness.camera.isInterrupting()).toBe(false);
    harness.camera.tick(.5);
    expect(harness.requestFrame).not.toHaveBeenCalled();
    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(2);
  });

  it("ends interruption suppression even when the SDK interrupt throws", () => {
    const harness = setup();
    harness.camera.fitInitial(null);
    harness.options.interruptFit.mockImplementation(() => {
      expect(harness.camera.isInterrupting()).toBe(true);
      throw new Error("SDK interrupt failed");
    });
    expect(() => harness.camera.cancel()).toThrow("SDK interrupt failed");
    expect(harness.camera.isInterrupting()).toBe(false);
    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
  });

  it.each([null, new Float32Array([1, 2, 3, 4])])("owns canonical initial Fit %s even when progressive permission is disabled", (positions) => {
    const harness = setup(false);
    harness.camera.fitInitial(positions);
    expect(harness.options.applyFit).toHaveBeenCalledWith(positions, false);
    expect(harness.camera.positionsForLabels()).toBeNull();
    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
  });

  it.each(["disabled", "expired", "cooldown"] as const)("preserves a pending canonical Fit when the progressive gate is %s", (state) => {
    const harness = setup(state !== "disabled");
    if (state === "expired") {
      harness.camera.tick(.5);
      harness.flush();
      harness.camera.nativeEnd();
      harness.time(4_000);
    }
    harness.camera.fitInitial(harness.positions);
    const submittedFits = harness.options.applyFit.mock.calls.length;
    const requestedFrames = harness.requestFrame.mock.calls.length;
    harness.camera.tick(state === "cooldown" ? .019 : .5);
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    expect(harness.options.applyFit).toHaveBeenLastCalledWith(harness.positions, false);

    // Reheating/Resume cannot reopen progressive framing, but it must not
    // discard the canonical initial/worker camera operation already queued.
    harness.time(10_000);
    harness.camera.tick(1);
    expect(harness.options.applyFit).toHaveBeenCalledTimes(submittedFits);
    expect(harness.requestFrame).toHaveBeenCalledTimes(requestedFrames);
    expect(harness.options.interruptFit).not.toHaveBeenCalled();

    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
  });

  it("does not interrupt a canonical Fit when an older progressive RAF expires", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.camera.fitInitial(harness.positions);
    harness.time(4_000);
    harness.flush();
    expect(harness.options.readPositions).not.toHaveBeenCalled();
    expect(harness.options.applyFit.mock.calls).toEqual([[harness.positions, false]]);
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    harness.camera.tick(.5);
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
  });

  it("reuses one position read for labels until its native Fit ends", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.flush();
    for (let index = 0; index < 5; index++) {
      expect(harness.camera.positionsForLabels()).toBe(harness.positions);
    }
    harness.time(200);
    harness.camera.tick(.5);
    expect(harness.options.readPositions).toHaveBeenCalledTimes(1);
    harness.camera.nativeEnd();
    expect(harness.camera.positionsForLabels()).toBeNull();
    harness.camera.tick(.5);
    harness.flush();
    expect(harness.options.readPositions).toHaveBeenCalledTimes(2);
  });

  it("releases reused label positions when a canonical worker Fit replaces a progressive Fit", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.flush();
    const next = new Float32Array([50, 60]);
    harness.camera.fitInitial(next);
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
    expect(harness.camera.positionsForLabels()).toBeNull();
    expect(harness.options.applyFit.mock.calls).toEqual([[harness.positions, true], [next, false]]);
  });

  it("drops destroyed native ownership without making an SDK interruption call", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.flush();
    harness.camera.cancel(false);
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    expect(harness.camera.positionsForLabels()).toBeNull();
    harness.time(200);
    harness.camera.tick(.5);
    harness.camera.nativeEnd();
    harness.camera.cancel();
    expect(harness.options.applyFit).toHaveBeenCalledTimes(1);
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
  });

  it("does not execute a stale RAF after destroy-style cancellation", () => {
    const harness = setup();
    harness.camera.tick(.5);
    const staleCallback = harness.frames.get(1)!;
    harness.camera.cancel(false);
    staleCallback(0);
    expect(harness.options.canFrame).toHaveBeenCalledTimes(1);
    expect(harness.options.readPositions).not.toHaveBeenCalled();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
  });

  it.each([
    new Float32Array(),
    new Float32Array([1, NaN]),
    new Float32Array([Infinity, 1]),
    new Float32Array([1, -Infinity]),
  ])("fails closed on empty or nonfinite native positions %s", (positions) => {
    const harness = setup();
    harness.options.readPositions.mockReturnValue(positions);
    harness.camera.tick(.5);
    harness.flush();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    harness.options.readPositions.mockReturnValue(harness.positions);
    harness.time(200);
    harness.camera.tick(.5);
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
  });

  it("consumes cooldown before the first claim and cannot rearm on reheating", () => {
    const harness = setup();
    harness.camera.tick(.019);
    harness.time(100_000);
    harness.camera.tick(1);
    expect(harness.requestFrame).not.toHaveBeenCalled();
    expect(harness.options.readPositions).not.toHaveBeenCalled();
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
  });

  it("allows alpha .02 but cancels submitted Fit when it cools below the threshold", () => {
    const harness = setup();
    harness.camera.tick(.02);
    harness.flush();
    harness.time(100);
    harness.camera.tick(.019999);
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
    expect(harness.camera.positionsForLabels()).toBeNull();
    harness.time(200);
    harness.camera.tick(1);
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
  });

  it("cancels pending RAF on cooldown without reading or submitting a native Fit", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.camera.tick(.01);
    expect(harness.cancelFrame).toHaveBeenCalledWith(1);
    expect(harness.frames.size).toBe(0);
    expect(harness.options.readPositions).not.toHaveBeenCalled();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
  });

  it("interrupts a submitted Fit at the deadline even while native completion is pending", () => {
    const harness = setup();
    harness.camera.tick(.5);
    harness.flush();
    harness.time(4_000);
    harness.camera.tick(.5);
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(1);
    expect(harness.camera.positionsForLabels()).toBeNull();
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
  });

  it("keeps CPU framing bounded to sixty progressive fits at 500-ms cadence, with no Fit at forty seconds", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    for (let now = 0; now <= 40_000; now += 10) {
      harness.time(now); harness.camera.tick(1);
      if (harness.frames.size) { harness.flush(); harness.camera.nativeEnd(); }
    }
    expect(harness.options.applyFit).toHaveBeenCalledTimes(60);
    expect(harness.options.applyFit.mock.calls.every(([, simulation]) => simulation)).toBe(true);
    harness.camera.finishLayout();
    expect(harness.options.applyFit).toHaveBeenCalledTimes(60);
    expect(harness.requestFrame).toHaveBeenCalledTimes(60);
  });

  it("starts the CPU deadline behind a still-pending native initial Fit", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.fitInitial(harness.positions);
    harness.camera.tick(1);
    harness.time(40_000);
    harness.camera.nativeEnd();
    harness.camera.tick(1);
    harness.camera.finishLayout();
    expect(harness.options.applyFit.mock.calls).toEqual([[harness.positions, false]]);
    expect(harness.requestFrame).not.toHaveBeenCalled();
  });

  it("allows a final canonical Fit at 12 seconds and consumes the gate without rearming on Resume", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1); harness.flush(); harness.camera.nativeEnd();
    harness.time(12_000);
    harness.camera.finishLayout();
    expect(harness.options.applyFit.mock.calls).toEqual([[harness.positions, true], [harness.positions, false]]);
    harness.camera.finishLayout();
    harness.camera.tick(1);
    harness.camera.nativeEnd();
    harness.time(20_000); harness.camera.tick(1); harness.camera.finishLayout();
    harness.time(40_000); harness.camera.tick(1); harness.camera.finishLayout();
    expect(harness.options.applyFit).toHaveBeenCalledTimes(2);
    expect(harness.requestFrame).toHaveBeenCalledTimes(1);
  });

  it("cancels an eligible queued progressive RAF before submitting the terminal canonical Fit", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1);
    const lateFrame = [...harness.frames.values()][0];
    harness.camera.finishLayout();
    expect(harness.cancelFrame).toHaveBeenCalledWith(1);
    expect(harness.frames.size).toBe(0);
    expect(harness.options.applyFit).toHaveBeenCalledExactlyOnceWith(harness.positions, false);
    lateFrame(0);
    expect(harness.options.applyFit).toHaveBeenCalledOnce();
    expect(harness.options.readPositions).toHaveBeenCalledOnce();
  });

  it("interrupts only its owned pending Fit before asking canFrame about the final Fit", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    let fitReferencePending = false;
    harness.options.applyFit.mockImplementation(() => { fitReferencePending = true; });
    harness.options.interruptFit.mockImplementation(() => { fitReferencePending = false; });
    harness.options.canFrame.mockImplementation(() => !fitReferencePending);
    harness.camera.fitInitial(harness.positions);
    harness.camera.tick(1);
    harness.time(12_000);
    harness.camera.finishLayout();
    expect(harness.options.interruptFit).toHaveBeenCalledOnce();
    expect(harness.options.applyFit.mock.calls).toEqual([[harness.positions, false], [harness.positions, false]]);
    expect(fitReferencePending).toBe(true);
    harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledTimes(2);
  });

  it("preserves final canonical native ownership after consumed-gate ticks and expires only on user cancellation", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1);
    harness.camera.finishLayout();
    harness.time(40_000);
    harness.camera.tick(1); harness.camera.finishLayout();
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    harness.camera.cancel(); harness.camera.cancel();
    expect(harness.options.interruptFit).toHaveBeenCalledOnce();
  });

  it.each(["user", "Pause", "hidden", "scene"])("prevents progressive and final Fits after %s cancellation", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1); harness.camera.cancel();
    harness.camera.finishLayout();
    harness.time(500); harness.camera.tick(1);
    harness.time(40_000); harness.camera.tick(1); harness.camera.finishLayout();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    expect(harness.options.readPositions).not.toHaveBeenCalled();
  });

  it("never interrupts an unowned user Fit when terminal framing is unavailable", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1);
    harness.options.canFrame.mockReturnValue(false);
    harness.camera.finishLayout();
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
    expect(harness.options.readPositions).not.toHaveBeenCalled();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    harness.options.canFrame.mockReturnValue(true);
    harness.camera.finishLayout();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
  });

  it("does not submit a final Fit when its owned native interruption reentrantly cancels permission", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.fitInitial(harness.positions);
    harness.camera.tick(1);
    harness.options.interruptFit.mockImplementation(() => harness.camera.cancel());
    harness.camera.finishLayout();
    expect(harness.options.applyFit).toHaveBeenCalledOnce();
    expect(harness.options.interruptFit).toHaveBeenCalledOnce();
    expect(harness.options.readPositions).not.toHaveBeenCalled();
  });

  it("rechecks terminal cancellation even when canFrame reentrantly cancels and returns true", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1);
    harness.options.canFrame.mockImplementation(() => { harness.camera.cancel(); return true; });
    harness.camera.finishLayout();
    expect(harness.options.readPositions).not.toHaveBeenCalled();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
  });

  it("rechecks terminal cancellation after the positions read", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1);
    harness.options.readPositions.mockImplementation(() => { harness.camera.cancel(); return harness.positions; });
    harness.camera.finishLayout();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
  });

  it("consumes the gate before synchronous Fit callbacks can request another final Fit", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1);
    harness.options.applyFit.mockImplementation(() => harness.camera.finishLayout());
    harness.camera.finishLayout();
    expect(harness.options.applyFit).toHaveBeenCalledExactlyOnceWith(harness.positions, false);
    expect(harness.options.interruptFit).not.toHaveBeenCalled();
  });

  it.each([30_000, 40_000, NaN, Infinity, -Infinity, -1])("fails closed on terminal deadline/clock fence %s", clock => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1);
    harness.time(clock); harness.camera.finishLayout();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    harness.time(500); harness.camera.finishLayout();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
  });

  it("rechecks the terminal deadline when reading positions consumes the remaining budget", () => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1);
    harness.time(29_999);
    harness.options.readPositions.mockImplementation(() => { harness.time(30_000); return harness.positions; });
    harness.camera.finishLayout();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
  });

  it.each([
    new Float32Array(), new Float32Array([1]), new Float32Array([1, NaN]), new Float32Array([Infinity, 1]),
  ])("consumes terminal permission without fitting malformed rows %s", positions => {
    const harness = setup(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    harness.camera.tick(1);
    harness.options.readPositions.mockReturnValue(positions);
    harness.camera.finishLayout();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
    harness.options.readPositions.mockReturnValue(harness.positions);
    harness.camera.finishLayout();
    expect(harness.options.applyFit).not.toHaveBeenCalled();
  });
});

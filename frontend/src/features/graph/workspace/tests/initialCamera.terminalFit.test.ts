import { afterEach, describe, expect, it, vi } from "vitest";

import { createInitialWorkspaceCamera } from "../initialCamera";
import type { InitialWorkspaceFramingPolicy, InitialWorkspaceTerminalFitPolicy } from "../initialFraming";

// The admitted PC shape: a four-second progressive window, then one Fit once
// the native layout has cooled. The native alpha starts at .35, halves in ~500
// ticks (about 8 s at 60 Hz) and the layout extent stops moving with it.
const PROGRESSIVE: InitialWorkspaceFramingPolicy = { windowMs: 4_000, intervalMs: 200 };
const TERMINAL: InitialWorkspaceTerminalFitPolicy = { cooledAlpha: .175, windowMs: 30_000 };

function setup(overrides: { eligible?: boolean; terminalFit?: InitialWorkspaceTerminalFitPolicy | undefined } = {}) {
  const { eligible = true } = overrides;
  const terminalFit = "terminalFit" in overrides ? overrides.terminalFit : TERMINAL;
  let now = 0;
  let nextFrame = 1;
  const frames = new Map<number, FrameRequestCallback>();
  vi.spyOn(performance, "now").mockImplementation(() => now);
  const requestFrame = vi.fn((callback: FrameRequestCallback) => { const id = nextFrame++; frames.set(id, callback); return id; });
  const cancelFrame = vi.fn((id: number) => { frames.delete(id); });
  vi.stubGlobal("requestAnimationFrame", requestFrame);
  vi.stubGlobal("cancelAnimationFrame", cancelFrame);
  const positions = new Float32Array([10, 20, 30, 40]);
  // Like the adapter, a submitted native Fit blocks every new claim until it ends.
  let nativeFitPending = false;
  const options = {
    eligible,
    canFrame: vi.fn(() => !nativeFitPending),
    readPositions: vi.fn(() => positions),
    applyFit: vi.fn<(value: Float32Array | null, enableSimulation: boolean) => void>(() => { nativeFitPending = true; }),
    interruptFit: vi.fn<() => void>(() => { nativeFitPending = false; }),
    framingPolicy: PROGRESSIVE,
    terminalFit,
  };
  const camera = createInitialWorkspaceCamera(options);
  const harness = {
    camera, options, positions, frames, requestFrame, cancelFrame,
    time(value: number) { now = value; },
    flush() {
      const next = frames.entries().next().value as [number, FrameRequestCallback] | undefined;
      if (!next) throw new Error("No pending camera frame");
      frames.delete(next[0]);
      next[1](now);
    },
    /** The native Fit finishes (D3 transition end), as the adapter's ZoomEnd does. */
    nativeEnd() { nativeFitPending = false; camera.nativeEnd(); },
    /** Run the progressive window as the adapter does: a tick per frame, a Fit per claim. */
    runProgressiveWindow() {
      for (let at = 0; at <= 4_000; at += 10) {
        now = at;
        camera.tick(.35 - at * 1e-5);
        if (frames.size) { harness.flush(); harness.nativeEnd(); }
      }
    },
    fitCalls: () => options.applyFit.mock.calls.length,
  };
  return harness;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("one terminal Fit for a cooled native layout", () => {
  it("fits once from one fresh position read after the progressive window, when alpha reaches the cooled threshold", () => {
    const h = setup();
    h.runProgressiveWindow();
    const progressive = h.fitCalls(), reads = h.options.readPositions.mock.calls.length;
    // The progressive window ending and a hot layout do not spend or revoke the permission.
    h.time(4_200); h.camera.tick(.34);
    h.time(6_000); h.camera.tick(.26);
    h.time(8_000); h.camera.tick(.176);
    expect(h.frames.size).toBe(0);
    expect(h.options.readPositions).toHaveBeenCalledTimes(reads);

    h.time(8_100); h.camera.tick(.175);
    expect(h.options.readPositions).toHaveBeenCalledTimes(reads); // never read inside the simulation tick
    h.flush();
    expect(h.options.readPositions).toHaveBeenCalledTimes(reads + 1);
    expect(h.fitCalls()).toBe(progressive + 1);
    expect(h.options.applyFit).toHaveBeenLastCalledWith(h.positions, false);

    // Once: cooler ticks, including the progressive 0.02 cooldown, queue nothing.
    h.nativeEnd();
    for (const [at, alpha] of [[8_200, .17], [20_000, .019], [29_000, .001]] as const) {
      h.time(at); h.camera.tick(alpha);
      expect(h.frames.size).toBe(0);
    }
    expect(h.fitCalls()).toBe(progressive + 1);
  });

  it("lives thirty seconds from the first observed tick, not from construction, and a delayed frame cannot outlive it", () => {
    const early = setup();
    early.time(100_000); early.camera.tick(.34);
    early.flush(); early.nativeEnd();
    early.time(107_000); early.camera.tick(.175);
    early.flush();
    expect(early.options.applyFit.mock.calls.at(-1)).toEqual([early.positions, false]);

    const late = setup();
    late.time(100_000); late.camera.tick(.34);
    late.flush(); late.nativeEnd();
    late.time(130_000); late.camera.tick(.1);
    expect(late.frames.size).toBe(0);
    expect(late.fitCalls()).toBe(1);

    const delayed = setup();
    delayed.runProgressiveWindow();
    const reads = delayed.options.readPositions.mock.calls.length, fits = delayed.fitCalls();
    delayed.time(29_999); delayed.camera.tick(.1);
    delayed.time(30_000); delayed.flush();
    expect(delayed.options.readPositions).toHaveBeenCalledTimes(reads);
    expect(delayed.fitCalls()).toBe(fits);
  });

  it("never fits once the camera was taken, whether before or after the cooled tick", () => {
    const early = setup();
    early.runProgressiveWindow();
    const fits = early.fitCalls();
    early.camera.cancel();
    early.time(9_000); early.camera.tick(.1);
    expect(early.frames.size).toBe(0);
    expect(early.fitCalls()).toBe(fits);

    const late = setup();
    late.runProgressiveWindow();
    late.time(9_000); late.camera.tick(.1);
    const queued = [...late.frames.values()][0];
    const reads = late.options.readPositions.mock.calls.length;
    late.camera.cancel();
    expect(late.cancelFrame).toHaveBeenCalled();
    queued(9_000); // a frame that was already running when the camera was taken
    expect(late.options.readPositions).toHaveBeenCalledTimes(reads);
    expect(late.fitCalls()).toBe(fits);
  });

  it("waits without consuming while a native Fit blocks framing, then fits at a later tick", () => {
    const h = setup();
    h.runProgressiveWindow();
    const progressive = h.fitCalls(), reads = h.options.readPositions.mock.calls.length;
    h.options.canFrame.mockReturnValue(false);
    h.time(9_000); h.camera.tick(.1); h.flush();
    expect(h.options.readPositions).toHaveBeenCalledTimes(reads);
    expect(h.fitCalls()).toBe(progressive);
    h.options.canFrame.mockReturnValue(true);
    h.time(9_016); h.camera.tick(.099);
    h.flush();
    expect(h.fitCalls()).toBe(progressive + 1);
  });

  it("fails closed on malformed GPU rows: no Fit and no second read", () => {
    const h = setup();
    h.runProgressiveWindow();
    const progressive = h.fitCalls(), reads = h.options.readPositions.mock.calls.length;
    h.options.readPositions.mockReturnValue(new Float32Array([1, NaN]));
    h.time(9_000); h.camera.tick(.1); h.flush();
    h.options.readPositions.mockReturnValue(h.positions);
    h.time(9_016); h.camera.tick(.09);
    expect(h.frames.size).toBe(0);
    expect(h.options.readPositions).toHaveBeenCalledTimes(reads + 1);
    expect(h.fitCalls()).toBe(progressive);
  });

  it("does not exist, and never makes the camera provisional, without a terminal policy or for an ineligible camera", () => {
    for (const h of [setup({ terminalFit: undefined }), setup({ eligible: false })]) {
      expect(h.camera.isProvisional()).toBe(false);
      h.runProgressiveWindow();
      const fits = h.fitCalls();
      h.time(9_000); h.camera.tick(.1);
      expect(h.frames.size).toBe(0);
      expect(h.fitCalls()).toBe(fits);
      expect(h.camera.isProvisional()).toBe(false);
    }
  });

  it("is provisional from creation until the terminal Fit is applied, the camera is taken, or its window closes", () => {
    const h = setup();
    expect(h.camera.isProvisional()).toBe(true);
    h.runProgressiveWindow();
    h.time(8_100); h.camera.tick(.175);
    expect(h.camera.isProvisional()).toBe(true); // queued, not yet applied
    h.flush();
    expect(h.camera.isProvisional()).toBe(false);

    const taken = setup();
    taken.runProgressiveWindow();
    taken.camera.cancel();
    expect(taken.camera.isProvisional()).toBe(false);

    // A layout that never cools (a slow frame rate) must not keep its camera unreportable for good.
    const expired = setup();
    expired.runProgressiveWindow();
    expired.time(30_000); expired.camera.tick(.3);
    expect(expired.camera.isProvisional()).toBe(false);
  });
});

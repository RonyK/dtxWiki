import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceTrackedPositionsFrame } from "../gpuTrackedPositions";
import { createWorkspaceTrackedPositions } from "../gpuTrackedPositions";
import { createWorkspaceGpuReadback } from "../gpuReadback";

vi.mock("../gpuReadback", () => ({ createWorkspaceGpuReadback: vi.fn() }));

function harness() {
  let pending = false;
  const readback = {
    request: vi.fn(() => { pending = true; return true; }),
    poll: vi.fn<() => Float32Array | null>(() => null),
    pending: vi.fn(() => pending),
    readSync: vi.fn(), discard: vi.fn(() => { pending = false; }), destroy: vi.fn(),
  };
  vi.mocked(createWorkspaceGpuReadback).mockReturnValue(readback);
  const gl = { isContextLost: vi.fn(() => false) } as unknown as WebGL2RenderingContext;
  let input: WorkspaceTrackedPositionsFrame | null = { gl, framebuffer: {}, width: 2, height: 2, indices: [7, 2] };
  const getFrame = vi.fn(() => input), onPositions = vi.fn(), onError = vi.fn();
  const controller = createWorkspaceTrackedPositions({ getFrame, onPositions, onError });
  return { readback, controller, getFrame, onPositions, onError, gl,
    setFrame(frame: WorkspaceTrackedPositionsFrame | null) { input = frame; },
    frame: input,
    complete(rows = Float32Array.from([10, 20, 1, 1, 30, 40, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0])) {
      pending = false; readback.poll.mockReturnValueOnce(rows);
    } };
}

let nextFrame = 0;
const frames = new Map<number, FrameRequestCallback>();
function tick() {
  const callbacks = [...frames.values()]; frames.clear();
  callbacks.forEach(callback => callback(0));
}
beforeEach(() => {
  frames.clear(); nextFrame = 0; vi.clearAllMocks();
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
    const id = ++nextFrame; frames.set(id, callback); return id;
  }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => { frames.delete(id); }));
});
afterEach(() => vi.unstubAllGlobals());

describe("bounded asynchronous native label positions", () => {
  it("requests one bounded snapshot and polls its fence without another GPU tracking query", () => {
    const h = harness();
    expect(h.controller.request()).toBe(true);
    expect(createWorkspaceGpuReadback).toHaveBeenCalledWith(h.gl, 64);
    expect(h.readback.request).toHaveBeenCalledWith(h.frame!.framebuffer, 2, 2);
    expect(h.controller.request()).toBe(false);
    tick(); tick(); expect(h.getFrame).toHaveBeenCalledTimes(1);
    expect(h.readback.request).toHaveBeenCalledTimes(1);
    expect(h.onPositions).not.toHaveBeenCalled();
    h.complete(); tick();
    expect(h.onPositions).toHaveBeenCalledExactlyOnceWith(new Map([[7, [10, 20]], [2, [30, 40]]]));
    expect(frames.size).toBe(0); expect(h.readback.readSync).not.toHaveBeenCalled();
    h.controller.destroy();
  });

  it("maps the issued index snapshot, not a subsequently mutated caller array", () => {
    const h = harness(), indices = [7, 2]; h.setFrame({ ...h.frame!, indices });
    h.controller.request(); indices.reverse(); h.complete(); tick();
    expect(h.onPositions.mock.calls[0][0].get(7)).toEqual([10, 20]); h.controller.destroy();
  });

  it("fences callbacks retained after invalidation and allows a fresh paused snapshot", () => {
    const h = harness(); h.controller.request(); const obsolete = [...frames.values()][0];
    h.controller.invalidate(); h.complete(); obsolete(0);
    expect(h.onPositions).not.toHaveBeenCalled(); expect(frames.size).toBe(0);
    h.controller.request(); h.complete(); tick();
    expect(h.onPositions).toHaveBeenCalledTimes(1); h.controller.destroy();
  });

  it("an obsolete callback cannot erase a newer request's RAF cancellation handle", () => {
    const h = harness(); h.controller.request(); const obsolete = [...frames.values()][0];
    h.controller.invalidate(); expect(h.controller.request()).toBe(true);
    const currentHandle = [...frames.keys()][0];
    obsolete(0);
    h.controller.invalidate();
    expect(cancelAnimationFrame).toHaveBeenLastCalledWith(currentHandle);
    expect(frames.size).toBe(0); expect(h.readback.poll).not.toHaveBeenCalled();
    expect(h.onPositions).not.toHaveBeenCalled(); h.controller.destroy();
  });

  it("releases a retired GL owner before using a different context", () => {
    const h = harness(); h.controller.request(); h.complete(); tick();
    const nextGl = { isContextLost: () => false } as unknown as WebGL2RenderingContext;
    h.setFrame({ ...h.frame!, gl: nextGl }); h.controller.request();
    expect(h.readback.destroy).toHaveBeenCalledTimes(1);
    expect(createWorkspaceGpuReadback).toHaveBeenLastCalledWith(nextGl, 64); h.controller.destroy();
  });

  it("skips nonfinite rows and padding rather than creating incorrect node positions", () => {
    const h = harness(); h.controller.request();
    h.complete(Float32Array.from([NaN, 20, 1, 1, 30, 40, 1, 1, 5, 6, 1, 1, 7, 8, 1, 1])); tick();
    expect(h.onPositions).toHaveBeenCalledWith(new Map([[2, [30, 40]]])); h.controller.destroy();
  });

  it.each([
    { indices: Array.from({ length: 41 }, (_, index) => index), width: 7, height: 7 },
    { indices: [1, 1] }, { indices: [-1] }, { indices: [NaN] },
    { width: 9, height: 8 }, { width: 1, height: 1 }, { width: 1.5 },
  ])("fails once on an invalid snapshot: %j", invalid => {
    const h = harness(); h.setFrame({ ...h.frame!, ...invalid });
    expect(h.controller.request()).toBe(false); expect(h.onError).toHaveBeenCalledTimes(1);
    h.controller.invalidate(); h.controller.request(); expect(h.onError).toHaveBeenCalledTimes(1);
    expect(createWorkspaceGpuReadback).not.toHaveBeenCalled(); h.controller.destroy();
  });

  it("does not spin on a lost context or absent tracking attachment", () => {
    const h = harness(); vi.mocked(h.gl.isContextLost).mockReturnValue(true);
    expect(h.controller.request()).toBe(false); expect(frames.size).toBe(0);
    h.setFrame(null); expect(h.controller.request()).toBe(false);
    expect(h.onError).not.toHaveBeenCalled(); h.controller.destroy();
  });

  it("stops after a failed fence and after destruction, without publishing stale titles", () => {
    const h = harness(); h.controller.request(); h.readback.poll.mockImplementationOnce(() => { throw new Error("GPU fence"); });
    tick(); expect(h.onError).toHaveBeenCalledTimes(1); expect(h.readback.destroy).toHaveBeenCalledTimes(1);
    expect(h.controller.request()).toBe(false); h.controller.destroy(); h.controller.destroy();
    expect(h.onPositions).not.toHaveBeenCalled(); expect(frames.size).toBe(0);
  });

  it("cancels an outstanding RAF and discards/deletes its owned readback exactly once", () => {
    const h = harness(); h.controller.request(); const obsolete = [...frames.values()][0];
    h.controller.destroy(); h.controller.destroy(); h.complete(); obsolete(0);
    expect(h.readback.destroy).toHaveBeenCalledTimes(1); expect(h.onPositions).not.toHaveBeenCalled();
    expect(h.controller.request()).toBe(false); expect(frames.size).toBe(0);
  });
});

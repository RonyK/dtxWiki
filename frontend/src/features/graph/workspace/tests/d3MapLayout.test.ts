import { describe, expect, it, vi } from "vitest";
import type { D3WorkerLayoutOptions } from "../../shared/physics/d3WorkerLayout";
import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import { createWorkspaceD3Layout } from "../d3MapLayout";
import { buildWorkspaceD3UnitBasis } from "../d3MapUnits";

const mock = vi.hoisted(() => ({
  options: null as D3WorkerLayoutOptions | null,
  driver: { destroy: vi.fn(), resume: vi.fn(), pause: vi.fn(), update: vi.fn(),
    setPositions: vi.fn(), setReducedMotion: vi.fn(), isRunning: () => false },
}));
vi.mock("../../shared/physics/d3WorkerLayout", () => ({
  createD3WorkerLayout: (_input: unknown, options: D3WorkerLayoutOptions) => {
    mock.options = options;
    return mock.driver;
  },
}));

describe("Map D3 consumer failure boundary", () => {
  it("retires the Worker and notifies the host when a coordinate GPU-upload consumer throws", () => {
    const positions = Float32Array.from([0, 0, 80, 0]);
    const diameters = Float32Array.from([6, 6]);
    const shapes = Float32Array.from([0, 0]);
    const viewport = { width: 1000, height: 500 };
    const keys = ["context:a", "context:b"];
    const columns = { nodeKeys: keys, edgeIds: ["edge:a"], edgeSources: Uint32Array.from([0]),
      edgeTargets: Uint32Array.from([1]), edgeKinds: Uint8Array.from([0]),
      edgeCounts: Uint32Array.from([1]), edgeWeights: Float64Array.from([1]) } as WorkspaceMapColumns;
    const error = new Error("GPU position upload lost its rendering context");
    const onPositions = vi.fn(() => { throw error; });
    const onState = vi.fn(), onError = vi.fn();
    const layout = createWorkspaceD3Layout({ columns, positions, diameters, shapes, pins: [], viewport,
      restoredSolved: true, reducedMotion: false,
      unitBasis: buildWorkspaceD3UnitBasis({ positions, diameters, shapes, viewport }) },
    { onPositions, onState, onError });
    expect(() => mock.options!.onIndexedTick!(positions, keys)).not.toThrow();
    expect(onError).toHaveBeenCalledExactlyOnceWith(error);
    expect(onState).toHaveBeenLastCalledWith({ ready: false, running: false, phase: "paused" });
    expect(mock.driver.destroy).toHaveBeenCalledOnce();
    mock.options!.onIndexedTick!(positions, keys);
    mock.options!.onState!({ ready: true, running: true, phase: "running" });
    expect(onPositions).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledOnce();
    expect(layout.isRunning()).toBe(false);
  });
});

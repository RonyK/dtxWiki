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
    const columns = { nodeKeys: keys, nodeFolderKeys: [null, null], edgeIds: ["edge:a"], edgeSources: Uint32Array.from([0]),
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

  it("finishes one cooling in two batches and accepts a Fit too small to separate every body", () => {
    vi.clearAllMocks();
    // Two bodies a tenth of a unit apart beside a far third stay overlapped at any Fit.
    const dense = Float32Array.from([0, 0, .1, 0, 10_000, 0]);
    const spread = Float32Array.from([0, 0, 5_000, 0, 10_000, 0]);
    const keys = ["context:a", "context:b", "context:c"];
    const input = { columns: { nodeKeys: keys, nodeFolderKeys: [null, null, null], edgeIds: ["edge:a"], edgeSources: Uint32Array.from([0]), edgeTargets: Uint32Array.from([1]),
      edgeKinds: Uint8Array.from([0]), edgeCounts: Uint32Array.from([1]), edgeWeights: Float64Array.from([1]) } as WorkspaceMapColumns,
    positions: dense, diameters: Float32Array.from([6, 6, 6]), shapes: new Float32Array(3), pins: [], reducedMotion: false,
    viewport: { width: 1000, height: 500 } };
    const onPositions = vi.fn(), onState = vi.fn(), onError = vi.fn();
    const ticks = () => mock.driver.update.mock.calls.map(call => call[2].settleTicks);
    const layout = createWorkspaceD3Layout(input, { onPositions, onState, onError });
    expect(mock.options!.initialTicks).toBe(150);
    expect(mock.options!.initialAlpha).toBe(1);
    mock.options!.onIndexedTick!(dense, keys);
    expect(ticks()).toEqual([160]);
    // The Worker holds newer rows than this provisional frame until one is accepted.
    layout.stop();
    expect(mock.driver.pause).toHaveBeenLastCalledWith(true);
    layout.start();
    expect(ticks()).toEqual([160, 1]);
    for (let frame = 0; frame < 4; frame++) mock.options!.onIndexedTick!(dense, keys);
    expect(ticks()).toEqual([160, 1, 80, 80, 80]);
    expect(onPositions).toHaveBeenCalledExactlyOnceWith(dense);
    expect(onError).not.toHaveBeenCalled();

    // Separated bodies are accepted at once, even when the Fit is below the reference.
    mock.driver.update.mockClear();
    const separated = vi.fn();
    createWorkspaceD3Layout(input, { onPositions: separated, onState, onError });
    mock.options!.onIndexedTick!(Float32Array.from([0, 0, 40, 0, 80, 0]), keys);
    mock.options!.onIndexedTick!(spread, keys);
    expect(ticks()).toEqual([160]);
    expect(separated).toHaveBeenCalledExactlyOnceWith(spread);
    expect(onError).not.toHaveBeenCalled();

    // Far-apart saved pins clamp the native Fit to .001. The clearance
    // slack must not turn that supported zoom into an invalid .00097 basis.
    mock.driver.update.mockClear();
    const farPins = Float32Array.from([0, 0, .1, 0, 1_000_000, 0]);
    const farSeparated = Float32Array.from([0, 0, 500_000, 0, 1_000_000, 0]);
    const published = vi.fn(), calibratedBasis = vi.fn();
    createWorkspaceD3Layout({ ...input, positions: farPins, pins: [0, 2],
      viewport: { width: 390, height: 844 } },
    { onPositions: published, onState, onError, onUnitBasis: calibratedBasis });
    mock.options!.onIndexedTick!(farPins, keys);
    expect(ticks()).toEqual([160]);
    mock.options!.onIndexedTick!(farSeparated, keys);
    expect(calibratedBasis).toHaveBeenCalledOnce();
    expect(calibratedBasis.mock.calls[0][0].referenceZoom).toBe(.001);
    expect(published).toHaveBeenCalledExactlyOnceWith(farSeparated);
    expect(onError).not.toHaveBeenCalled();

    // Two TreeRail branches of two rows each: a structured start keeps a low temperature.
    createWorkspaceD3Layout({ ...input, positions: Float32Array.from([0, 0, .1, 0, 10_000, 0, 5, 5]),
      diameters: Float32Array.from([6, 6, 6, 6]), shapes: new Float32Array(4),
      columns: { ...input.columns, nodeKeys: [...keys, "context:d"], nodeContainerIndices: Int32Array.from([-1, -1, 0, 1]) } },
    { onPositions, onState, onError });
    expect(mock.options!.initialAlpha).toBe(.25);
  });
});

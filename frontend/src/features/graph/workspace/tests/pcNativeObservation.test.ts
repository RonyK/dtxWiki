import { describe, expect, it, vi } from "vitest";

import { assertPcNativePositions, hasPcNativeMovement, observePcNativeFrame, type PcNativeDiagnostics, type PcNativeSource } from "./pcNativeObservation";

function fixture() {
  let reads = 0, captures = 0, glError = 0;
  const native = Float32Array.from([10, 20, 30, 40]);
  const source: PcNativeSource = {
    positions: vi.fn(() => { reads++; return native; }),
    counters: () => ({ positionReads: reads, remountCaptures: captures }),
    state: vi.fn(() => ({ viewport: { zoom: reads, pan: { x: reads, y: reads } },
      running: true, geometryUploads: 2, topologyUploads: 1, nativeTicks: reads, fits: [], glError })),
    geometry: vi.fn(positions => ({ nodes: positions.length / 2, boundaryNodes: 0, unpinnedBoundaryNodes: 0,
      overlapPairs: 0, nodesWithOverlappingBodyDisks: 0,
      bounds: { minX: positions[0], maxX: positions[2], minY: positions[1], maxY: positions[3] } })),
  };
  const diagnostics: PcNativeDiagnostics = { frames: 0, positionReads: 0, remountCaptures: 0, glErrorCodes: [], checkpoints: [] };
  return { source, native, diagnostics, setGlError: (value: number) => { glError = value; },
    extraRead: () => { reads++; }, capture: () => { captures++; } };
}

describe("single-read PC native fixture observations", () => {
  it.each([[], [1, 2], [1, 2, NaN, 4], [1, 2, 3, Infinity]].map(positions => ({ positions })))(
    "rejects partial or nonfinite later native frames $positions", ({ positions }) => {
      expect(() => assertPcNativePositions(positions, 2)).toThrow("every finite original-node coordinate");
    },
  );

  it("accepts the exact finite original-node column", () => {
    expect(() => assertPcNativePositions([1, 2, 3, 4], 2)).not.toThrow();
  });

  it("uses one actual native read and passes that same captured array to geometry and the returned frame", () => {
    const { source, diagnostics } = fixture();
    const frame = observePcNativeFrame(source, diagnostics, true, "ready");
    expect(source.positions).toHaveBeenCalledOnce();
    expect(source.geometry).toHaveBeenCalledExactlyOnceWith(frame.positions);
    expect(vi.mocked(source.geometry).mock.calls[0][0]).toBe(frame.positions);
    expect(frame.geometry?.bounds).toEqual({ minX: 10, maxX: 30, minY: 20, maxY: 40 });
    expect(frame.nativeTicks).toBe(1);
    expect(diagnostics).toMatchObject({ frames: 1, positionReads: 1, remountCaptures: 0, glErrorCodes: [0] });
    expect(diagnostics.checkpoints[0]).toMatchObject({ phase: "ready", tick: 1, positionReads: 1, remountCaptures: 0 });
    expect(diagnostics.checkpoints[0].durationMs).toBeGreaterThanOrEqual(0);
  });

  it("copies the native array so later GPU-buffer reuse cannot mutate a paused baseline", () => {
    const { source, diagnostics, native } = fixture();
    const paused = observePcNativeFrame(source, diagnostics, true);
    native[0] = 99;
    const later = observePcNativeFrame(source, diagnostics);
    expect(paused.positions).toEqual([10, 20, 30, 40]);
    expect(paused.geometry?.bounds.minX).toBe(10);
    expect(later.positions[0]).toBe(99);
    expect(hasPcNativeMovement(paused.positions, later.positions)).toBe(true);
  });

  it("does not calculate geometry or perform its queries for an ordinary motion observation", () => {
    const { source, diagnostics } = fixture();
    const frame = observePcNativeFrame(source, diagnostics);
    expect(frame.geometry).toBeNull();
    expect(source.geometry).not.toHaveBeenCalled();
    expect(source.positions).toHaveBeenCalledOnce();
    expect(diagnostics.positionReads).toBe(1);
  });

  it("rejects metadata, camera and tick-only changes as movement when native positions stay frozen", () => {
    const { source, diagnostics } = fixture();
    const before = observePcNativeFrame(source, diagnostics);
    const after = observePcNativeFrame(source, diagnostics);
    expect(after.nativeTicks).not.toBe(before.nativeTicks);
    expect(after.viewport).not.toEqual(before.viewport);
    expect(hasPcNativeMovement(before.positions, after.positions)).toBe(false);
  });

  it.each([[.0009, false], [.001, false], [.0010001, true]] as const)(
    "retains the original >.001 native-coordinate threshold for displacement %f", (delta, expected) => {
      expect(hasPcNativeMovement([0, 0, 0, 0], [0, 0, delta, 0])).toBe(expected);
    },
  );

  it("retains an earlier GL error even when later observations consume zero", () => {
    const owner = fixture();
    owner.setGlError(1280);
    observePcNativeFrame(owner.source, owner.diagnostics);
    owner.setGlError(0);
    observePcNativeFrame(owner.source, owner.diagnostics);
    expect(owner.diagnostics.glErrorCodes).toEqual([1280, 0]);
    expect(() => expect(owner.diagnostics.glErrorCodes).toEqual([0])).toThrow();
  });

  it("exposes an accidental second native read and remount capture instead of fabricating single-read counters", () => {
    const owner = fixture(), read = owner.source.positions;
    owner.source.positions = () => { owner.extraRead(); owner.capture(); return read(); };
    observePcNativeFrame(owner.source, owner.diagnostics);
    expect(owner.diagnostics).toMatchObject({ frames: 1, positionReads: 2, remountCaptures: 1 });
    expect(() => expect(owner.diagnostics.positionReads).toBe(owner.diagnostics.frames)).toThrow();
    expect(() => expect(owner.diagnostics.remountCaptures).toBe(0)).toThrow();
  });

  it("bounds numeric checkpoint details while preserving aggregate read counts and every GL error code", () => {
    const owner = fixture();
    for (let index = 0; index < 100; index++) observePcNativeFrame(owner.source, owner.diagnostics, false, "poll");
    expect(owner.diagnostics.checkpoints).toHaveLength(32);
    expect(owner.diagnostics).toMatchObject({ frames: 100, positionReads: 100, remountCaptures: 0, glErrorCodes: [0] });
  });
});

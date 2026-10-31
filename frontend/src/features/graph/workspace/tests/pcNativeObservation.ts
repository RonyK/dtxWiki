import type { GraphViewport } from "../../shared/rendererTypes";
import type { measureNativeGeometry } from "./nativeGeometry";

export type PcNativeState = {
  viewport: GraphViewport | null;
  running: boolean;
  geometryUploads: number;
  topologyUploads: number;
  nativeTicks: number;
  fits: Array<{ at: number; points: number; before: GraphViewport | null }>;
  glError: number;
};
export type PcNativeDiagnostics = {
  frames: number;
  positionReads: number;
  remountCaptures: number;
  glErrorCodes: number[];
  checkpoints: Array<{ phase: string; tick: number; durationMs: number; positionReads: number; remountCaptures: number }>;
};
export type PcNativeSource = {
  positions(): ArrayLike<number>;
  counters(): { positionReads: number; remountCaptures: number };
  state(): PcNativeState;
  geometry(positions: ArrayLike<number>): ReturnType<typeof measureNativeGeometry>;
};

export function assertPcNativePositions(positions: readonly number[], nodeCount: number) {
  if (positions.length !== nodeCount * 2 || !positions.every(Number.isFinite)) {
    throw new Error("PC native observation requires every finite original-node coordinate");
  }
}

/** Test-only observation: one native read, with geometry from that same frame. */
export function observePcNativeFrame(source: PcNativeSource, diagnostics: PcNativeDiagnostics, geometry = false, phase?: string) {
  const started = performance.now(), before = source.counters();
  const positions = Array.from(source.positions());
  const measured = geometry ? source.geometry(positions) : null;
  const state = source.state(), after = source.counters();
  const reads = after.positionReads - before.positionReads, captures = after.remountCaptures - before.remountCaptures;
  diagnostics.frames++;
  diagnostics.positionReads += reads;
  diagnostics.remountCaptures += captures;
  if (!diagnostics.glErrorCodes.includes(state.glError)) diagnostics.glErrorCodes.push(state.glError);
  if (phase && diagnostics.checkpoints.length < 32) diagnostics.checkpoints.push({ phase,
    tick: state.nativeTicks, durationMs: performance.now() - started, positionReads: reads, remountCaptures: captures });
  return { ...state, positions, geometry: measured };
}

export function hasPcNativeMovement(before: readonly number[], after: readonly number[]) {
  return after.some((value, index) => Math.abs(value - before[index]) > .001);
}

export type PcNativeFrame = ReturnType<typeof observePcNativeFrame>;

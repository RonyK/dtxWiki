import { createWorkspaceGpuReadback } from "./gpuReadback";

/** Borrowed tracking attachment; its rows belong to this exact index snapshot. */
export interface WorkspaceTrackedPositionsFrame {
  readonly gl: WebGL2RenderingContext;
  readonly framebuffer: WebGLFramebuffer;
  readonly width: number;
  readonly height: number;
  readonly indices: readonly number[];
}

interface TrackedPositionsOptions {
  getFrame(): WorkspaceTrackedPositionsFrame | null;
  onPositions(positions: ReadonlyMap<number, [number, number]>): void;
  onError(error: Error): void;
}

/** Request at the label cadence; poll only the fence, never GPU positions, on RAF. */
export function createWorkspaceTrackedPositions(options: TrackedPositionsOptions) {
  let context: WebGL2RenderingContext | null = null;
  let readback: ReturnType<typeof createWorkspaceGpuReadback> | null = null;
  let animationFrame: number | null = null;
  let generation = 0;
  let failed = false;
  let destroyed = false;

  function invalidate() {
    generation += 1;
    if (animationFrame !== null) cancelAnimationFrame(animationFrame);
    animationFrame = null;
    readback?.discard();
  }

  function fail(error: unknown) {
    if (failed || destroyed) return;
    failed = true;
    invalidate();
    readback?.destroy();
    readback = null;
    options.onError(error instanceof Error ? error : new Error(String(error)));
  }

  function request(): boolean {
    if (destroyed || failed || animationFrame !== null || readback?.pending()) return false;
    try {
      const frame = options.getFrame();
      if (!frame || frame.gl.isContextLost() || frame.indices.length === 0) return false;
      const { width, height } = frame;
      const indices = [...frame.indices];
      if (indices.length > 40 || new Set(indices).size !== indices.length
        || indices.some(index => !Number.isInteger(index) || index < 0)
        || !Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1
        || width * height > 64 || width * height < indices.length) {
        throw new RangeError("Tracked GPU label snapshot exceeds its bounded index/texel contract");
      }
      if (context !== frame.gl) {
        readback?.destroy();
        context = frame.gl;
        readback = createWorkspaceGpuReadback(context, 64);
      }
      const owner = readback!;
      if (!owner.request(frame.framebuffer, width, height)) return false;
      const issuedGeneration = generation;
      const poll = () => {
        if (destroyed || failed || issuedGeneration !== generation) return;
        animationFrame = null;
        try {
          const rows = owner.poll();
          if (rows === null) {
            if (owner.pending()) animationFrame = requestAnimationFrame(poll);
            return;
          }
          if (rows.length !== width * height * 4) throw new Error("Invalid tracked GPU label readback size");
          const positions = new Map<number, [number, number]>();
          indices.forEach((index, slot) => {
            const x = rows[slot * 4], y = rows[slot * 4 + 1];
            if (Number.isFinite(x) && Number.isFinite(y)) positions.set(index, [x, y]);
          });
          if (!destroyed && issuedGeneration === generation) options.onPositions(positions);
        } catch (error) { fail(error); }
      };
      animationFrame = requestAnimationFrame(poll);
      return true;
    } catch (error) {
      fail(error);
      return false;
    }
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    invalidate();
    readback?.destroy();
    readback = null;
    context = null;
  }

  return { request, invalidate, destroy };
}

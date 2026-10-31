export interface WorkspaceGpuReadback {
  /** Issues one bounded RGBA/FLOAT read. Never queues behind an existing read. */
  request(framebuffer: WebGLFramebuffer, width: number, height: number): boolean;
  /** Nonblocking: null means no result yet; each completed result is caller-owned. */
  poll(): Float32Array | null;
  pending(): boolean;
  /** Explicit click path only. Cancels any older asynchronous result. */
  readSync(framebuffer: WebGLFramebuffer, width: number, height: number): Float32Array;
  discard(): void;
  destroy(): void;
}

/**
 * A single owned pixel-pack buffer/fence, shared by bounded label/picking reads.
 * There is no scheduler or result queue, and never a full graph readback.
 * Callers retain responsibility for scene/pointer identity and polling cadence.
 */
export function createWorkspaceGpuReadback(
  gl: WebGL2RenderingContext,
  maxTexels: number,
): WorkspaceGpuReadback {
  if (!Number.isInteger(maxTexels) || maxTexels < 1 || maxTexels > 64) {
    throw new RangeError("Workspace GPU readback requires a budget of 1..64 texels");
  }
  let buffer: WebGLBuffer | null = null;
  let fence: WebGLSync | null = null;
  let capacityBytes = 0;
  let resultLength = 0;
  let destroyed = false;

  function validateSize(width: number, height: number): number {
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1
      || width > maxTexels || height > maxTexels || width * height > maxTexels) {
      throw new RangeError("Workspace GPU readback dimensions exceed the texel budget");
    }
    return width * height * 4;
  }

  function discard(): void {
    const previous = fence;
    fence = null;
    resultLength = 0;
    if (previous) gl.deleteSync(previous);
  }

  function release(): void {
    discard();
    const previous = buffer;
    buffer = null;
    capacityBytes = 0;
    if (previous) gl.deleteBuffer(previous);
  }

  function live(): boolean {
    if (destroyed) return false;
    if (!gl.isContextLost()) return true;
    release();
    return false;
  }

  /** Binding/layout mutations are synchronous and restored even on failure. */
  function withReadState<T>(framebuffer: WebGLFramebuffer, action: () => T): T {
    const readFramebuffer = gl.getParameter(gl.READ_FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;
    const drawFramebuffer = gl.getParameter(gl.DRAW_FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;
    const readBuffer = gl.getParameter(gl.READ_BUFFER) as number;
    const packBuffer = gl.getParameter(gl.PIXEL_PACK_BUFFER_BINDING) as WebGLBuffer | null;
    const layout = [gl.PACK_ALIGNMENT, gl.PACK_ROW_LENGTH, gl.PACK_SKIP_ROWS, gl.PACK_SKIP_PIXELS]
      .map(parameter => [parameter, gl.getParameter(parameter) as number] as const);
    try {
      gl.bindFramebuffer(gl.READ_FRAMEBUFFER, framebuffer);
      gl.readBuffer(gl.COLOR_ATTACHMENT0);
      for (const [parameter] of layout) gl.pixelStorei(parameter, parameter === gl.PACK_ALIGNMENT ? 4 : 0);
      return action();
    } finally {
      gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, drawFramebuffer);
      gl.bindFramebuffer(gl.READ_FRAMEBUFFER, readFramebuffer);
      gl.readBuffer(readBuffer);
      gl.bindBuffer(gl.PIXEL_PACK_BUFFER, packBuffer);
      for (const [parameter, value] of layout) gl.pixelStorei(parameter, value);
    }
  }

  return {
    request(framebuffer, width, height) {
      const length = validateSize(width, height);
      if (!live() || fence) return false;
      try {
        return withReadState(framebuffer, () => {
          if (!buffer) {
            buffer = gl.createBuffer();
            if (!buffer) throw new Error("Workspace GPU readback buffer allocation failed");
          }
          gl.bindBuffer(gl.PIXEL_PACK_BUFFER, buffer);
          const bytes = length * Float32Array.BYTES_PER_ELEMENT;
          if (bytes > capacityBytes) {
            gl.bufferData(gl.PIXEL_PACK_BUFFER, bytes, gl.STREAM_READ);
            capacityBytes = bytes;
          }
          gl.readPixels(0, 0, width, height, gl.RGBA, gl.FLOAT, 0);
          fence = gl.fenceSync(gl.SYNC_GPU_COMMANDS_COMPLETE, 0);
          if (!fence) throw new Error("Workspace GPU readback fence allocation failed");
          resultLength = length;
          gl.flush();
          return live();
        });
      } catch (error) {
        release();
        throw error;
      }
    },
    poll() {
      if (!live() || !fence) return null;
      try {
        const status = gl.clientWaitSync(fence, 0, 0);
        if (status === gl.TIMEOUT_EXPIRED) return null;
        if (!live()) return null;
        if (status !== gl.ALREADY_SIGNALED && status !== gl.CONDITION_SATISFIED) {
          throw new Error("Workspace GPU readback fence wait failed");
        }
        const packBuffer = gl.getParameter(gl.PIXEL_PACK_BUFFER_BINDING) as WebGLBuffer | null;
        const output = new Float32Array(resultLength);
        try {
          gl.bindBuffer(gl.PIXEL_PACK_BUFFER, buffer);
          gl.getBufferSubData(gl.PIXEL_PACK_BUFFER, 0, output);
        } finally {
          gl.bindBuffer(gl.PIXEL_PACK_BUFFER, packBuffer);
        }
        discard();
        return live() ? output : null;
      } catch (error) {
        release();
        throw error;
      }
    },
    pending() {
      return live() && fence !== null;
    },
    readSync(framebuffer, width, height) {
      const length = validateSize(width, height);
      if (!live()) throw new Error("Workspace GPU synchronous readback requires a live context");
      discard();
      return withReadState(framebuffer, () => {
        // The typed-array overload is invalid if an enclosing caller bound a PBO.
        gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
        const output = new Float32Array(length);
        gl.readPixels(0, 0, width, height, gl.RGBA, gl.FLOAT, output);
        if (!live()) throw new Error("Workspace GPU context lost during synchronous readback");
        return output;
      });
    },
    discard,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      release();
    },
  };
}

import { describe, expect, it, vi } from "vitest";
import { createWorkspaceGpuReadback } from "../gpuReadback";

function context() {
  let lost = false, serial = 0;
  const names = ["READ_FRAMEBUFFER_BINDING", "DRAW_FRAMEBUFFER_BINDING", "READ_BUFFER", "PIXEL_PACK_BUFFER_BINDING",
    "PACK_ALIGNMENT", "PACK_ROW_LENGTH", "PACK_SKIP_ROWS", "PACK_SKIP_PIXELS", "READ_FRAMEBUFFER", "DRAW_FRAMEBUFFER",
    "COLOR_ATTACHMENT0", "PIXEL_PACK_BUFFER", "STREAM_READ", "RGBA", "FLOAT", "SYNC_GPU_COMMANDS_COMPLETE",
    "TIMEOUT_EXPIRED", "WAIT_FAILED", "ALREADY_SIGNALED", "CONDITION_SATISFIED"] as const;
  const constants = Object.fromEntries(names.map((name, index) => [name, index + 1])) as Record<typeof names[number], number>;
  const borrowed = { read: { borrowed: "read" }, draw: { borrowed: "draw" }, pack: { borrowed: "pack" } };
  const values = new Map<number, unknown>([
    [constants.READ_FRAMEBUFFER_BINDING, borrowed.read], [constants.DRAW_FRAMEBUFFER_BINDING, borrowed.draw],
    [constants.READ_BUFFER, 987], [constants.PIXEL_PACK_BUFFER_BINDING, borrowed.pack],
    [constants.PACK_ALIGNMENT, 8], [constants.PACK_ROW_LENGTH, 128],
    [constants.PACK_SKIP_ROWS, 3], [constants.PACK_SKIP_PIXELS, 7],
  ]);
  const original = new Map(values);
  const readState = () => new Map(values);
  const allocate = () => ({ owned: ++serial });
  const source = Float32Array.from({ length: 256 }, (_, index) => index + .25);
  const api = {
    ...constants,
    isContextLost: vi.fn(() => lost), getParameter: vi.fn((key: number) => values.get(key)),
    createBuffer: vi.fn(allocate), deleteBuffer: vi.fn(), bufferData: vi.fn(),
    bindFramebuffer: vi.fn((target: number, value: unknown) => values.set(
      target === constants.READ_FRAMEBUFFER ? constants.READ_FRAMEBUFFER_BINDING : constants.DRAW_FRAMEBUFFER_BINDING, value)),
    readBuffer: vi.fn((value: number) => values.set(constants.READ_BUFFER, value)),
    bindBuffer: vi.fn((_target: number, value: unknown) => values.set(constants.PIXEL_PACK_BUFFER_BINDING, value)),
    pixelStorei: vi.fn((key: number, value: number) => values.set(key, value)),
    readPixels: vi.fn((...args: unknown[]) => {
      if (args[6] instanceof Float32Array) args[6].set(source.subarray(0, args[6].length));
    }),
    fenceSync: vi.fn(allocate), flush: vi.fn(), deleteSync: vi.fn(),
    clientWaitSync: vi.fn(() => constants.CONDITION_SATISFIED),
    getBufferSubData: vi.fn((_target: number, _offset: number, output: Float32Array) => output.set(source.subarray(0, output.length))),
  };
  return { api, gl: api as unknown as WebGL2RenderingContext, values, original, readState, borrowed, source,
    lose: () => { lost = true; }, restore: () => { lost = false; } };
}

describe("bounded workspace GPU readback", () => {
  it.each([0, -1, 65, 10_000, NaN, Infinity, 1.5])("rejects invalid texel budget %s before GL work", max => {
    const { gl, api } = context();
    expect(() => createWorkspaceGpuReadback(gl, max)).toThrow(RangeError);
    expect(api.getParameter).not.toHaveBeenCalled(); expect(api.createBuffer).not.toHaveBeenCalled();
  });

  it.each([[0, 1], [1, 0], [-1, 1], [1.5, 1], [NaN, 1], [1, Infinity], [8, 9], [65, 1], [10_000, 1]])(
    "rejects dimensions %s × %s, never turning a label read into a graph read", (width, height) => {
      const { gl, api } = context(), reader = createWorkspaceGpuReadback(gl, 64);
      expect(() => reader.request({}, width, height)).toThrow(RangeError);
      expect(() => reader.readSync({}, width, height)).toThrow(RangeError);
      expect(api.readPixels).not.toHaveBeenCalled(); expect(api.createBuffer).not.toHaveBeenCalled();
    });

  it("lazily owns one16-byte PBO/fence and performs no synchronous read on request or pending poll", () => {
    const { gl, api, readState, original } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    expect(reader.pending()).toBe(false); expect(reader.poll()).toBeNull();
    expect(api.createBuffer).not.toHaveBeenCalled();
    expect(reader.request({}, 1, 1)).toBe(true);
    expect(api.bufferData).toHaveBeenCalledExactlyOnceWith(gl.PIXEL_PACK_BUFFER, 16, gl.STREAM_READ);
    expect(api.readPixels).toHaveBeenCalledExactlyOnceWith(0, 0, 1, 1, gl.RGBA, gl.FLOAT, 0);
    expect(api.fenceSync).toHaveBeenCalledExactlyOnceWith(gl.SYNC_GPU_COMMANDS_COMPLETE, 0);
    expect(api.flush).toHaveBeenCalledTimes(1); expect(readState()).toEqual(original);
    expect(reader.pending()).toBe(true); expect(reader.request({}, 1, 1)).toBe(false);
    api.clientWaitSync.mockReturnValue(gl.TIMEOUT_EXPIRED);
    expect(reader.poll()).toBeNull(); expect(reader.pending()).toBe(true);
    expect(api.clientWaitSync).toHaveBeenCalledExactlyOnceWith(api.fenceSync.mock.results[0].value, 0, 0);
    expect(api.getBufferSubData).not.toHaveBeenCalled();
    expect(api.readPixels).toHaveBeenCalledTimes(1); expect(api.createBuffer).toHaveBeenCalledTimes(1);
    reader.destroy();
  });

  it.each(["ALREADY_SIGNALED", "CONDITION_SATISFIED"] as const)("copies one completed result on %s and preserves caller PBO", status => {
    const { gl, api, source, readState, original } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    reader.request({}, 1, 1); api.clientWaitSync.mockReturnValue(gl[status]);
    const first = reader.poll();
    expect(first).toEqual(source.slice(0, 4)); expect(reader.pending()).toBe(false);
    expect(api.getBufferSubData).toHaveBeenCalledExactlyOnceWith(gl.PIXEL_PACK_BUFFER, 0, first);
    expect(api.deleteSync).toHaveBeenCalledTimes(1); expect(readState()).toEqual(original);
    expect(reader.poll()).toBeNull(); expect(api.getBufferSubData).toHaveBeenCalledTimes(1);
    first![0] = -123;
    reader.request({}, 1, 1); const second = reader.poll();
    expect(second).toEqual(source.slice(0, 4)); expect(second).not.toBe(first);
    expect(api.createBuffer).toHaveBeenCalledTimes(1); expect(api.bufferData).toHaveBeenCalledTimes(1);
    reader.destroy(); expect(api.deleteBuffer).toHaveBeenCalledTimes(1);
  });

  it("grows the same PBO only within the64-texel bound and exposes only requested floats", () => {
    const { gl, api } = context(), reader = createWorkspaceGpuReadback(gl, 64);
    reader.request({}, 1, 1); expect(reader.poll()).toHaveLength(4);
    reader.request({}, 7, 7); expect(reader.poll()).toHaveLength(196);
    reader.request({}, 8, 8); expect(reader.poll()).toHaveLength(256);
    reader.request({}, 2, 3); expect(reader.poll()).toHaveLength(24);
    expect(api.createBuffer).toHaveBeenCalledTimes(1);
    expect(api.bufferData.mock.calls).toEqual([[gl.PIXEL_PACK_BUFFER, 16, gl.STREAM_READ],
      [gl.PIXEL_PACK_BUFFER, 784, gl.STREAM_READ], [gl.PIXEL_PACK_BUFFER, 1024, gl.STREAM_READ]]);
    reader.destroy();
  });

  it("normalizes contaminated PACK/read bindings then restores all caller state for async and explicit sync reads", () => {
    const { gl, api, values, readState, original } = context(), reader = createWorkspaceGpuReadback(gl, 64);
    const framebuffer = {};
    api.readPixels.mockImplementation((...args: unknown[]) => {
      expect(values.get(gl.READ_FRAMEBUFFER_BINDING)).toBe(framebuffer);
      expect(values.get(gl.DRAW_FRAMEBUFFER_BINDING)).toBe(original.get(gl.DRAW_FRAMEBUFFER_BINDING));
      expect(values.get(gl.READ_BUFFER)).toBe(gl.COLOR_ATTACHMENT0);
      expect(values.get(gl.PACK_ALIGNMENT)).toBe(4);
      for (const key of [gl.PACK_ROW_LENGTH, gl.PACK_SKIP_ROWS, gl.PACK_SKIP_PIXELS]) expect(values.get(key)).toBe(0);
      if (args[6] instanceof Float32Array) expect(values.get(gl.PIXEL_PACK_BUFFER_BINDING)).toBeNull();
      else expect(values.get(gl.PIXEL_PACK_BUFFER_BINDING)).toBe(api.createBuffer.mock.results[0].value);
    });
    reader.request(framebuffer, 7, 7); expect(readState()).toEqual(original);
    const output = reader.readSync(framebuffer, 1, 1);
    expect(output).toHaveLength(4); expect(reader.pending()).toBe(false);
    expect(api.deleteSync).toHaveBeenCalledTimes(1); expect(readState()).toEqual(original);
    expect(api.clientWaitSync).not.toHaveBeenCalled(); expect(api.getBufferSubData).not.toHaveBeenCalled();
    reader.destroy();
  });

  it("restores state and drops owned resources when request/readPixels throws", () => {
    const { gl, api, readState, original, borrowed } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    api.readPixels.mockImplementationOnce(() => { throw new Error("transfer failed"); });
    expect(() => reader.request({}, 1, 1)).toThrow("transfer failed");
    expect(readState()).toEqual(original); expect(reader.pending()).toBe(false);
    expect(api.deleteBuffer).toHaveBeenCalledTimes(1);
    expect(api.deleteBuffer.mock.calls.flat()).not.toContain(borrowed.pack);
    expect(reader.request({}, 1, 1)).toBe(true); expect(api.createBuffer).toHaveBeenCalledTimes(2);
    reader.destroy();
  });

  it("restores caller PBO and releases the fence/buffer when getBufferSubData throws", () => {
    const { gl, api, readState, original } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    reader.request({}, 1, 1);
    api.getBufferSubData.mockImplementationOnce(() => { throw new Error("copy failed"); });
    expect(() => reader.poll()).toThrow("copy failed");
    expect(readState()).toEqual(original); expect(reader.pending()).toBe(false);
    expect(api.deleteSync).toHaveBeenCalledTimes(1); expect(api.deleteBuffer).toHaveBeenCalledTimes(1);
    reader.destroy(); expect(api.deleteBuffer).toHaveBeenCalledTimes(1);
  });

  it("restores caller read state after explicit synchronous transfer throws", () => {
    const { gl, api, readState, original } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    api.readPixels.mockImplementationOnce(() => { throw new Error("sync transfer failed"); });
    expect(() => reader.readSync({}, 1, 1)).toThrow("sync transfer failed");
    expect(readState()).toEqual(original); expect(reader.pending()).toBe(false);
    expect(api.createBuffer).not.toHaveBeenCalled(); reader.destroy();
  });

  it.each(["buffer", "fence"] as const)("cleans up failed %s allocation and can allocate a fresh bounded request", kind => {
    const { gl, api, readState, original } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    if (kind === "buffer") api.createBuffer.mockReturnValueOnce(null as unknown as ReturnType<typeof api.createBuffer>);
    else api.fenceSync.mockReturnValueOnce(null as unknown as ReturnType<typeof api.fenceSync>);
    expect(() => reader.request({}, 1, 1)).toThrow("allocation failed");
    expect(readState()).toEqual(original); expect(reader.pending()).toBe(false);
    expect(api.deleteBuffer).toHaveBeenCalledTimes(kind === "buffer" ? 0 : 1);
    expect(reader.request({}, 1, 1)).toBe(true); reader.destroy();
  });

  it("cleans up WAIT_FAILED without copying or publishing stale values", () => {
    const { gl, api, readState, original } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    reader.request({}, 1, 1); api.clientWaitSync.mockReturnValue(gl.WAIT_FAILED);
    expect(() => reader.poll()).toThrow("fence wait failed");
    expect(reader.pending()).toBe(false); expect(api.getBufferSubData).not.toHaveBeenCalled();
    expect(api.deleteSync).toHaveBeenCalledTimes(1); expect(api.deleteBuffer).toHaveBeenCalledTimes(1);
    expect(readState()).toEqual(original);
    api.clientWaitSync.mockReturnValue(gl.CONDITION_SATISFIED);
    expect(reader.request({}, 1, 1)).toBe(true); expect(reader.poll()).toHaveLength(4);
    expect(api.createBuffer).toHaveBeenCalledTimes(2); reader.destroy();
  });

  it.each(["pending", "poll", "request"] as const)("context loss at %s discards owned handles and never publishes a result", operation => {
    const { gl, api, lose, restore } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    reader.request({}, 1, 1); lose();
    if (operation === "request") expect(reader.request({}, 1, 1)).toBe(false);
    else if (operation === "pending") expect(reader.pending()).toBe(false);
    else expect(reader.poll()).toBeNull();
    expect(reader.pending()).toBe(false); expect(api.getBufferSubData).not.toHaveBeenCalled();
    expect(api.deleteSync).toHaveBeenCalledTimes(1); expect(api.deleteBuffer).toHaveBeenCalledTimes(1);
    expect(() => reader.readSync({}, 1, 1)).toThrow("requires a live context");
    restore(); expect(reader.request({}, 1, 1)).toBe(true);
    expect(api.createBuffer).toHaveBeenCalledTimes(2); reader.destroy();
  });

  it("does not publish values if context is lost during completion copy", () => {
    const { gl, api, lose, readState, original } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    reader.request({}, 1, 1); api.getBufferSubData.mockImplementationOnce(lose);
    expect(reader.poll()).toBeNull(); expect(readState()).toEqual(original);
    expect(reader.pending()).toBe(false); expect(api.deleteSync).toHaveBeenCalledTimes(1);
    expect(api.deleteBuffer).toHaveBeenCalledTimes(1); reader.destroy();
  });

  it("discard is idempotent and reuses its buffer, while destroy forbids all later reads", () => {
    const { gl, api } = context(), reader = createWorkspaceGpuReadback(gl, 1);
    reader.request({}, 1, 1); reader.discard(); reader.discard();
    expect(reader.pending()).toBe(false); expect(reader.poll()).toBeNull();
    expect(api.deleteSync).toHaveBeenCalledTimes(1); expect(api.deleteBuffer).not.toHaveBeenCalled();
    reader.request({}, 1, 1); expect(api.createBuffer).toHaveBeenCalledTimes(1);
    reader.destroy(); reader.destroy(); reader.discard();
    expect(api.deleteSync).toHaveBeenCalledTimes(2); expect(api.deleteBuffer).toHaveBeenCalledTimes(1);
    expect(reader.request({}, 1, 1)).toBe(false); expect(reader.pending()).toBe(false); expect(reader.poll()).toBeNull();
    expect(() => reader.readSync({}, 1, 1)).toThrow("requires a live context");
    expect(api.readPixels).toHaveBeenCalledTimes(2);
  });
});

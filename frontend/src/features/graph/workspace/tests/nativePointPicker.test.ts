import { describe, expect, it, vi } from "vitest";
import type { CosmosPointPickFrame } from "../cosmosPointPickingTypes";
import {
  NATIVE_MOVING_ANSWER_INTERVAL_MS, NATIVE_SLOW_READ_BACKOFF_MS, NATIVE_SLOW_READ_MS, createWorkspaceNativePointPicker,
} from "../nativePointPicker";

// These tests exercise ownership/readback/lifecycle, not GPU shader semantics.
// The real native WebGL overlap fixture covers actual nearest-center rasterization.
vi.mock("../forceAtlas2GlState", () => ({ withWorkspaceForceAtlas2GlState: (_gl: unknown, action: () => unknown) => action() }));

/**
 * A restore hands the same context object back, but only objects made since then are valid, and extension state is reset
 * (`floatTargets` is whether EXT_color_buffer_float is on when the picker first meets the context). Deleting an invalid
 * object on a live context is what raises INVALID_OPERATION: `stale` collects those; on a lost context it is a no-op.
 */
function context({ floatTargets = true }: { floatTargets?: boolean } = {}) {
  let lost = false, nextHandle = 0, drawn = false, pbo = false, floatsOn = floatTargets;
  const result = Float32Array.from([0, 2048, 2048, 1]), miss = new Float32Array(4);
  const live = new Set<object>(), stale: object[] = [], canvas = new EventTarget();
  const bound: Record<"program" | "draw" | "vao", object | null> = { program: null, draw: null, vao: null };
  const values = new Map<number, unknown>();
  const names = ["DEPTH_FUNC", "DEPTH_WRITEMASK", "DEPTH_RANGE", "VERTEX_SHADER", "FRAGMENT_SHADER", "COMPILE_STATUS",
    "LINK_STATUS", "TEXTURE3", "TEXTURE_2D", "TEXTURE_MIN_FILTER", "TEXTURE_MAG_FILTER", "NEAREST", "TEXTURE_WRAP_S",
    "TEXTURE_WRAP_T", "CLAMP_TO_EDGE", "RGBA32F", "DEPTH_COMPONENT24", "RGBA", "FLOAT", "DRAW_FRAMEBUFFER",
    "COLOR_ATTACHMENT0", "DEPTH_ATTACHMENT", "FRAMEBUFFER_COMPLETE", "PIXEL_PACK_BUFFER", "STREAM_READ", "BLEND",
    "STENCIL_TEST", "SCISSOR_TEST", "CULL_FACE", "RASTERIZER_DISCARD", "DEPTH_TEST", "LESS", "COLOR", "DEPTH",
    "TEXTURE0", "POINTS", "READ_FRAMEBUFFER", "SYNC_GPU_COMMANDS_COMPLETE", "TIMEOUT_EXPIRED", "WAIT_FAILED",
    "CONDITION_SATISFIED", "ALREADY_SIGNALED", "READ_FRAMEBUFFER_BINDING", "DRAW_FRAMEBUFFER_BINDING", "READ_BUFFER",
    "PIXEL_PACK_BUFFER_BINDING", "PACK_ALIGNMENT", "PACK_ROW_LENGTH", "PACK_SKIP_ROWS", "PACK_SKIP_PIXELS",
    "FRAMEBUFFER_INCOMPLETE_ATTACHMENT"] as const;
  const constants = Object.fromEntries(names.map((name, index) => [name, index + 1])) as Record<typeof names[number], number>;
  values.set(constants.DEPTH_FUNC, 999); values.set(constants.DEPTH_WRITEMASK, false);
  values.set(constants.DEPTH_RANGE, Float32Array.from([.2, .8]));
  const allocate = () => { const handle = { handle: ++nextHandle }; live.add(handle); return handle; };
  const release = (handle: object) => { if (!lost && !live.has(handle)) stale.push(handle); live.delete(handle); };
  const exists = (handle: object | null) => handle !== null && live.has(handle);
  const api = { ...constants, canvas,
    isContextLost: vi.fn(() => lost), getParameter: vi.fn((name: number) => values.get(name)),
    depthFunc: vi.fn((value: number) => values.set(constants.DEPTH_FUNC, value)),
    depthMask: vi.fn((value: boolean) => values.set(constants.DEPTH_WRITEMASK, value)),
    depthRange: vi.fn((first: number, last: number) => values.set(constants.DEPTH_RANGE, Float32Array.from([first, last]))),
    createShader: vi.fn(allocate), createProgram: vi.fn(allocate), createVertexArray: vi.fn(allocate),
    createFramebuffer: vi.fn(allocate), createBuffer: vi.fn(allocate), createTexture: vi.fn(allocate), fenceSync: vi.fn(allocate),
    getShaderParameter: vi.fn(() => true), getProgramParameter: vi.fn(() => true), getUniformLocation: vi.fn(allocate),
    getExtension: vi.fn((name: string) => { if (name === "EXT_color_buffer_float") floatsOn = true; return {}; }),
    // A float colour attachment is only renderable once the extension is on, as in WebGL2.
    checkFramebufferStatus: vi.fn(() => floatsOn ? constants.FRAMEBUFFER_COMPLETE : constants.FRAMEBUFFER_INCOMPLETE_ATTACHMENT),
    shaderSource: vi.fn(), compileShader: vi.fn(), attachShader: vi.fn(), linkProgram: vi.fn(),
    activeTexture: vi.fn(), bindTexture: vi.fn(), texParameteri: vi.fn(), texStorage2D: vi.fn(), texSubImage2D: vi.fn(),
    bindFramebuffer: vi.fn((target: number, handle: object | null) => { if (target === constants.DRAW_FRAMEBUFFER) bound.draw = handle; }),
    framebufferTexture2D: vi.fn(), bindBuffer: vi.fn(), bufferData: vi.fn(),
    useProgram: vi.fn((handle: object | null) => { bound.program = handle; }),
    bindVertexArray: vi.fn((handle: object | null) => { bound.vao = handle; }),
    viewport: vi.fn(), disable: vi.fn(), enable: vi.fn(), colorMask: vi.fn(),
    clearBufferfv: vi.fn(), uniform1i: vi.fn(), uniform1f: vi.fn(), uniform2f: vi.fn(), uniformMatrix4fv: vi.fn(),
    // A draw through a dead program, framebuffer or vertex array writes nothing, so the read finds no point.
    drawArrays: vi.fn(() => { drawn = exists(bound.program) && exists(bound.draw) && exists(bound.vao); }),
    readBuffer: vi.fn(), pixelStorei: vi.fn(), flush: vi.fn(), deleteSync: vi.fn(release),
    deleteTexture: vi.fn(release), deleteShader: vi.fn(release), deleteBuffer: vi.fn(release), deleteFramebuffer: vi.fn(release),
    deleteVertexArray: vi.fn(release), deleteProgram: vi.fn(release),
    isProgram: vi.fn(exists), isFramebuffer: vi.fn(exists), isVertexArray: vi.fn(exists), isTexture: vi.fn(exists),
    readPixels: vi.fn((...args: unknown[]) => {
      if (args[6] instanceof Float32Array) args[6].set(drawn ? result : miss); else pbo = drawn;
    }),
    clientWaitSync: vi.fn((sync: object) => exists(sync) ? constants.CONDITION_SATISFIED : constants.WAIT_FAILED),
    getBufferSubData: vi.fn((_target: number, _offset: number, output: Float32Array) => output.set(pbo ? result : miss)),
  };
  return { api, gl: api as unknown as WebGL2RenderingContext, result, stale,
    // The context reports itself lost; its loss event has not been delivered yet.
    lose: () => { lost = true; },
    loseAndNotify: () => { lost = true; canvas.dispatchEvent(new Event("webglcontextlost")); },
    restore: () => { lost = false; live.clear(); floatsOn = false; } };
}

function frame(gl: WebGL2RenderingContext): CosmosPointPickFrame {
  return { gl, positionTexture: {}, pointStatusTexture: {}, exitTexture: {}, textureSize: { width: 2, height: 2 },
    nodeCount: 2, transformationMatrix: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    screenSize: [600, 400], pointer: [300, 200], spaceSize: 4096, pointSizeScale: 1, scalePointsOnZoom: false,
    maxPointSize: 64, pixelRatio: 1, pointOpacity: 1, pointGreyoutOpacity: null };
}

const moved = (input: CosmosPointPickFrame, dx: number, dy = 0): CosmosPointPickFrame =>
  ({ ...input, pointer: [input.pointer[0] + dx, input.pointer[1] + dy] });

describe("native nearest-center query ownership", () => {
  it("reads exactly one RGBA pixel synchronously and restores caller depth state", () => {
    const { gl, api } = context(), picker = createWorkspaceNativePointPicker(Float32Array.from([3, 20]), Float32Array.from([0, 1]));
    expect(picker.pickSync(frame(gl))).toEqual({ index: 0, position: [2048, 2048] });
    expect(api.readPixels).toHaveBeenCalledExactlyOnceWith(0, 0, 1, 1, gl.RGBA, gl.FLOAT, expect.any(Float32Array));
    expect((api.readPixels.mock.calls[0][6] as Float32Array).length).toBe(4);
    expect(api.depthFunc).toHaveBeenLastCalledWith(999); expect(api.depthMask).toHaveBeenLastCalledWith(false);
    expect(api.depthRange.mock.calls.at(-1)).toEqual([Math.fround(.2), Math.fround(.8)]);
    picker.destroy();
  });

  it("uses a 16-byte PBO and returns only four floats when its fence completes", () => {
    const { gl, api } = context(), input = frame(gl);
    const picker = createWorkspaceNativePointPicker(Float32Array.from([3, 20]), Float32Array.from([0, 1]));
    expect(picker.request(input)).toBe(true); expect(picker.hasPending).toBe(true);
    expect(picker.request(input)).toBe(false);
    expect(api.bufferData).toHaveBeenCalledWith(gl.PIXEL_PACK_BUFFER, 16, gl.STREAM_READ);
    expect(api.readPixels).toHaveBeenCalledExactlyOnceWith(0, 0, 1, 1, gl.RGBA, gl.FLOAT, 0);
    expect(picker.take(input)).toEqual({ index: 0, position: [2048, 2048] });
    expect((api.getBufferSubData.mock.calls[0][2] as Float32Array).length).toBe(4);
    expect(picker.hasPending).toBe(false); picker.destroy();
  });

  // The pointer is judged separately below: a one-frame-old result may still serve it.
  it.each(["screenSize", "pixelRatio", "spaceSize", "transformationMatrix", "pointSizeScale"] as const)(
    "discards a completed result after %s changed, without publishing or reading it", key => {
      const { gl, api } = context(), input = frame(gl);
      const picker = createWorkspaceNativePointPicker(Float32Array.from([3, 20]), Float32Array.from([0, 1]));
      picker.request(input);
      const changed = { ...input, [key]: Array.isArray(input[key]) ? [...input[key] as readonly number[]].map(value => value + 1)
        : Number(input[key]) + 1 } as CosmosPointPickFrame;
      expect(picker.take(changed)).toBeUndefined(); expect(picker.hasPending).toBe(false);
      expect(api.getBufferSubData).not.toHaveBeenCalled(); expect(api.deleteSync).toHaveBeenCalledTimes(1);
      expect(picker.request(changed)).toBe(true); picker.destroy();
    });

  it("does not starve an async query merely because native simulation advanced its position texture", () => {
    const { gl } = context(), input = frame(gl);
    const picker = createWorkspaceNativePointPicker(Float32Array.from([3, 20]), Float32Array.from([0, 1]));
    picker.request(input);
    expect(picker.take({ ...input, positionTexture: {} })).toEqual({ index: 0, position: [2048, 2048] });
    picker.destroy();
  });

  it("fails closed on context loss, scene-count mismatch, destroyed provider and invalid GPU rows", () => {
    const { gl, api, lose, result } = context(), input = frame(gl);
    const picker = createWorkspaceNativePointPicker(Float32Array.from([3, 20]), Float32Array.from([0, 1]));
    result.set([5, 2048, 2048, 1]); expect(picker.pickSync(input)).toBeNull();
    expect(picker.pickSync({ ...input, nodeCount: 3 })).toBeNull();
    picker.request(input); lose(); expect(picker.take(input)).toBeUndefined(); expect(picker.hasPending).toBe(false);
    picker.destroy(); picker.destroy(); expect(picker.pickSync(input)).toBeNull();
    expect(api.deleteTexture).toHaveBeenCalledTimes(3);
    expect(api.deleteShader).toHaveBeenCalledTimes(2); expect(api.deleteProgram).toHaveBeenCalledTimes(1);
    expect(api.deleteVertexArray).toHaveBeenCalledTimes(1); expect(api.deleteBuffer).toHaveBeenCalledTimes(1);
    expect(api.deleteFramebuffer).toHaveBeenCalledTimes(1);
    expect(api.deleteTexture.mock.calls.flat()).not.toContain(input.positionTexture);
  });

  it.each([[[], []], [[1], [NaN]], [[NaN], [0]], [[-1], [0]], [[1, 2], [0]]])("rejects invalid size/shape rows", (sizes, shapes) => {
    expect(() => createWorkspaceNativePointPicker(Float32Array.from(sizes), Float32Array.from(shapes))).toThrow();
  });
});

describe("pointer motion while a native query is in flight", () => {
  // Node 0 projects exactly onto the base pointer (300, 200); node 1 is a larger square.
  const create = (sizes = [3, 20], shapes = [0, 1]) => createWorkspaceNativePointPicker(Float32Array.from(sizes), Float32Array.from(shapes));
  const hit = { index: 0, position: [2048, 2048] };

  // The fence resolves a frame after its query, by which time a moving pointer has moved on. The answer still serves it
  // while its node is within the 12 px target of the pointer NOW, however far the pointer travelled to get there.
  it.each([
    ["a small move stays on the node", 0, [5, -4], "served"],
    ["a 24 px move across the node ends on its 12 px edge: the target is measured from the node, not from the move", -12, [24, 0], "served"],
    ["a 7 px move out of the target (13 px from the node) is dropped", 6, [7, 0], "dropped"],
    ["a diagonal 9,9 is 12.7 px, not 9 px per axis", 0, [9, 9], "dropped"],
    ["a jump far beyond any target is dropped", 0, [100, 100], "dropped"],
  ] as const)("%s", (_name, from, by, outcome) => {
    const { gl } = context(), asked = moved(frame(gl), from), next = moved(asked, by[0], by[1]), picker = create();
    picker.request(asked);
    expect(picker.take(next)).toEqual(outcome === "served" ? hit : undefined);
    // Whatever the outcome the slot is free, so the caller asks again in the same frame.
    expect(picker.hasPending).toBe(false); expect(picker.request(next)).toBe(true); picker.destroy();
  });

  // The shader keeps a point whose body is under the pointer even beyond 12 px, so the CPU re-check uses the ANSWERED
  // node's own size and shape (not node 0's, not the largest node's), as the shader does.
  it.each([
    ["a 40 px square answered beside a 3 px circle reaches its corner", [3, 40], [0, 1], 1, [16, 16], true],
    ["a 40 px circle drops the corner that a square of the same size keeps", [40, 40], [1, 0], 1, [16, 16], false],
    ["a 40 px circle is still hit inside its body, past 12 px", [40, 3], [0, 0], 0, [14, 14], true],
    ["a 3 px circle beside a 40 px node has only the 12 px target", [40, 3], [0, 0], 1, [13, 0], false],
    // The GPU clamps the rendered size to maxPointSize (64 here): a 100 px square reaches 25.6 px, not 40 px.
    ["a 100 px square is capped to maxPointSize and drops what an uncapped body would keep", [3, 100], [0, 1], 1, [30, 30], false],
  ] as const)("%s", (_name, sizes, shapes, answered, by, served) => {
    const { gl, result } = context(), input = frame(gl), picker = create([...sizes], [...shapes]);
    result.set([answered, 2048, 2048, 1]); picker.request(input);
    expect(picker.take(moved(input, by[0], by[1]))).toEqual(served ? { index: answered, position: [2048, 2048] } : undefined);
    picker.destroy();
  });

  it("never applies a 'no point' answer to a pointer that moved after it was asked", () => {
    const { gl, result } = context(), input = frame(gl), picker = create();
    result.set([-1, 0, 0, 0]);
    picker.request(input);
    // Nothing proves the moved pointer is still outside every node, so this is obsolete, not "no point".
    expect(picker.take(moved(input, 1))).toBeUndefined(); expect(picker.hasPending).toBe(false);
    picker.request(input);
    expect(picker.take(input)).toBeNull(); picker.destroy();
  });

  // The pointer is no longer part of the scene: a camera or size change must still discard when the pointer moved too.
  // A pan alone changes only the translation, which a check of the scale would not see.
  it.each([
    ["a pan of the camera", { transformationMatrix: [1, 0, 0, 0, 0, 1, 0, 0, 1e-3, 0, 1, 0, 0, 0, 0, 1] }],
    ["a canvas resize", { screenSize: [601, 400] as const }],
  ])("discards unread on %s that also moved the pointer", (_name, change) => {
    const { gl, api } = context(), input = frame(gl), picker = create();
    picker.request(input);
    expect(picker.take({ ...moved(input, 1), ...change })).toBeUndefined(); expect(picker.hasPending).toBe(false);
    expect(api.getBufferSubData).not.toHaveBeenCalled(); expect(api.deleteSync).toHaveBeenCalledTimes(1);
    expect(picker.request(input)).toBe(true); picker.destroy();
  });

  it("discards when another context object asks, even at the original pointer", () => {
    const { gl } = context(), other = context(), input = frame(gl), picker = create();
    picker.request(input);
    expect(picker.take({ ...input, gl: other.gl })).toBeUndefined(); expect(picker.hasPending).toBe(false); picker.destroy();
  });

  it("keeps a query whose fence is not yet signaled for the next frame, and starts no interval for it", () => {
    const { gl, api } = context(), input = frame(gl), picker = create();
    picker.request(input);
    api.clientWaitSync.mockReturnValueOnce(api.TIMEOUT_EXPIRED);
    expect(picker.take(moved(input, 3))).toBeUndefined(); expect(picker.hasPending).toBe(true);
    expect(api.getBufferSubData).not.toHaveBeenCalled(); expect(api.deleteSync).not.toHaveBeenCalled();
    expect(picker.take(moved(input, 6))).toEqual(hit); expect(picker.hasPending).toBe(false); picker.destroy();
  });

  it("lets a synchronous pick retire an in-flight query, so a click or drag never sees an older answer", () => {
    const { gl, api } = context(), input = frame(gl), picker = create();
    picker.request(input); expect(picker.hasPending).toBe(true);
    expect(picker.pickSync(moved(input, 3))).toEqual(hit);
    expect(picker.hasPending).toBe(false); expect(api.deleteSync).toHaveBeenCalledTimes(1);
    expect(picker.take(moved(input, 3))).toBeUndefined();
    // Even a frame it cannot query for retires the older answer.
    picker.request(input);
    expect(picker.pickSync({ ...input, pointer: [Number.NaN, 0] })).toBeNull(); expect(picker.hasPending).toBe(false);
    picker.destroy();
  });
});

describe("bounded answer rate for a moving pointer", () => {
  const INTERVAL = NATIVE_MOVING_ANSWER_INTERVAL_MS, SLOW = NATIVE_SLOW_READ_MS, BACKOFF = NATIVE_SLOW_READ_BACKOFF_MS;
  const hit = { index: 0, position: [2048, 2048] };
  // A clock the test moves by hand; a slow GPU read is a read that advances it.
  function rig() {
    const fake = context();
    let time = 5_000;
    const clock = { now: () => time, advance: (ms: number) => { time += ms; } };
    const picker = createWorkspaceNativePointPicker(Float32Array.from([3, 20]), Float32Array.from([0, 1]), undefined, clock.now);
    const slowRead = (ms: number) => fake.api.getBufferSubData.mockImplementationOnce((_target: number, _offset: number, output: Float32Array) => {
      output.set(fake.result); clock.advance(ms);
    });
    return { ...fake, clock, picker, input: frame(fake.gl), slowRead };
  }

  it("answers a pointer that keeps moving at most once per interval, a read that left the target included, and never holds a pointer at rest", () => {
    const { picker, input, clock, api } = rig();
    // GPU readbacks stay bounded: answers for a moving pointer come no faster than one per animation frame.
    expect(INTERVAL).toBeGreaterThanOrEqual(16);
    picker.request(input); clock.advance(16);
    // The node is out of reach, so this answer is dropped, but reading it cost the same and starts the interval.
    expect(picker.take(moved(input, 13))).toBeUndefined(); expect(api.getBufferSubData).toHaveBeenCalledTimes(1);
    // Inside the interval the next answer is dropped unread, though its node is now on the pointer.
    picker.request(moved(input, 13)); clock.advance(INTERVAL - 1);
    expect(picker.take(moved(input, 2))).toBeUndefined(); expect(picker.hasPending).toBe(false);
    expect(api.getBufferSubData).toHaveBeenCalledTimes(1);
    picker.request(moved(input, 2)); clock.advance(1);
    expect(picker.take(moved(input, 1))).toEqual(hit); expect(api.getBufferSubData).toHaveBeenCalledTimes(2);
    // A pointer that stopped is asked and taken at the same position, and is served at once, inside the new interval.
    picker.request(moved(input, 1));
    expect(picker.take(moved(input, 1))).toEqual(hit); expect(api.getBufferSubData).toHaveBeenCalledTimes(3);
    picker.destroy();
  });

  it("stays silent for a moved pointer for the back-off after a slow read, but still reads a pointer at rest", () => {
    const { picker, input, clock, api, slowRead } = rig();
    slowRead(SLOW * 3);
    picker.request(input);
    expect(picker.take(input)).toEqual(hit);
    // Past the plain interval, inside the back-off: a moved pointer is not read.
    picker.request(input); clock.advance(INTERVAL + 1);
    expect(picker.take(moved(input, 1))).toBeUndefined(); expect(api.getBufferSubData).toHaveBeenCalledTimes(1);
    // A pointer at rest is always read.
    picker.request(moved(input, 1));
    expect(picker.take(moved(input, 1))).toEqual(hit); expect(api.getBufferSubData).toHaveBeenCalledTimes(2);
    // The back-off ends BACKOFF ms after the slow read finished.
    picker.request(moved(input, 1)); clock.advance(BACKOFF - (INTERVAL + 1) - 1);
    expect(picker.take(moved(input, 2))).toBeUndefined(); expect(api.getBufferSubData).toHaveBeenCalledTimes(2);
    picker.request(moved(input, 2)); clock.advance(1);
    expect(picker.take(moved(input, 1))).toEqual(hit); expect(api.getBufferSubData).toHaveBeenCalledTimes(3);
    picker.destroy();
  });
});

describe("context restoration on the same context object", () => {
  const create = (onError?: (error: unknown) => void) => createWorkspaceNativePointPicker(Float32Array.from([3, 20]), Float32Array.from([0, 1]), onError);
  const hit = { index: 0, position: [2048, 2048] };

  it("releases its GL objects at the loss and builds new ones on the restored context, deleting nothing stale", () => {
    const { gl, api, loseAndNotify, restore, stale } = context(), input = frame(gl), picker = create();
    expect(picker.pickSync(input)).toEqual(hit);
    loseAndNotify();
    expect(picker.pickSync(input)).toBeNull();
    restore();
    // The mock draws nothing through a dead program, framebuffer or vertex array, so a hit means everything was rebuilt.
    expect(picker.pickSync(input)).toEqual(hit); expect(api.createProgram).toHaveBeenCalledTimes(2);
    // The rebuilt objects are kept; later queries neither rebuild nor ask the GPU process whether they survived.
    picker.request(input); picker.take(input);
    expect(picker.pickSync(input)).toEqual(hit); expect(api.createProgram).toHaveBeenCalledTimes(2);
    for (const check of ["isProgram", "isFramebuffer", "isVertexArray", "isTexture"] as const) expect(api[check], check).not.toHaveBeenCalled();
    picker.destroy(); expect(stale).toEqual([]);
  });

  it("retires a query in flight at the loss, with no call in between, and serves the async path again after the restore", () => {
    const { gl, loseAndNotify, restore, stale } = context(), input = frame(gl), onError = vi.fn(), picker = create(onError);
    expect(picker.request(input)).toBe(true);
    loseAndNotify(); restore();
    // Its fence and buffer died with the context: never waited on or deleted afterwards.
    expect(picker.hasPending).toBe(false); expect(picker.take(input)).toBeUndefined();
    expect(picker.request(input)).toBe(true); expect(picker.take(input)).toEqual(hit);
    expect(onError).not.toHaveBeenCalled(); picker.destroy(); expect(stale).toEqual([]);
  });

  it("requests EXT_color_buffer_float itself before building its float attachment, again after a restore reset it", () => {
    const { gl, api, loseAndNotify, restore } = context({ floatTargets: false }), input = frame(gl), onError = vi.fn(), picker = create(onError);
    expect(picker.pickSync(input)).toEqual(hit);
    loseAndNotify(); restore();
    expect(picker.pickSync(input)).toEqual(hit);
    expect(api.getExtension).toHaveBeenCalledWith("EXT_color_buffer_float");
    expect(onError).not.toHaveBeenCalled(); picker.destroy();
  });
});

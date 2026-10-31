import { expect, test, type Page } from "@playwright/test";
import { createContext, runInContext } from "node:vm";

import { captureNativeLongCalls, installNativeDrawMeter, NATIVE_LONG_CALL_PREFIX } from "../e2e/workspace_graph_native_draw";

// Execute the actual serialized init script without requesting a browser/page
// fixture. A pending application DOM evaluation is deliberately not involved.
async function harness(enabled?: boolean, inheritedNativeMethods = false) {
  let clock = 100;
  let elapsed = 101;
  let thrown: unknown;
  let sinkThrows = false;
  let clockThrows = false;
  let queries = 0;
  let trackParameterCalls = false;
  let duringNative: (() => void) | undefined;
  const result = {};
  const calls: Array<{ receiver: unknown; method: string; args: unknown[] }> = [];
  const logs: string[] = [];
  let observer: ((list: { getEntries(): Array<{ startTime: number; duration: number }> }) => void) | undefined;
  const native = (receiver: unknown, method: string, args: unknown[]) => {
    calls.push({ receiver, method, args });
    duringNative?.();
    clock += elapsed;
    if (thrown !== undefined) throw thrown;
    return result;
  };
  class Canvas {
    getContext() { return gl; }
  }
  class GL {
    canvas = new Canvas();
    POINTS = 0;
    TRIANGLE_STRIP = 5;
    DRAW_FRAMEBUFFER_BINDING = 36_006;
    getParameter(...args: unknown[]) { queries += 1; return trackParameterCalls ? native(this, "getParameter", args) : null; }
    readPixels(...args: unknown[]) { return native(this, "readPixels", args); }
    drawArrays(...args: unknown[]) { return native(this, "drawArrays", args); }
    drawArraysInstanced(...args: unknown[]) { return native(this, "drawArraysInstanced", args); }
    clientWaitSync(...args: unknown[]) { return native(this, "clientWaitSync", args); }
    getBufferSubData(...args: unknown[]) { return native(this, "getBufferSubData", args); }
    flush(...args: unknown[]) { return native(this, "flush", args); }
  }
  const originals = { readPixels: GL.prototype.readPixels, drawArrays: GL.prototype.drawArrays,
    drawArraysInstanced: GL.prototype.drawArraysInstanced, getParameter: GL.prototype.getParameter,
    clientWaitSync: GL.prototype.clientWaitSync, getBufferSubData: GL.prototype.getBufferSubData,
    flush: GL.prototype.flush };
  if (inheritedNativeMethods) {
    const parent = Object.create(Object.getPrototypeOf(GL.prototype));
    for (const method of Object.keys(originals) as Array<keyof typeof originals>) {
      Object.defineProperty(parent, method, Object.getOwnPropertyDescriptor(GL.prototype, method)!);
      Reflect.deleteProperty(GL.prototype, method);
    }
    Object.setPrototypeOf(GL.prototype, parent);
  }
  const gl = new GL();
  const context = createContext({ HTMLCanvasElement: Canvas, WebGL2RenderingContext: GL, window: {},
    performance: { now: () => { if (clockThrows) throw new Error("clock failed"); return clock; } },
    console: { info: (text: string) => { if (sinkThrows) throw new Error("sink failed"); logs.push(text); } },
    PerformanceObserver: class {
      static supportedEntryTypes = ["longtask"];
      constructor(callback: NonNullable<typeof observer>) { observer = callback; }
      observe() { /* No browser observer needed. */ }
    },
  });
  const page = { async addInitScript(callback: Function, argument: unknown) {
    runInContext(`(${callback.toString()})(${JSON.stringify(argument)})`, context);
  } } as unknown as Page;
  await installNativeDrawMeter(page, enabled === undefined ? undefined : { longCallLog: enabled });
  return { gl, originals, calls, logs, result, context,
    get queries() { return queries; },
    setElapsed(value: number) { elapsed = value; },
    setThrown(value: unknown) { thrown = value; },
    trackParameters() { trackParameterCalls = true; },
    onNative(callback: () => void) { duringNative = callback; },
    failSink() { sinkThrows = true; },
    failClock() { clockThrows = true; },
    task(startTime: number, duration: number) { observer?.({ getEntries: () => [{ startTime, duration }] }); },
    records: () => logs.map(text => JSON.parse(text.slice(NATIVE_LONG_CALL_PREFIX.length))),
  };
}

test("native cost opt-in preserves CPU-array/PBO overloads, this, arguments and return values", async () => {
  const meter = await harness(true);
  const pixels = new Float32Array([31_337, 99]);
  const arrayArgs = [2, 3, 17, 19, 6_408, 5_126, pixels, 1];
  expect(meter.gl.readPixels(...arrayArgs)).toBe(meter.result);
  const pboArgs = [4, 5, 1, 1, 6_408, 5_126, 16];
  expect(meter.gl.readPixels(...pboArgs)).toBe(meter.result);
  expect(meter.calls[0].receiver).toBe(meter.gl);
  expect(meter.calls[0].args).toEqual(arrayArgs);
  expect(meter.calls[0].args[6]).toBe(pixels);
  expect(meter.calls[1].args).toEqual(pboArgs);
  expect(meter.records()).toEqual([
    { operation: 1, phase: 0, width: 17, height: 19, target: 1, startedMs: 100, endedMs: 100, wallMs: 0, threw: 0 },
    { operation: 1, phase: 1, width: 17, height: 19, target: 1, startedMs: 100, endedMs: 201, wallMs: 101, threw: 0 },
    { operation: 1, phase: 1, width: 1, height: 1, target: 2, startedMs: 201, endedMs: 302, wallMs: 101, threw: 0 },
  ]);
  expect(meter.queries).toBe(0);
  expect(meter.logs.join(" ")).not.toContain("31337");
});

test("native cost draw wrappers add no GL query/read and retain native counters", async () => {
  const plain = await harness(false);
  const timed = await harness(true);
  for (const meter of [plain, timed]) {
    expect(meter.gl.drawArrays(0, 0, 10_000)).toBe(meter.result);
    expect(meter.gl.drawArraysInstanced(5, 0, 4, 100_000)).toBe(meter.result);
    expect(meter.calls.map(call => [call.method, call.args])).toEqual([
      ["drawArrays", [0, 0, 10_000]], ["drawArraysInstanced", [5, 0, 4, 100_000]],
    ]);
    expect(meter.calls.every(call => call.receiver === meter.gl)).toBe(true);
    expect(runInContext("window.__pc10kNativeDraw.points", meter.context)).toBe(10_000);
    expect(runInContext("window.__pc10kNativeDraw.links", meter.context)).toBe(100_000);
  }
  expect(timed.queries).toBe(plain.queries);
  expect(timed.queries).toBe(2); // Only the meter's pre-existing framebuffer checks.
  expect(timed.records().map(record => [record.operation, record.width, record.height, record.target]))
    .toEqual([[2, 0, 0, 0], [3, 0, 0, 0]]);
});

test("native cost supports inherited native methods without freezing counter-wrapper assignment", async () => {
  const meter = await harness(true, true);
  expect(meter.gl.readPixels(0, 0, 1, 1, 0, 0, 0)).toBe(meter.result);
  expect(meter.gl.drawArrays(0, 0, 10_000)).toBe(meter.result);
  expect(meter.gl.drawArraysInstanced(5, 0, 4, 100_000)).toBe(meter.result);
  expect(meter.calls.every(call => call.receiver === meter.gl)).toBe(true);
  expect(runInContext("window.__pc10kNativeDraw.points", meter.context)).toBe(10_000);
  expect(runInContext("window.__pc10kNativeDraw.links", meter.context)).toBe(100_000);
  for (const method of Object.keys(meter.originals)) {
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(meter.gl), method);
    expect(descriptor?.writable).toBe(true);
    expect(descriptor?.configurable).toBe(true);
  }
  expect(meter.records().map(record => record.operation)).toEqual([1, 2, 3]);
});

test("native cost preserves the exact thrown object even if the diagnostic sink fails", async () => {
  const meter = await harness(true);
  const sentinel = {};
  meter.setThrown(sentinel);
  let caught: unknown;
  try { meter.gl.readPixels(0, 0, 1, 1, 0, 0, 0); } catch (error) { caught = error; }
  expect(caught).toBe(sentinel);
  expect(meter.records()[0].threw).toBe(1);
  meter.failSink();
  try { meter.gl.drawArrays(0, 0, 1); } catch (error) { caught = error; }
  expect(caught).toBe(sentinel);
  meter.setThrown(undefined);
  expect(meter.gl.readPixels(0, 0, 1, 1, 0, 0, 0)).toBe(meter.result);
  expect(meter.calls).toHaveLength(3);
});

test("native cost clock failure cannot prevent the original operation", async () => {
  const meter = await harness(true);
  meter.failClock();
  expect(meter.gl.readPixels(0, 0, 1, 1, 0, 0, 0)).toBe(meter.result);
  expect(meter.calls).toHaveLength(1);
  expect(meter.logs).toEqual([]);
});

test("native cost preserves existing parameter/sync/buffer/flush calls with numeric metadata only", async () => {
  const meter = await harness(true);
  meter.trackParameters();
  const sync = {};
  const destination = new Float32Array([31_337, 99]);
  const invocations = [
    { method: "getParameter", args: [34_016] },
    { method: "clientWaitSync", args: [sync, 0, 0] },
    { method: "getBufferSubData", args: [35_051, 16, destination, 1, 1] },
    { method: "flush", args: [] },
  ] as const;
  for (const call of invocations) expect(meter.gl[call.method](...call.args)).toBe(meter.result);
  expect(meter.calls).toHaveLength(4);
  for (const [index, call] of invocations.entries()) {
    expect(meter.calls[index].receiver).toBe(meter.gl);
    expect(meter.calls[index].method).toBe(call.method);
    expect(meter.calls[index].args).toEqual(call.args);
  }
  expect(meter.calls[1].args[0]).toBe(sync);
  expect(meter.calls[2].args[2]).toBe(destination);
  expect(meter.records().map(record => [record.operation, record.width, record.height, record.target, record.threw]))
    .toEqual([[5, 34_016, 0, 0, 0], [6, 0, 0, 0, 0], [7, 16, 8, 1, 0], [8, 0, 0, 0, 0]]);
  expect(meter.queries).toBe(1); // The one requested query, with no diagnostic query added.
  expect(meter.logs.join(" ")).not.toContain("31337");
});

test("native cost new wrappers retain exact exceptions even when diagnostic logging throws", async () => {
  const meter = await harness(true);
  meter.trackParameters();
  const sentinel = {};
  meter.setThrown(sentinel);
  const invoke = [() => meter.gl.getParameter(34_016), () => meter.gl.clientWaitSync({}, 0, 0),
    () => meter.gl.getBufferSubData(35_051, 0, new Float32Array(4)), () => meter.gl.flush()];
  for (const call of invoke) {
    let caught: unknown;
    try { call(); } catch (error) { caught = error; }
    expect(caught).toBe(sentinel);
  }
  expect(meter.records().map(record => [record.operation, record.threw])).toEqual([[5, 1], [6, 1], [7, 1], [8, 1]]);
  meter.failSink();
  for (const call of invoke) {
    let caught: unknown;
    try { call(); } catch (error) { caught = error; }
    expect(caught).toBe(sentinel);
  }
  expect(meter.calls).toHaveLength(8);
});

test("native cost logs only large CPU-array entry before the native read has returned", async () => {
  const meter = await harness(true);
  meter.setElapsed(0);
  const pixels = new Float32Array(1);
  meter.gl.readPixels(0, 0, 8, 8, 0, 0, pixels); // Exactly 64: not a whole-scene marker.
  meter.gl.readPixels(0, 0, 100, 100, 0, 0, 0); // PBO: no entry marker.
  expect(meter.logs).toEqual([]);
  // Capture logs while the original native function is running, not just once
  // the wrapper returns, so the entry evidence cannot be moved after the call.
  const probe = new Proxy(pixels, { get(target, property) { return Reflect.get(target, property, target); } });
  // A genuine view is required: an object/proxy is not classified as a CPU view.
  meter.gl.readPixels(0, 0, 100, 100, 0, 0, probe);
  expect(meter.logs).toEqual([]);
  let enteredRecords = 0;
  meter.onNative(() => {
    enteredRecords = meter.records().filter(record => record.phase === 0).length;
    expect(enteredRecords).toBe(1);
    expect(meter.records()).toHaveLength(1);
  });
  meter.gl.readPixels(0, 0, 9, 8, 0, 0, pixels);
  expect(enteredRecords).toBe(1);
  expect(meter.records()[0]).toMatchObject({ operation: 1, phase: 0, width: 9, height: 8, target: 1, wallMs: 0 });
});

test("native cost is disabled by default and emits only over 100ms with one shared 64-record cap", async () => {
  const plain = await harness();
  for (const method of ["readPixels", "getParameter", "clientWaitSync", "getBufferSubData", "flush"] as const) {
    expect(plain.gl[method]).toBe(plain.originals[method]);
  }
  plain.gl.readPixels(0, 0, 1, 1, 0, 0, 0);
  plain.task(0, 1_000);
  expect(plain.logs).toEqual([]);
  const meter = await harness(true);
  for (const duration of [0, 99, 100]) {
    meter.setElapsed(duration);
    meter.gl.readPixels(0, 0, 1, 1, 0, 0, 0);
  }
  expect(meter.logs).toEqual([]);
  meter.setElapsed(101);
  for (let index = 0; index < 30; index += 1) meter.gl.readPixels(0, 0, 1, 1, 0, 0, 0);
  for (let index = 0; index < 20; index += 1) meter.gl.drawArrays(0, 0, 1);
  for (let index = 0; index < 20; index += 1) meter.task(index * 1_000, 101);
  meter.gl.drawArraysInstanced(5, 0, 4, 100_000);
  expect(meter.logs).toHaveLength(64);
  expect(new Set(meter.records().map(record => record.operation))).toEqual(new Set([1, 2, 4]));
  expect(meter.records().every(record => Object.values(record).every(value => typeof value === "number" && Number.isFinite(value))))
    .toBe(true);
  expect(meter.calls).toHaveLength(54); // Cap never suppresses a real native operation.
});

test("Node native cost collector is bounded, strict, disposable and independent of page.evaluate", () => {
  let listener: ((message: { text(): string }) => void) | undefined;
  let removed = false;
  const page = {
    on(event: string, callback: typeof listener) { expect(event).toBe("console"); listener = callback; },
    off(event: string, callback: typeof listener) { expect(event).toBe("console"); expect(callback).toBe(listener); removed = true; },
    evaluate() { throw new Error("Collector must never evaluate the page"); },
  } as unknown as Page;
  const collector = captureNativeLongCalls(page);
  const value = { operation: 1, phase: 1, width: 7, height: 7, target: 1, startedMs: 10, endedMs: 111, wallMs: 101, threw: 0 };
  const send = (text: string) => listener!({ text: () => text });
  for (const text of ["unrelated", NATIVE_LONG_CALL_PREFIX + "not json", NATIVE_LONG_CALL_PREFIX + "[]",
    NATIVE_LONG_CALL_PREFIX + "null", NATIVE_LONG_CALL_PREFIX + "x".repeat(1_025),
    ...[{ ...value, url: "must-not-retain" }, { ...value, operation: 9 }, { ...value, phase: 2 }, { ...value, target: 4 },
      { ...value, threw: 2 }, { ...value, wallMs: 100 }, { ...value, width: -2 },
      { ...value, startedMs: -1 }, { ...value, endedMs: 9 }, { ...value, wallMs: "101" },
      { ...value, wallMs: null }, { ...value, wallMs: 102 },
      { ...value, phase: 0, endedMs: 10, wallMs: 0 },
      { ...value, phase: 0, target: 2, width: 100, height: 100, endedMs: 10, wallMs: 0 }]
      .map(record => NATIVE_LONG_CALL_PREFIX + JSON.stringify(record))]) send(text);
  expect(collector.records).toEqual([]);
  const entry = { ...value, phase: 0, width: 100, height: 100, endedMs: 10, wallMs: 0 };
  send(NATIVE_LONG_CALL_PREFIX + JSON.stringify(entry));
  expect(collector.records).toEqual([entry]);
  for (const operation of [5, 6, 7, 8]) send(NATIVE_LONG_CALL_PREFIX + JSON.stringify({ ...value, operation }));
  expect(collector.records.map(record => record.operation)).toEqual([1, 5, 6, 7, 8]);
  for (let index = 0; index < 70; index += 1) send(NATIVE_LONG_CALL_PREFIX + JSON.stringify(value));
  expect(collector.records).toHaveLength(64);
  expect(collector.records[0]).toEqual(entry);
  expect(collector.records[5]).toEqual(value);
  collector.dispose();
  expect(removed).toBe(true);
});

import { expect, type ConsoleMessage, type Page } from "@playwright/test";

export const NATIVE_LONG_CALL_PREFIX = "__DTX_PC10K_NATIVE_COST__";

/** Numeric metadata only; no pixel values, URLs, source identities or credentials. */
export interface NativeLongCallRecord {
  operation: number; // 1 readPixels, 2 drawArrays, 3 instanced draw, 4 long task, 5 getParameter, 6 clientWaitSync, 7 getBufferSubData, 8 flush
  phase: number; // 0 entered a large CPU-array read, 1 completed slow invocation/task
  width: number; // op1 pixels; op5 pname; op6 flags; op7 source byte offset; otherwise 0
  height: number; // op1 pixels; op6 timeout; op7 destination byte capacity (not transferred length); otherwise 0
  target: number; // 0 not a read, 1 CPU array, 2 PBO offset, 3 other read overload
  startedMs: number;
  endedMs: number;
  wallMs: number;
  threw: number;
}

/** Install before navigation: this survives an unrelated pending DOM evaluate. */
export function captureNativeLongCalls(page: Page) {
  const records: NativeLongCallRecord[] = [];
  const keys = ["operation", "phase", "width", "height", "target", "startedMs", "endedMs", "wallMs", "threw"];
  const listener = (message: ConsoleMessage) => {
    const text = message.text();
    if (!text.startsWith(NATIVE_LONG_CALL_PREFIX) || text.length > 1_024 || records.length >= 64) return;
    try {
      const record: unknown = JSON.parse(text.slice(NATIVE_LONG_CALL_PREFIX.length));
      if (record === null || typeof record !== "object" || Array.isArray(record)) return;
      const fields = record as Record<string, unknown>;
      if (Object.keys(fields).length !== keys.length
        || !keys.every(key => typeof fields[key] === "number" && Number.isFinite(fields[key]))) return;
      const value = record as NativeLongCallRecord;
      if (![1, 2, 3, 4, 5, 6, 7, 8].includes(value.operation) || ![0, 1].includes(value.phase) || ![0, 1, 2, 3].includes(value.target)
        || ![0, 1].includes(value.threw) || value.width < -1 || value.height < -1
        || value.startedMs < 0 || value.endedMs < value.startedMs
        || value.wallMs !== value.endedMs - value.startedMs) return;
      if (value.phase === 0
        ? value.operation !== 1 || value.target !== 1 || value.width <= 0 || value.height <= 0
          || value.width * value.height <= 64 || value.wallMs !== 0 || value.threw !== 0
        : value.wallMs <= 100) return;
      records.push(value);
    } catch { /* Ignore non-diagnostic console output without retaining it. */ }
  };
  page.on("console", listener);
  return { records, dispose: () => page.off("console", listener) };
}

export async function installNativeDrawMeter(page: Page, options: { longCallLog?: boolean } = {}) {
  await page.addInitScript(({ longCallLog, logPrefix }) => {
    type Canvas = HTMLCanvasElement | OffscreenCanvas;
    type Renderer = { vendor: string | null; renderer: string | null; version: string | null;
      source: "first WebGL2 getContext result only"; error?: string };
    type QueryTiming = { count: number; sumJsMs: number; maxJsMs: number };
    const state: { points: number; links: number; canvas: Canvas | null;
      renderers: WeakMap<Canvas, Renderer>; queryTimings: WeakMap<Canvas, QueryTiming> } = {
      points: 0, links: 0, canvas: null, renderers: new WeakMap(), queryTimings: new WeakMap(),
    };
    // Explicit diagnostic only. Installed before luma captures native methods,
    // so it times the existing invocation rather than issuing another GL read.
    if (longCallLog) {
      let emitted = 0;
      const emit = (operation: number, width: number, height: number, target: number,
        startedMs: number, endedMs: number, threw: number, phase = 1) => {
        const wallMs = endedMs - startedMs;
        if ((phase === 1 && wallMs <= 100) || emitted >= 64 || !Number.isFinite(wallMs)) return;
        emitted += 1;
        // Instrumentation must never replace the wrapped call's return/throw.
        try { console.info(logPrefix + JSON.stringify({ operation, phase, width, height, target,
          startedMs, endedMs, wallMs, threw })); } catch { /* Diagnostic sink only. */ }
      };
      const prototype = WebGL2RenderingContext.prototype;
      for (const [method, operation] of [["readPixels", 1], ["drawArrays", 2], ["drawArraysInstanced", 3],
        ["getParameter", 5], ["clientWaitSync", 6], ["getBufferSubData", 7], ["flush", 8]] as const) {
        const original = prototype[method] as (...args: unknown[]) => unknown;
        const timed = function (this: WebGL2RenderingContext, ...args: unknown[]) {
          let startedMs: number;
          try { startedMs = performance.now(); }
          catch { return original.apply(this, args); }
          // A single bounded entry marker identifies an in-flight whole-position
          // CPU read even if it has not returned by the failing test's teardown.
          // Do not log starts for tiny label reads, PBO reads or draw calls.
          if (operation === 1 && typeof args[2] === "number" && typeof args[3] === "number"
            && Number.isFinite(args[2]) && Number.isFinite(args[3]) && args[2] > 0 && args[3] > 0
            && args[2] * args[3] > 64 && ArrayBuffer.isView(args[6])) {
            emit(operation, args[2], args[3], 1, startedMs, startedMs, 0, 0);
          }
          let threw = 1;
          try {
            const result = original.apply(this, args);
            threw = 0;
            return result;
          } finally {
            try {
              const endedMs = performance.now();
              const dimension = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : -1;
              let width = 0;
              let height = 0;
              let target = 0;
              if (operation === 1) {
                width = dimension(args[2]); height = dimension(args[3]);
                target = ArrayBuffer.isView(args[6]) ? 1 : typeof args[6] === "number" ? 2 : 3;
              } else if (operation === 5) {
                width = dimension(args[0]);
              } else if (operation === 6) {
                width = dimension(args[1]); height = dimension(args[2]);
              } else if (operation === 7) {
                width = dimension(args[1]);
                // View metadata only, never contents. Capacity is bytes; the
                // optional requested length is elements and can be omitted.
                height = ArrayBuffer.isView(args[2]) ? args[2].byteLength : -1;
                target = ArrayBuffer.isView(args[2]) ? 1 : 3;
              }
              emit(operation, width, height, target, startedMs, endedMs, threw);
            } catch { /* Preserve the original result even if a clock/sink fails. */ }
          }
        };
        const descriptor = Object.getOwnPropertyDescriptor(prototype, method);
        Object.defineProperty(prototype, method, descriptor ? { ...descriptor, value: timed }
          : { configurable: true, writable: true, value: timed });
      }
      // Browser main-thread task duration is a separate diagnostic, not GPU
      // execution time. It uses the same bounded numeric-only sink.
      try {
        if (typeof PerformanceObserver !== "undefined" && PerformanceObserver.supportedEntryTypes.includes("longtask")) {
          new PerformanceObserver(list => {
            for (const entry of list.getEntries()) {
              emit(4, 0, 0, 0, entry.startTime, entry.startTime + entry.duration, 0);
            }
          }).observe({ type: "longtask", buffered: true });
        }
      } catch { /* Optional browser diagnostic support. */ }
    }
    const getContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, ...args: Parameters<typeof getContext>) {
      const context = getContext.apply(this, args);
      if (context instanceof WebGL2RenderingContext && !state.renderers.has(this)) {
        const renderer: Renderer = { vendor: null, renderer: null, version: null,
          source: "first WebGL2 getContext result only" };
        try {
          const info = context.getExtension("WEBGL_debug_renderer_info");
          renderer.vendor = String(context.getParameter(info?.UNMASKED_VENDOR_WEBGL ?? context.VENDOR));
          renderer.renderer = String(context.getParameter(info?.UNMASKED_RENDERER_WEBGL ?? context.RENDERER));
          renderer.version = String(context.getParameter(context.VERSION));
        } catch (error) { renderer.error = String(error); }
        state.renderers.set(this, renderer);
      }
      return context;
    } as typeof getContext;
    const ownCanvas = (canvas: HTMLCanvasElement | OffscreenCanvas) => {
      if (state.canvas !== canvas) Object.assign(state, { canvas, points: 0, links: 0 });
    };
    Object.assign(window, { __pc10kNativeDraw: state });
    const defaultFramebuffer = (context: WebGL2RenderingContext) => {
      const started = performance.now();
      // Time only the existing JS method invocation. Luma may return cached
      // state; this is not a GPU synchronization or GPU execution measure.
      const framebuffer = context.getParameter(context.DRAW_FRAMEBUFFER_BINDING);
      const elapsed = performance.now() - started;
      const timing = state.queryTimings.get(context.canvas) ?? { count: 0, sumJsMs: 0, maxJsMs: 0 };
      timing.count += 1;
      timing.sumJsMs += elapsed;
      timing.maxJsMs = Math.max(timing.maxJsMs, elapsed);
      state.queryTimings.set(context.canvas, timing);
      return framebuffer === null;
    };
    const draw = WebGL2RenderingContext.prototype.drawArrays;
    WebGL2RenderingContext.prototype.drawArrays = function (mode, first, count) {
      if (mode === this.POINTS && defaultFramebuffer(this)) {
        ownCanvas(this.canvas);
        state.points = count;
      }
      return draw.call(this, mode, first, count);
    };
    const instanced = WebGL2RenderingContext.prototype.drawArraysInstanced;
    WebGL2RenderingContext.prototype.drawArraysInstanced = function (mode, first, count, instances) {
      if (mode === this.TRIANGLE_STRIP && count === 4 && defaultFramebuffer(this)) {
        ownCanvas(this.canvas);
        state.links = instances;
      }
      return instanced.call(this, mode, first, count, instances);
    };
  }, { longCallLog: options.longCallLog === true, logPrefix: NATIVE_LONG_CALL_PREFIX });
}

/** Cached metadata and timing only: this issues no GL query or position read. */
export function readNativeDrawDiagnostics(page: Page) {
  return page.evaluate(() => {
    type Canvas = HTMLCanvasElement | OffscreenCanvas;
    const state = (window as unknown as { __pc10kNativeDraw: {
      points: number; links: number; canvas: Canvas | null;
      renderers: WeakMap<Canvas, { vendor: string | null; renderer: string | null; version: string | null; source: string; error?: string }>;
      queryTimings: WeakMap<Canvas, { count: number; sumJsMs: number; maxJsMs: number }>;
    } }).__pc10kNativeDraw;
    return {
      points: state.points, links: state.links,
      currentCanvas: state.canvas === document.querySelector('[data-testid="graph-canvas"] canvas'),
      renderer: state.canvas ? state.renderers.get(state.canvas) ?? null : null,
      framebufferQueryJsTiming: state.canvas ? state.queryTimings.get(state.canvas) ?? null : null,
      timingScope: "Existing getParameter JS method duration, possibly cached; not GPU execution or synchronization time",
    };
  });
}

export function readNativeDraw(page: Page) {
  return page.evaluate(() => {
    const state = (window as unknown as { __pc10kNativeDraw: {
      points: number; links: number; canvas: HTMLCanvasElement | OffscreenCanvas | null;
    } }).__pc10kNativeDraw;
    return { points: state.points, links: state.links,
      currentCanvas: state.canvas === document.querySelector('[data-testid="graph-canvas"] canvas') };
  });
}

export async function expectNativeCounts(page: Page, points: number, links: number) {
  await expect.poll(() => readNativeDraw(page), { message: "Native GL must draw the admitted original 10K scene" })
    .toEqual({ points, links, currentCanvas: true });
}

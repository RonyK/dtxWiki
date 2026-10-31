import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";

import { assertExact10kMeasurement, assertFreshNativeSceneProof, assertPc10kAcceptance, installGraphMeasurement,
  interactiveFrameBoundaries, mapBenchmarkBudget, readCurrentGraphGpuMetadata,
  selectMapBenchmarkScale } from "./render_contract.mjs";
import { installReadPixelsTiming, summarizeReadPixels, validateMotionProfileOptions, validateStartupProfileOptions } from "./render_profile.mjs";
import { measureNodeInteractions } from "./render_motion.mjs";
import { neutralizeNodeInteraction, waitForNeutralNodePublication } from "./render_interaction_setup.mjs";

function exactMeasurement() {
  return {
    request: { documents: 10_000, relations: 100_000 },
    response: { nodes: 10_000, edges: 30_895, documents: 10_000, clusters: 0, aggregated_edges: 0,
      candidate_documents: 10_000, candidate_relations: 30_895,
      representation: "exact", completeness: "complete", truncated: false },
    columns: { nodes: 10_000, edges: 30_895 },
    native: { node_vertices: 10_000, edge_instances: 30_895 },
    expectedRelations: 30_895,
  };
}

test("the map scale changes only the explicit PC request budget", () => {
  assert.deepEqual(mapBenchmarkBudget("2k"), { documents: 2_000, relations: 6_000 });
  assert.deepEqual(mapBenchmarkBudget("10k"), { documents: 10_000, relations: 100_000 });
  assert.throws(() => mapBenchmarkBudget("20k"), /--map-scale/);
  const budget = mapBenchmarkBudget("10k");
  budget.documents = 20_000;
  assert.equal(mapBenchmarkBudget("10k").documents, 10_000);
});

test("duplicate native submissions cannot make 50ms RAF frames pass a 33ms budget", () => {
  const rafs = [0, 50, 100, 150, 200];
  const native = Array.from({ length: 4 }, (_, frame) =>
    Array.from({ length: 30 }, (_, index) => frame * 50 + 1 + index * 0.1)).flat();
  const result = interactiveFrameBoundaries(rafs, native, [{ start_ms: 0, end_ms: 200 }]);
  assert.equal(result.rendered_raf_boundaries.length, 4);
  assert.deepEqual(result.interactive_raf_intervals, [50, 50, 50, 50]);
  const raw = [...result.interactive_native_intervals].sort((left, right) => left - right);
  assert.ok(raw[Math.ceil(raw.length * 0.95) - 1] < 1, "Native submission p95 is misleadingly fast");
  const sample = acceptedPcSample();
  sample.motion.interactive_raf_frame_interval_ms.p95 = 50;
  assert.throws(() => assertPc10kAcceptance(sample), /33ms/);
});

test("idle RAF callbacks without an actual full-scene native draw are not frame proof", () => {
  const result = interactiveFrameBoundaries([0, 16, 32, 48, 64], [], [{ start_ms: 0, end_ms: 64 }]);
  assert.deepEqual(result.rendered_raf_boundaries, []);
  assert.deepEqual(result.interactive_raf_intervals, []);
  assert.equal(result.missing_interaction_windows.length, 1);
});

test("drawn RAF slots preserve real gaps and exclude inactive-window submissions", () => {
  const result = interactiveFrameBoundaries([0, 16, 32, 48, 64, 80, 96],
    [1, 2, 17, 18, 40, 41, 81, 82], [{ start_ms: 0, end_ms: 32 }, { start_ms: 64, end_ms: 96 }]);
  assert.deepEqual(result.interactive_raf_intervals, [16, 16, 32]);
  assert.equal(result.missing_interaction_windows.length, 0);
  assert.equal(result.rendered_raf_boundaries.length, 4);
});

test("a window with no native response remains a missing interaction even with surrounding frames", () => {
  const result = interactiveFrameBoundaries([0, 16, 32, 48, 64, 80, 96], [1, 17, 81],
    [{ start_ms: 0, end_ms: 32 }, { start_ms: 32, end_ms: 64 }, { start_ms: 64, end_ms: 96 }]);
  assert.deepEqual(result.missing_interaction_windows, [{ start_ms: 32, end_ms: 64 }]);
  assert.deepEqual(result.interactive_raf_intervals, [16, 16, 64]);
});

test("native complete 10K representative and 100K maximum scenes are accepted", () => {
  assert.doesNotThrow(() => assertExact10kMeasurement(exactMeasurement()));
  const maximum = exactMeasurement();
  maximum.response.edges = maximum.response.candidate_relations = 100_000;
  maximum.columns.edges = maximum.native.edge_instances = maximum.expectedRelations = 100_000;
  assert.doesNotThrow(() => assertExact10kMeasurement(maximum));
});

function acceptedPcSample() {
  return {
    gpu: { renderer: "ANGLE Intel UHD 770 Direct3D11", unmasked_renderer: "ANGLE Intel UHD 770 Direct3D11",
      source: "current-graph-canvas", context_type: "webgl2", identification: "unmasked", webgl: true },
    map_measurement: { response: { edges: 30_895 } },
    final_native_proof: { started_at_ms: 1, finished_at_ms: 2, node_vertex_counts: [1, 10_000], edge_instance_counts: [30_895] },
    graph_timing: { response_end_to_first_frame_ms: 500 },
    motion: { initial: { moved_node_count: 10 }, interactive_raf_frame_interval_ms: { p95: 16.7 },
      missing_interaction_windows: [],
      pan_trials: [{ changed: true }] },
    interaction: {
      samples: Array.from({ length: 10 }, () => [
        { kind: "hover", status: "hit", latency_ms: 80 },
        { kind: "selection", status: "hit", latency_ms: 50 },
      ]).flat(),
      hover: { attempted: 10, missed: 0, latency_ms: { p95: 80 } },
      selection: { attempted: 10, missed: 0, latency_ms: { p95: 50 } },
    },
  };
}

test("PC acceptance requires all hardware, native motion and interaction budgets", () => {
  assert.doesNotThrow(() => assertPc10kAcceptance(acceptedPcSample()));
});

test("PC hardware acceptance rejects masked renderer metadata rather than guessing hardware", () => {
  const sample = acceptedPcSample();
  sample.gpu = { renderer: "WebKit WebGL", vendor: "WebKit", webgl: true,
    source: "current-graph-canvas", context_type: "webgl2", identification: "masked" };
  assert.throws(() => assertPc10kAcceptance(sample), /identified hardware GPU/);
});

test("PC hardware acceptance does not retroactively qualify old separate-canvas GPU evidence", () => {
  const sample = acceptedPcSample();
  sample.gpu = { renderer: "ANGLE Intel UHD 770 Direct3D11", vendor: "Google Inc. (Intel)", webgl: true };
  assert.throws(() => assertPc10kAcceptance(sample), /identified hardware GPU/);
});

for (const [label, mutate] of [
  ["missing current-canvas provenance", gpu => { delete gpu.source; }],
  ["a separate probe canvas", gpu => { gpu.source = "separate-canvas"; }],
  ["a WebGL1 context", gpu => { gpu.context_type = "webgl"; }],
  ["an unavailable context", gpu => { gpu.webgl = false; }],
  ["missing unmasked provenance", gpu => { delete gpu.identification; }],
  ["masked-only provenance", gpu => { gpu.identification = "masked"; }],
  ["missing unmasked name", gpu => { delete gpu.unmasked_renderer; }],
  ["a name unlike the unmasked observation", gpu => { gpu.unmasked_renderer = "Other GPU"; }],
  ...["", "   ", "WebKit WebGL", "WebGL 2", "ANGLE", "Unknown GPU", "Generic Renderer",
    "ANGLE (SwiftShader Device)", "llvmpipe (LLVM 15.0.7)", "softpipe", "lavapipe", "swrast",
    "Microsoft Basic Render Driver", "GDI Generic", "Mesa Offscreen"].map(renderer => [renderer || "empty renderer", gpu => {
      gpu.renderer = gpu.unmasked_renderer = renderer;
    }]),
]) {
  test(`PC hardware qualification rejects ${label}`, () => {
    const sample = acceptedPcSample();
    mutate(sample.gpu);
    assert.throws(() => assertPc10kAcceptance(sample), /identified hardware GPU/);
  });
}

test("PC hardware qualification has no OS or hardware-manufacturer allowlist", () => {
  const sample = acceptedPcSample();
  sample.gpu.renderer = sample.gpu.unmasked_renderer = "ExampleGPU Model 42 (OpenGL 4.6)";
  assert.doesNotThrow(() => assertPc10kAcceptance(sample));
});

function gpuMetadataBrowser(options = {}) {
  const readySelector = ".current-native-map";
  const calls = [];
  const canvas = { isConnected: true, getContext() { assert.fail("Do not request or allocate a context"); } };
  const decoyCanvas = { isConnected: true };
  const extension = { UNMASKED_RENDERER_WEBGL: 37446, UNMASKED_VENDOR_WEBGL: 37445 };
  class NativeGL {
    RENDERER = 7937;
    VENDOR = 7936;
    constructor(owner, renderer) { this.canvas = owner; this.renderer = renderer; }
    isContextLost() { return options.lost ?? false; }
    getExtension(name) {
      assert.equal(name, "WEBGL_debug_renderer_info");
      calls.push(["extension", this.canvas]);
      return options.masked ? null : extension;
    }
    getParameter(parameter) {
      calls.push(["parameter", this.canvas, parameter]);
      if (options.queryError) throw new Error("metadata unavailable");
      if (parameter === this.RENDERER) return "WebKit WebGL";
      if (parameter === this.VENDOR) return "WebKit";
      if (parameter === extension.UNMASKED_RENDERER_WEBGL) return this.renderer;
      if (parameter === extension.UNMASKED_VENDOR_WEBGL) return "ExampleGPU Vendor";
      assert.fail(`Unexpected GL query ${parameter}`);
    }
  }
  class GL extends NativeGL {}
  class LegacyGL extends NativeGL {}
  const native = new (options.legacy ? LegacyGL : GL)(canvas, "ExampleGPU Model 42 (OpenGL 4.6)");
  const decoy = new GL(decoyCanvas, "ANGLE SwiftShader");
  const legacyCanvas = { isConnected: true };
  const legacy = new LegacyGL(legacyCanvas, "LegacyGPU Model 7");
  const nativeContexts = new WeakMap([[canvas, native], [decoyCanvas, decoy], [legacyCanvas, legacy]]);
  // Sigma roots contain multiple layered canvases, including 2D or idle ones
  // before the actually drawn GL layer. An observed other-root canvas exists
  // too, but must never become fallback metadata for this graph.
  let currentCanvases = [{ isConnected: true }, canvas];
  const context = vm.createContext({
    document: {
      querySelector(selector) {
        assert.equal(selector, readySelector);
        return { querySelectorAll(child) { assert.equal(child, "canvas"); return currentCanvases; } };
      },
      createElement() { assert.fail("GPU metadata must not allocate a separate canvas"); },
    },
    window: { __dtxGraphRenderBench: { nativeContexts }, navigator: { userAgent: "fixture", platform: "any OS" },
      devicePixelRatio: 1, innerWidth: 1440, innerHeight: 900, matchMedia: () => ({ matches: false }) },
    WebGL2RenderingContext: GL,
    WebGLRenderingContext: LegacyGL,
  });
  return { calls, canvas, legacyCanvas, native, nativeContexts,
    setCurrentCanvases(canvases) { currentCanvases = canvases; },
    read: () => JSON.parse(vm.runInContext(`JSON.stringify((${readCurrentGraphGpuMetadata.toString()})(${JSON.stringify(readySelector)}))`, context)) };
}

test("GPU metadata reads only the current already-drawn WebGL2 context without allocating a canvas or context", () => {
  const browser = gpuMetadataBrowser();
  const result = browser.read();
  assert.deepEqual({ renderer: result.renderer, vendor: result.vendor, source: result.source,
    context_type: result.context_type, identification: result.identification, webgl: result.webgl }, {
    renderer: "ExampleGPU Model 42 (OpenGL 4.6)", vendor: "ExampleGPU Vendor", source: "current-graph-canvas",
    context_type: "webgl2", identification: "unmasked", webgl: true,
  });
  assert.equal(result.masked_renderer, "WebKit WebGL");
  assert.equal(result.unmasked_renderer, result.renderer);
  assert.ok(browser.calls.length > 0);
  assert.ok(browser.calls.every(call => call[1] === browser.canvas), "Never query the unrelated context");
  const sample = acceptedPcSample();
  sample.gpu = result;
  assert.doesNotThrow(() => assertPc10kAcceptance(sample));
});

test("GPU metadata retains masked renderer diagnostics but cannot qualify them", () => {
  const result = gpuMetadataBrowser({ masked: true }).read();
  assert.equal(result.renderer, "WebKit WebGL");
  assert.equal(result.masked_renderer, "WebKit WebGL");
  assert.equal(result.unmasked_renderer, null);
  assert.equal(result.identification, "masked");
  const sample = acceptedPcSample();
  sample.gpu = result;
  assert.throws(() => assertPc10kAcceptance(sample), /identified hardware GPU/);
});

test("GPU metadata never falls back to another graph's observed context when current-root layers have no context", () => {
  const browser = gpuMetadataBrowser();
  browser.setCurrentCanvases([{ isConnected: true }]);
  const result = browser.read();
  assert.equal(result.identification, "unavailable");
  assert.equal(result.context_type, null);
  assert.equal(browser.calls.length, 0);
});

test("GPU metadata prefers the actual current WebGL2 layer when a ready root also contains a WebGL1 layer", () => {
  const browser = gpuMetadataBrowser();
  browser.setCurrentCanvases([browser.legacyCanvas, browser.canvas]);
  const result = browser.read();
  assert.equal(result.context_type, "webgl2");
  assert.equal(result.renderer, "ExampleGPU Model 42 (OpenGL 4.6)");
  assert.ok(browser.calls.every(call => call[1] === browser.canvas));
});

test("Local WebGL1 fallback keeps actual stacked-canvas GPU diagnostics but cannot qualify PC10K", () => {
  const result = gpuMetadataBrowser({ legacy: true }).read();
  assert.equal(result.context_type, "webgl");
  assert.equal(result.webgl, true);
  assert.equal(result.identification, "unmasked");
  assert.equal(result.renderer, "ExampleGPU Model 42 (OpenGL 4.6)");
  const sample = acceptedPcSample();
  sample.gpu = result;
  assert.throws(() => assertPc10kAcceptance(sample), /identified hardware GPU/);
});

for (const state of ["not observed", "detached", "wrong canvas", "context lost", "query error"]) {
  test(`GPU metadata refuses qualification when the current canvas is ${state}`, () => {
    const browser = gpuMetadataBrowser({ lost: state === "context lost", queryError: state === "query error" });
    if (state === "not observed") browser.nativeContexts.delete(browser.canvas);
    if (state === "detached") browser.canvas.isConnected = false;
    if (state === "wrong canvas") browser.native.canvas = {};
    const result = browser.read();
    assert.equal(result.identification, "unavailable");
    if (state !== "query error") assert.equal(browser.calls.length, 0);
    const sample = acceptedPcSample();
    sample.gpu = result;
    assert.throws(() => assertPc10kAcceptance(sample), /identified hardware GPU/);
  });
}

for (const [label, mutate] of [
  ["software GPU", (s) => { s.gpu.renderer = "ANGLE SwiftShader"; }],
  ["missing response-end timing", (s) => { s.graph_timing.response_end_to_first_frame_ms = null; }],
  ["slow first frame", (s) => { s.graph_timing.response_end_to_first_frame_ms = 2_001; }],
  ["slow interaction frames", (s) => { s.motion.interactive_raf_frame_interval_ms.p95 = 33.1; }],
  ["RAF-only proof", (s) => { s.motion.interactive_raf_frame_interval_ms = null; }],
  ["user interaction without a native frame", (s) => { s.motion.missing_interaction_windows.push({ start_ms: 0, end_ms: 250 }); }],
  ["camera-only movement", (s) => { s.motion.initial.moved_node_count = 0; }],
  ["no actual pan", (s) => { s.motion.pan_trials[0].changed = false; }],
  ["missed hover removed from latency denominator", (s) => { s.interaction.hover.missed = 1; }],
  ["too few points", (s) => { s.interaction.hover.attempted = 9; }],
  ["slow hover", (s) => { s.interaction.hover.latency_ms.p95 = 100.1; }],
  ["negative frame-origin latency", (s) => { s.interaction.hover.latency_ms.p95 = -1; }],
  ["one negative hit hidden by p95", (s) => { s.interaction.samples[0].latency_ms = -1; }],
  ["one nonfinite hit hidden by p95", (s) => { s.interaction.samples[0].latency_ms = NaN; }],
  ["omitted raw misses", (s) => { s.interaction.samples.pop(); }],
  ["missing selection timing", (s) => { s.interaction.selection.latency_ms.p95 = null; }],
]) {
  test(`PC performance evidence rejects ${label}`, () => {
    const sample = acceptedPcSample();
    mutate(sample);
    assert.throws(() => assertPc10kAcceptance(sample));
  });
}

for (const [label, mutate] of [
  ["default 2K budget", (m) => { m.request = { documents: 2_000, relations: 6_000 }; }],
  ["missing original request", (m) => { m.request = null; }],
  ["aggregated response", (m) => { m.response.representation = "aggregated"; }],
  ["capped response", (m) => { m.response.completeness = "capped"; }],
  ["truncated response", (m) => { m.response.truncated = true; }],
  ["cluster replaced document", (m) => { m.response.clusters = 1; }],
  ["aggregated edge", (m) => { m.response.aggregated_edges = 1; }],
  ["sampled documents", (m) => { m.response.nodes = m.response.documents = 2_000; }],
  ["omitted candidate relation", (m) => { m.response.candidate_relations += 1; }],
  ["wrong corpus", (m) => { m.expectedRelations = 100_000; }],
  ["hidden unlinked documents", (m) => { m.columns.nodes = 9_660; }],
  ["missing visible relationship", (m) => { m.columns.edges -= 1; }],
  ["DOM count without native node draw", (m) => { m.native.node_vertices = null; }],
  ["native sampled points", (m) => { m.native.node_vertices = 2_000; }],
  ["native relationship omitted", (m) => { m.native.edge_instances -= 1; }],
]) {
  test(`10K evidence rejects ${label}`, () => {
    const measurement = exactMeasurement();
    mutate(measurement);
    assert.throws(() => assertExact10kMeasurement(measurement));
  });
}

test("10K selection uses the production control and waits for its exact budget", async () => {
  const events = [];
  let predicate;
  const control = {
    async waitFor() { events.push("visible"); },
    async selectOption(value) { events.push(`select:${value}`); },
  };
  const page = {
    locator(selector) {
      assert.equal(selector, "details.workspace-graph-view-options");
      return { locator(child) {
        return child === "summary" ? { async click() { events.push("view"); } } : control;
      } };
    },
    async evaluate() { events.push("reset-measurement"); },
    async waitForResponse(callback) { predicate = callback; events.push("observe-request"); },
  };
  await selectMapBenchmarkScale(page, "10k");
  assert.deepEqual(events, ["view", "visible", "reset-measurement", "observe-request", "select:10k", "view"]);
  const response = (budget, method = "POST", path = "/api/v1/workspaces/a/graph/overview/query") => ({
    url: () => `http://localhost${path}`,
    request: () => ({ method: () => method, postDataJSON: () => ({ budget }) }),
  });
  assert.equal(predicate(response(mapBenchmarkBudget("10k"))), true);
  assert.equal(predicate(response(mapBenchmarkBudget("2k"))), false);
  assert.equal(predicate(response(mapBenchmarkBudget("10k"), "GET")), false);
  assert.equal(predicate(response(mapBenchmarkBudget("10k"), "POST", "/graph/query")), false);
});

test("2K and local graph measurements never activate a 10K control", async () => {
  await selectMapBenchmarkScale({ locator() { assert.fail("unexpected UI activation"); } }, "2k");
});

test("a missing PC scale control fails instead of overriding the mobile policy", async () => {
  let selected = false;
  const page = { locator: () => ({ locator: (selector) => selector === "summary"
    ? { async click() {} }
    : { async waitFor() { throw new Error("control unavailable"); }, async selectOption() { selected = true; } } }) };
  await assert.rejects(selectMapBenchmarkScale(page, "10k"), /control unavailable/);
  assert.equal(selected, false);
});

function measurementBrowser() {
  let now = 0;
  const frames = [];
  let ready = false;
  let labels = [];
  let links = [];
  const listeners = new Map();
  const observers = [];
  const timers = new Map();
  let nextTimer = 0;
  class Canvas {
    tagName = "CANVAS";
    width = 900;
    height = 600;
    style = {};
    __zoom = { x: 0, y: 0, k: 1 };
    closest() { return {}; }
    getBoundingClientRect() { return { x: 100, y: 200, width: 900, height: 600 }; }
  }
  const canvas = new Canvas();
  let pointerTarget = canvas;
  class GL {
    FRAMEBUFFER = 36160;
    DRAW_FRAMEBUFFER = 36009;
    READ_FRAMEBUFFER = 36008;
    POINTS = 0;
    TRIANGLE_STRIP = 5;
    canvas = canvas;
    bindFramebuffer() {}
    drawArrays() {}
    drawArraysInstanced() {}
    readPixels(...args) { this.lastReadArguments = args; return "native-result"; }
  }
  class LegacyGL {
    FRAMEBUFFER = 36160;
    TRIANGLES = 4;
    canvas = canvas;
    bindFramebuffer() { return "native-legacy-bind"; }
    drawArrays() { return "native-legacy-draw"; }
    drawElements() { return "native-legacy-elements"; }
    instancing = {
      drawArraysInstancedANGLE(...args) { this.arguments = args; return "native-legacy-instanced"; },
      drawElementsInstancedANGLE(...args) { this.arguments = args; return "native-legacy-indexed-instanced"; },
    };
    getExtension(name) { return name === "ANGLE_instanced_arrays" ? this.instancing : null; }
  }
  class XHR { open() {} send() {} }
  const payload = { data: { nodes: [], edges: [] } };
  const window = { fetch: async () => ({ async json() { now += 3; return payload; } }) };
  const context = vm.createContext({
    window,
    document: {
      documentElement: {},
      elementFromPoint: () => pointerTarget,
      querySelector: (selector) => selector === ".workspace-graph .graph-canvas canvas" ? canvas
        : ready ? { querySelector: () => canvas } : null,
      querySelectorAll: (selector) => selector.includes("span")
        ? selector.includes('[data-full-title="true"]') ? labels.filter((label) => label.dataset.fullTitle === "true") : labels
        : links,
      addEventListener: (type, listener, capture = false) => {
        const registered = listeners.get(type) ?? [];
        registered.push({ listener, capture }); listeners.set(type, registered);
      },
      removeEventListener: (type, listener) => listeners.set(type, (listeners.get(type) ?? []).filter(item => item.listener !== listener)),
    },
    performance: { now: () => ++now },
    requestAnimationFrame: (callback) => frames.push(callback),
    MutationObserver: class {
      constructor(callback) { this.callback = callback; observers.push(callback); }
      observe() {}
      disconnect() { const index = observers.indexOf(this.callback); if (index >= 0) observers.splice(index, 1); }
    },
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    PerformanceObserver: class { static supportedEntryTypes = []; },
    XMLHttpRequest: XHR,
    HTMLCanvasElement: Canvas,
    WebGL2RenderingContext: GL,
    WebGLRenderingContext: LegacyGL,
  });
  vm.runInContext(`(${installGraphMeasurement.toString()})();`, context);
  return {
    state: window.__dtxGraphRenderBench, window, payload, gl: new GL(), legacyGl: new LegacyGL(), canvas, frames,
    setReady() { ready = true; },
    setLabels(next) { labels = next; },
    getLabels() { return labels; },
    setPointerTarget(next) { pointerTarget = next; },
    run(callback, args) {
      context.__benchUnitArgs = args;
      try { return vm.runInContext(`(${callback.toString()})(...__benchUnitArgs)`, context); }
      finally { delete context.__benchUnitArgs; }
    },
    setLinks(next) { links = next; },
    publishPoint(label) { for (const observer of [...observers]) observer([{ attributeName: "data-point-x", target: label }]); },
    fireTimers() { for (const [id, timer] of [...timers]) { timers.delete(id); timer.callback(); } },
    advanceNow(duration) { now += duration; },
    timers, observerCount: () => observers.length,
    pointer(type, event, onlyCapture = null) {
      for (const capture of [true, false]) {
        if (onlyCapture !== null && capture !== onlyCapture) continue;
        for (const registered of listeners.get(type) ?? []) if (registered.capture === capture) {
          registered.listener({ type, isTrusted: true, ...event, target: canvas });
        }
      }
    },
    installPixels() { vm.runInContext(`(${installReadPixelsTiming.toString()})();`, context); },
  };
}

test("measurement excludes simulation/picking framebuffer counts and records visible draws", () => {
  const { state, gl, canvas, frames, setReady } = measurementBrowser();
  state.targetMapNodes = 10_000;
  state.targetMapEdges = 30_895;
  setReady();
  gl.bindFramebuffer(gl.FRAMEBUFFER, {});
  gl.drawArrays(gl.POINTS, 0, 10_000);
  gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, 30_895);
  assert.equal(state.nativeDraws.node_vertices, null);
  assert.equal(state.nativeDraws.edge_instances, null);
  assert.equal(state.nativeContexts.get(canvas), undefined, "Simulation/picking contexts are not native renderer provenance");
  assert.equal(frames.length, 0);
  gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, null);
  gl.drawArrays(gl.POINTS, 0, 2_000);
  assert.equal(state.nativeContexts.get(canvas), gl, "Retain the actual current native WebGL2 draw context");
  assert.equal(frames.length, 0);
  gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, 30_895);
  gl.drawArrays(gl.POINTS, 0, 10_000);
  assert.equal(state.nativeDraws.node_vertices, 10_000);
  assert.equal(state.nativeDraws.edge_instances, 30_895);
  assert.equal(frames.length, 1);
  frames.shift()(123);
  assert.ok(state.firstCanvasFrameAt > state.nativeDraws.first_scene_draw_at);
});

test("current-context capture includes non-POINTS WebGL2 Local draws without changing map count semantics", () => {
  const { state, gl, canvas } = measurementBrowser();
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  gl.drawArraysInstanced(4, 0, 3, 250);
  assert.equal(state.nativeContexts.get(canvas), gl);
  assert.equal(state.nativeDraws.node_vertices, null);
  assert.equal(state.nativeDraws.edge_instances, null);
});

test("Local WebGL1 native draws retain actual-context diagnostics without entering the WebGL2 map meter", () => {
  const { state, legacyGl, canvas } = measurementBrowser();
  assert.equal(legacyGl.bindFramebuffer(legacyGl.FRAMEBUFFER, {}), "native-legacy-bind");
  assert.equal(legacyGl.drawArrays(legacyGl.TRIANGLES, 0, 750), "native-legacy-draw");
  assert.equal(state.nativeContexts.get(canvas), undefined, "Offscreen picking is not a visible context submission");
  legacyGl.bindFramebuffer(legacyGl.FRAMEBUFFER, null);
  assert.equal(legacyGl.drawElements(legacyGl.TRIANGLES, 750, 5123, 0), "native-legacy-elements");
  assert.equal(state.nativeContexts.get(canvas), legacyGl);
  assert.equal(state.nativeDraws.node_vertices, null);
  assert.equal(state.nativeDraws.edge_instances, null);
});

test("Local WebGL1 ANGLE-instanced shapes retain native provenance without replacing extension identity or draw results", () => {
  const { state, legacyGl, canvas } = measurementBrowser();
  const extension = legacyGl.getExtension("ANGLE_instanced_arrays");
  assert.equal(extension, legacyGl.instancing);
  const observedDraw = extension.drawArraysInstancedANGLE;
  assert.equal(legacyGl.getExtension("ANGLE_instanced_arrays").drawArraysInstancedANGLE, observedDraw,
    "Repeated reads of an existing extension must not repeatedly wrap its draws");
  legacyGl.bindFramebuffer(legacyGl.FRAMEBUFFER, {});
  assert.equal(extension.drawArraysInstancedANGLE(4, 0, 3, 250), "native-legacy-instanced");
  assert.equal(state.nativeContexts.get(canvas), undefined);
  legacyGl.bindFramebuffer(legacyGl.FRAMEBUFFER, null);
  assert.equal(extension.drawElementsInstancedANGLE(4, 6, 5123, 0, 250), "native-legacy-indexed-instanced");
  assert.deepEqual(extension.arguments, [4, 6, 5123, 0, 250]);
  assert.equal(state.nativeContexts.get(canvas), legacyGl);
  assert.equal(state.nativeDraws.node_vertices, null);
  assert.equal(state.nativeDraws.edge_instances, null);
});

test("response timing reuses the app's decoded object and never records auth bodies", async () => {
  const { state, window, payload } = measurementBrowser();
  await window.fetch("/api/v1/auth/login", { method: "POST", body: '{"password":"secret-never-retained"}', headers: { Authorization: "secret-token" } });
  const response = await window.fetch("/api/v1/workspaces/a/graph/overview/query", {
    method: "POST", body: JSON.stringify({ budget: mapBenchmarkBudget("10k") }),
  });
  assert.equal(await response.json(), payload);
  assert.equal(state.requests[1].scene, payload.data);
  assert.equal(state.requests[1].budget.documents, 10_000);
  assert.ok(state.requests[1].json_end_ms > state.requests[1].json_start_ms);
  assert.ok(state.requests[1].headers_at_ms > state.requests[1].start_ms);
  assert.equal(JSON.stringify(state).includes("secret-"), false);
  assert.equal("budget" in state.requests[0], false);
});

function trackedLabel(id, x, y, full = false) {
  return {
    dataset: { nodeId: id, pointX: String(x), pointY: String(y), fullTitle: String(full), labelPriority: full ? "active" : "ordinary" },
    hasAttribute: (attribute) => attribute === "data-point-x" || attribute === "data-point-y",
    getClientRects: () => [{}],
  };
}

function interactionPage({ afterHover = () => {}, hoverHit = false, nodes = null, afterSelection = () => {} } = {}) {
  const browser = measurementBrowser();
  const intended = trackedLabel("intended", 20, 30);
  const neighbor = trackedLabel("neighbor", 80, 90);
  browser.setLabels(nodes ?? [intended, neighbor]);
  browser.state.requests.push({ budget: mapBenchmarkBudget("10k"), scene: { nodes: [
    { id: "intended", label: "Original intended document", path: "/original" },
    { id: "neighbor", label: "Neighbor", path: "/neighbor" },
  ] } });
  for (const label of browser.getLabels()) browser.publishPoint(label);
  const moves = [], clicks = [], waits = [];
  let captures = 0;
  const container = { getBoundingClientRect: () => browser.canvas.getBoundingClientRect(),
    querySelectorAll: () => browser.getLabels(), querySelector: () => browser.canvas };
  const evaluate = (callback, args) => {
    const value = browser.run(callback, args);
    return value?.then ? value.then(result => structuredClone(result)) : structuredClone(value);
  };
  const page = {
    locator(selector) {
      return { first() { return this; }, async scrollIntoViewIfNeeded() {}, async count() { return 0; },
        async evaluate(callback, argument) { captures += 1; return evaluate(callback, [container, argument]); } };
    },
    async evaluate(callback, argument) { return evaluate(callback, [argument]); },
    getByRole() { return { async focus() {} }; },
    mouse: {
      async move(x, y) {
        moves.push([x, y]);
        browser.pointer("pointermove", { clientX: x, clientY: y, offsetX: x - 100, offsetY: y - 200 });
      },
      async click(x, y) {
        clicks.push([x, y]);
        browser.pointer("pointerdown", { clientX: x, clientY: y, offsetX: x - 100, offsetY: y - 200 });
        if (x === 100 + Number(intended.dataset.pointX) && y === 200 + Number(intended.dataset.pointY)) {
          browser.state.pendingInteraction.end_ms = browser.state.pendingInteraction.start_ms + 20;
        }
      },
    },
    async waitForFunction(callback, _argument, options) {
      waits.push({ kind: browser.state.pendingInteraction.kind, ...options });
      if (browser.state.pendingInteraction.kind === "hover") {
        afterHover({ browser, intended, neighbor });
        if (hoverHit) browser.state.pendingInteraction.end_ms = browser.state.pendingInteraction.start_ms + 20;
      }
      if (!evaluate(callback, [])) throw new Error("Controlled one-attempt miss, without waiting or retries");
      if (browser.state.pendingInteraction.kind === "selection") afterSelection({ browser, intended, neighbor });
    },
  };
  return { page, browser, moves, clicks, waits, captures: () => captures };
}

test("actual interaction measurement refreshes selection coordinates for the same ID after a missed hover", async () => {
  const fixture = interactionPage({ afterHover({ intended }) { intended.dataset.pointX = "35"; intended.dataset.pointY = "45"; } });
  const samples = await measureNodeInteractions(fixture.page, ".workspace-graph", 1);
  assert.deepEqual(fixture.moves, [[0, 0], [120, 230]], "Original hover is issued once at its original capture");
  assert.deepEqual(fixture.clicks, [[135, 245]], "Selection must use the same original ID's refreshed point");
  assert.equal(fixture.captures(), 3);
  assert.equal(samples.length, 2);
  assert.equal(samples[0].status, "missed", "Fresh selection cannot erase the original hover miss");
  assert.equal(samples[0].diagnostics.capture.target_point.x, 20);
  assert.equal(samples[1].status, "hit");
  assert.equal(samples[1].node_id, "intended");
  assert.equal(samples[1].diagnostics.target.path, "/original");
  assert.equal(samples[1].diagnostics.capture.target_point.x, 35);
  assert.equal(fixture.browser.gl.lastReadArguments, undefined);
  assert.deepEqual(fixture.waits, [{ kind: "hover", timeout: 1000, polling: "raf" },
    { kind: "selection", timeout: 1000, polling: "raf" }]);
});

test("selection can refresh the same original GPU point from its active full-title record", async () => {
  const fixture = interactionPage({ hoverHit: true, afterHover({ intended }) {
    intended.dataset.fullTitle = "true"; intended.dataset.pointX = "21.5";
  } });
  const samples = await measureNodeInteractions(fixture.page, ".workspace-graph", 1);
  assert.deepEqual(fixture.clicks, [[121.5, 230]]);
  assert.equal(samples[0].status, "hit"); assert.equal(samples[1].status, "hit");
  assert.equal(samples[1].node_id, "intended");
});

for (const problem of ["absent", "outside", "NaN", "Infinity", "blocked", "different-canvas", "path-changed"]) {
  test(`selection retains an original-ID miss without a neighbor fallback when its current point is ${problem}`, async () => {
    const fixture = interactionPage({ afterHover({ browser, intended, neighbor }) {
      if (problem === "absent") browser.setLabels([neighbor]);
      else if (problem === "outside") intended.dataset.pointX = "901";
      else if (problem === "NaN" || problem === "Infinity") intended.dataset.pointX = problem;
      else if (problem === "blocked") browser.setPointerTarget({ tagName: "DIV" });
      else if (problem === "different-canvas") browser.setPointerTarget({ tagName: "CANVAS" });
      else browser.state.requests[0].scene.nodes[0].path = "/changed-identity";
    } });
    const samples = await measureNodeInteractions(fixture.page, ".workspace-graph", 1);
    assert.deepEqual(fixture.moves, [[0, 0], [120, 230]]);
    assert.deepEqual(fixture.clicks, [], "Never click the old coordinate or another available node");
    assert.equal(samples.length, 2);
    assert.equal(samples[0].status, "missed"); assert.equal(samples[1].status, "missed");
    assert.equal(samples[1].node_id, "intended"); assert.match(samples[1].reason, /original|point|canvas|path/);
    assert.equal(fixture.waits.length, 1, "No retry or selection wait when the original point is invalid");
  });
}

function neutralFixture(labels = [trackedLabel("one", 20, 30), trackedLabel("two", 40, 50)]) {
  const browser = measurementBrowser();
  browser.setLabels(labels);
  browser.state.requests.push({ budget: mapBenchmarkBudget("10k"), scene: {
    nodes: labels.map(label => ({ id: label.dataset.nodeId, path: `/${label.dataset.nodeId}`, label: label.dataset.nodeId })),
  } });
  for (const label of labels) browser.publishPoint(label);
  const container = { querySelectorAll: () => browser.getLabels(), querySelector: () => browser.canvas,
    getBoundingClientRect: () => browser.canvas.getBoundingClientRect() };
  return { browser, labels, start: () => browser.run(waitForNeutralNodePublication, [container, 1000]) };
}

test("neutral setup orders Clear, its RAF focus handoff, public search focus, then pointer outside", async () => {
  const events = [];
  let neutral = false;
  const page = {
    locator(selector) {
      if (selector === ".workspace-graph-selection-clear") return {
        async count() { return 1; }, async click() { events.push("clear"); neutral = false; },
      };
      return { async evaluate(observer, deadline) {
        events.push("observe"); assert.equal(observer, waitForNeutralNodePublication); assert.equal(deadline, 1000);
        return { ready: neutral };
      } };
    },
    async evaluate(callback) { assert.match(callback.toString(), /requestAnimationFrame/); events.push("clear-focus-RAF"); },
    getByRole(role, options) {
      assert.equal(role, "searchbox"); assert.equal(options.name, "Find in visible map");
      return { async focus() { assert.equal(events.at(-1), "clear-focus-RAF"); events.push("search-focus"); } };
    },
    mouse: { async move(x, y) { assert.deepEqual([x, y], [0, 0]);
      assert.equal(events.at(-1), "search-focus"); events.push("outside"); neutral = true; } },
  };
  assert.equal((await neutralizeNodeInteraction(page, "current-map")).ready, true);
  assert.deepEqual(events, ["clear", "clear-focus-RAF", "search-focus", "outside", "observe"]);
});

test("neutral publication waits for the actual late mutation, not a partial or stale-ID coordinate row", async () => {
  const fixture = neutralFixture();
  const baseObservers = fixture.browser.observerCount();
  delete fixture.labels[1].dataset.pointX;
  let settled = false;
  const pending = fixture.start().then(result => { settled = true; return result; });
  await Promise.resolve(); assert.equal(settled, false);
  assert.equal([...fixture.browser.timers.values()][0].delay, 1000);
  fixture.labels[1].dataset.pointX = "42";
  fixture.browser.state.pointPublications.set(fixture.labels[1], { node_id: "old-bound-ID", at_ms: 0 });
  await Promise.resolve(); assert.equal(settled, false);
  fixture.browser.publishPoint(fixture.labels[1]);
  const result = await pending;
  assert.equal(result.ready, true); assert.equal(result.diagnostics.published_rows, 2);
  assert.equal(result.diagnostics.eligible_count, 2); assert.equal(result.diagnostics.mutation_events, 1);
  assert.equal(fixture.browser.timers.size, 0); assert.equal(fixture.browser.observerCount(), baseObservers);
});

test("a publication mutation delivered after1000ms fails even before its delayed timer callback", async () => {
  const fixture = neutralFixture();
  delete fixture.labels[1].dataset.pointX;
  const baselineObservers = fixture.browser.observerCount();
  const pending = fixture.start();
  fixture.browser.advanceNow(1001);
  fixture.labels[1].dataset.pointX = "42";
  fixture.browser.publishPoint(fixture.labels[1]);
  const result = await pending;
  assert.equal(result.ready, false); assert.ok(result.diagnostics.elapsed_ms > 1000);
  assert.equal(result.diagnostics.published_rows, 2);
  assert.equal(fixture.browser.timers.size, 0, "Late event resolves failure without waiting for a delayed timer");
  assert.equal(fixture.browser.observerCount(), baselineObservers);
});

for (const problem of ["zero rows", "unpublished", "old-bound-ID", "explicit focus", "active hover", "too many rows", "duplicate IDs"]) {
  test(`neutral preflight fails at its unchanged1000ms deadline for ${problem}`, async () => {
    const fixture = neutralFixture(problem === "zero rows" ? [] : undefined);
    if (problem === "unpublished") delete fixture.labels[0].dataset.pointY;
    if (problem === "old-bound-ID") fixture.browser.state.pointPublications.set(fixture.labels[0], { node_id: "old", at_ms: 0 });
    if (problem === "explicit focus") { fixture.labels[0].dataset.fullTitle = "true"; fixture.labels[0].dataset.labelPriority = "explicit"; }
    if (problem === "active hover") fixture.browser.canvas.style.cursor = "grab";
    if (problem === "too many rows") fixture.browser.setLabels(Array.from({ length: 41 }, () => fixture.labels[0]));
    if (problem === "duplicate IDs") fixture.labels[1].dataset.nodeId = fixture.labels[0].dataset.nodeId;
    const pending = fixture.start(); fixture.browser.fireTimers();
    const result = await pending;
    assert.equal(result.ready, false); assert.equal(result.diagnostics.deadline_ms, 1000);
    assert.ok(result.diagnostics.row_count <= 41); assert.ok(result.candidates.length <= 40);
  });
}

test("neutral admission rejects another canvas and retains bounded blocker counts", async () => {
  const fixture = neutralFixture(); fixture.browser.setPointerTarget({ tagName: "CANVAS" });
  const result = await fixture.start();
  assert.equal(result.ready, true); assert.equal(result.candidates.length, 0);
  assert.equal(result.diagnostics.rejections.blocked, 2);
});

test("fewer than requested original targets fails every denominator slot without input substitution", async () => {
  const fixture = interactionPage();
  const samples = await measureNodeInteractions(fixture.page, ".workspace-graph", 3);
  assert.equal(samples.length, 6); assert.ok(samples.every(sample => sample.status === "missed"));
  assert.ok(samples.every(sample => sample.diagnostics.attempted_input === false));
  assert.equal(samples[0].diagnostics.setup.eligible_count, 2);
  assert.deepEqual(fixture.moves, [[0, 0]]); assert.deepEqual(fixture.clicks, []);
});

for (const changed of ["absent", "path", "ID"]) {
  test(`frozen admission never replaces its second original target after ${changed} changes`, async () => {
    const fixture = interactionPage({ hoverHit: true, afterSelection({ browser, neighbor }) {
      if (changed === "absent") browser.setLabels(browser.getLabels().filter(label => label !== neighbor));
      if (changed === "path") browser.state.requests[0].scene.nodes[1].path = "/replaced-path";
      if (changed === "ID") {
        neighbor.dataset.nodeId = "replacement";
        browser.state.requests[0].scene.nodes.push({ id: "replacement", path: "/replacement", label: "Replacement" });
        browser.publishPoint(neighbor);
      }
    } });
    const samples = await measureNodeInteractions(fixture.page, ".workspace-graph", 2);
    assert.equal(samples.length, 4); assert.equal(samples[0].status, "hit");
    assert.equal(samples[1].status, "hit");
    assert.equal(samples[2].node_id, "neighbor"); assert.equal(samples[3].node_id, "neighbor");
    assert.equal(samples[2].status, "missed"); assert.equal(samples[3].status, "missed");
    assert.equal(samples[2].diagnostics.attempted_input, false);
    assert.deepEqual(fixture.clicks, [[120, 230]]);
    assert.deepEqual(fixture.moves, [[0, 0], [120, 230], [0, 0]]);
    assert.deepEqual(samples[2].diagnostics.admission.admitted_targets.map(target => target.node_id), ["intended", "neighbor"]);
  });
}

test("interaction diagnostics distinguish DOM publication age from unavailable GPU snapshot age", () => {
  const browser = measurementBrowser();
  const label = trackedLabel("intended", 20, 30);
  browser.setLabels([label]);
  browser.publishPoint(label);
  const capture = browser.state.readInteractionDiagnostics("intended");
  assert.equal(capture.target_point.x, 20);
  assert.ok(capture.point_dom_age_ms >= 0);
  assert.equal(capture.gpu_point_age_ms, null);
  assert.match(capture.gpu_point_age_reason, /DOM publication age is not GPU snapshot age/);
  assert.equal(capture.native_camera.x, 0);
  browser.canvas.__zoom.x = 15;
  const after = browser.state.readInteractionDiagnostics("intended");
  assert.equal(after.native_camera.x, 15);
  assert.ok(after.point_dom_age_ms > capture.point_dom_age_ms);
});

test("a missed target retains capture/event/current-point and neighbor-hover diagnostics without retargeting", () => {
  const browser = measurementBrowser();
  const intended = trackedLabel("intended", 20, 30);
  const neighbor = trackedLabel("neighbor", 40, 50, true);
  browser.setLabels([intended, neighbor]);
  browser.publishPoint(intended);
  const capture = browser.state.readInteractionDiagnostics("intended");
  browser.state.pendingInteraction = {
    kind: "hover", node_id: "intended", start_ms: null, end_ms: null,
    framePending: false, capture_diagnostics: capture,
  };
  intended.dataset.pointX = "35";
  browser.publishPoint(intended);
  browser.canvas.style.cursor = "grab";
  browser.pointer("pointermove", { clientX: 120, clientY: 230, offsetX: 20, offsetY: 30 });
  const pending = browser.state.pendingInteraction;
  assert.equal(pending.node_id, "intended");
  assert.equal(pending.capture_diagnostics.target_point.x, 20);
  assert.equal(pending.dispatch_diagnostics.target_point.x, 35);
  assert.equal(pending.dispatch_diagnostics.event.offset_x, 20);
  assert.equal(pending.dispatch_diagnostics.event.native_canvas_target, true);
  assert.equal(pending.dispatch_diagnostics.inferred_hovered_node_id, "neighbor");
  assert.match(pending.dispatch_diagnostics.hover_identity_source, /Inference/);
  assert.equal(pending.end_ms, null);
  assert.equal(browser.frames.length, 0);
});

test("a terminal or partially unobservable zoom window is never issued", () => {
  const { state } = measurementBrowser();
  assert.equal(state.canIssueMotionWindow(6_000, 6_000, 6_000, false), false);
  assert.equal(state.canIssueMotionWindow(5_900, 5_900, 6_000, false), false);
  assert.equal(state.canIssueMotionWindow(5_750, 5_750, 6_000, false), true);
  assert.equal(state.canIssueMotionWindow(5_750, 5_750, null, false), false);
  assert.equal(state.canIssueMotionWindow(5_750, 5_750, 6_000, true), false);
});

test("post-interaction proof requires new full-body draws and permits a later one-point outline", () => {
  const { state, gl } = measurementBrowser();
  state.nativeDraws = { node_vertices: 10_000, edge_instances: 30_895 };
  state.nativeProofPhase = { started_at_ms: 1, node_vertex_counts: new Set(), edge_instance_counts: new Set() };
  const proof = () => ({ started_at_ms: 1, finished_at_ms: 2,
    node_vertex_counts: [...state.nativeProofPhase.node_vertex_counts],
    edge_instance_counts: [...state.nativeProofPhase.edge_instance_counts] });
  assert.throws(() => assertFreshNativeSceneProof(proof(), 10_000, 30_895));
  gl.bindFramebuffer(gl.FRAMEBUFFER, {});
  gl.drawArrays(gl.POINTS, 0, 10_000);
  gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, 30_895);
  assert.throws(() => assertFreshNativeSceneProof(proof(), 10_000, 30_895));
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, 30_895);
  gl.drawArrays(gl.POINTS, 0, 10_000);
  gl.drawArrays(gl.POINTS, 0, 1);
  assert.equal(state.nativeDraws.node_vertices, 1);
  assert.doesNotThrow(() => assertFreshNativeSceneProof(proof(), 10_000, 30_895));
});

test("diagnostic profile cannot become acceptance or an unbounded/multiple run", () => {
  const options = { profileMotion: true, pc10kAcceptance: false, mapScale: "10k", motionSeconds: 5,
    runs: 1, coldRuns: 0, cpuRates: [1], surfaces: ["knowledge-map"] };
  assert.doesNotThrow(() => validateMotionProfileOptions(options));
  for (const change of [{ pc10kAcceptance: true }, { motionSeconds: 60 }, { runs: 3 }, { cpuRates: [1, 4] }]) {
    assert.throws(() => validateMotionProfileOptions({ ...options, ...change }));
  }
});

test("startup CPU profile is one bounded diagnostic, never performance acceptance or combined motion profiling", () => {
  const options = { profileStartup: true, profileMotion: false, pc10kAcceptance: false, mapScale: "10k", motionSeconds: 0,
    runs: 1, coldRuns: 0, cpuRates: [1], surfaces: ["knowledge-map"] };
  assert.doesNotThrow(() => validateStartupProfileOptions(options));
  assert.doesNotThrow(() => validateStartupProfileOptions({ ...options, motionSeconds: 5 }));
  for (const change of [{ pc10kAcceptance: true }, { profileMotion: true }, { motionSeconds: 60 }, { runs: 3 },
    { coldRuns: 1 }, { cpuRates: [1, 4] }, { mapScale: "2k" }, { surfaces: ["context-graph"] }]) {
    assert.throws(() => validateStartupProfileOptions({ ...options, ...change }));
  }
});

test("diagnostic9660 draw and UI phase markers never satisfy the original exact10K frame gate", async () => {
  const browser = measurementBrowser(), { state, gl, frames } = browser;
  state.startup = { start_ms: 0, events: [], native_first_draws: [], dom_states: [], lost_records: 0 };
  state.targetMapNodes = 10_000; state.targetMapEdges = 30_895; browser.setReady();
  const response = await browser.window.fetch("/api/v1/workspaces/a/graph/overview/query", {
    method: "POST", body: JSON.stringify({ budget: mapBenchmarkBudget("10k") }),
  });
  await response.json();
  state.targetMapEdges = 30_895;
  gl.bindFramebuffer(gl.FRAMEBUFFER, {}); gl.drawArrays(gl.POINTS, 0, 9_660);
  assert.equal(state.startup.native_first_draws.length, 0, "Offscreen reads/draws are never first-frame proof");
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, 30_895); gl.drawArrays(gl.POINTS, 0, 9_660);
  assert.equal(state.startup.native_first_draws[0].node_count, 9_660);
  assert.equal(state.firstCanvasFrameAt, null); assert.equal(frames.length, 0);
  state.markStartup("unlinked-click-call");
  gl.drawArrays(gl.POINTS, 0, 10_000);
  assert.equal(state.startup.native_first_draws[1].node_count, 10_000);
  assert.equal(state.firstCanvasFrameAt, null); assert.equal(frames.length, 1);
  frames.shift()();
  assert.ok(state.firstCanvasFrameAt > state.startup.native_first_draws[1].at_ms);
  assert.equal(state.startup.events[0].name, "request-json-end");
  assert.equal(state.startup.events[1].name, "unlinked-click-call");
  assert.equal(state.startup.graph_json_end_ms, state.requests[0].json_end_ms);
  assert.equal(state.startup.lost_records, 0);
});

test("startup native-count evidence is bounded and reports rather than silently drops overflow", () => {
  const { state, gl } = measurementBrowser();
  state.startup = { start_ms: 0, graph_json_end_ms: 0, events: [], native_first_draws: [], dom_states: [], lost_records: 0 };
  for (let count = 2_000; count < 2_017; count++) gl.drawArrays(gl.POINTS, 0, count);
  assert.equal(state.startup.native_first_draws.length, 16);
  assert.equal(state.startup.lost_records, 1);
});

test("selection diagnostic phases preserve the original target while exposing a different native bubble winner", () => {
  const browser = measurementBrowser(), { state } = browser;
  state.selectionPhaseDiagnostics = true;
  const intended = trackedLabel("intended", 20, 30, true), neighbor = trackedLabel("neighbor", 40, 50, true);
  browser.setLabels([intended]); browser.canvas.style.cursor = "grab";
  state.pendingInteraction = { kind: "selection", node_id: "intended", path: "/intended", start_ms: null, end_ms: null };
  browser.pointer("pointerdown", { clientX: 120, clientY: 230 }, true);
  browser.setLabels([neighbor]);
  browser.pointer("pointerdown", { clientX: 120, clientY: 230 }, false);
  const pending = state.pendingInteraction;
  assert.equal(pending.phase_events[0].phase, "capture");
  assert.equal(pending.phase_events[0].inferred_hovered_node_id, "intended");
  assert.equal(pending.phase_events[1].phase, "bubble");
  assert.equal(pending.phase_events[1].inferred_hovered_node_id, "neighbor");
  assert.equal(pending.node_id, "intended"); assert.equal(pending.path, "/intended"); assert.equal(pending.end_ms, null);
  for (let index = 0; index < 10; index++) browser.pointer("click", { clientX: 120, clientY: 230 });
  assert.equal(pending.phase_events.length, 12); assert.equal(pending.phase_events_lost, 10);
});

test("selection phase diagnostics add no normal acceptance-path samples unless explicitly enabled", () => {
  const browser = measurementBrowser();
  browser.state.pendingInteraction = { kind: "selection", node_id: "intended", start_ms: null, end_ms: null };
  browser.pointer("pointerdown", {}); browser.pointer("pointerup", {}); browser.pointer("click", {});
  assert.equal(browser.state.pendingInteraction.phase_events, undefined);
});

test("readPixels timing preserves original arguments and return without retaining pixels", () => {
  const browser = measurementBrowser();
  const original = Object.getPrototypeOf(browser.gl).readPixels;
  browser.installPixels();
  const probe = browser.state.readPixelsProfile;
  const pixels = new Float32Array([65_535, 0, 0, 1]);
  assert.equal(browser.gl.readPixels(0, 0, 7, 7, 6_408, 5_126, pixels), "native-result");
  assert.equal(probe.records.length, 0);
  probe.active = true;
  assert.equal(browser.gl.readPixels(0, 0, 7, 7, 6_408, 5_126, pixels), "native-result");
  assert.equal(browser.gl.lastReadArguments[6], pixels);
  assert.equal(probe.records[0].target_mode, "cpu-array");
  assert.equal(probe.records[0].width, 7);
  assert.equal(JSON.stringify(probe.records).includes("65535"), false);
  browser.gl.readPixels(0, 0, 1, 1, 6_408, 5_126, 0);
  assert.equal(probe.records[1].target_mode, "pbo-offset");
  probe.restore();
  assert.equal(Object.getPrototypeOf(browser.gl).readPixels, original);
});

test("readPixels summaries clip wall time to the measured stable window", () => {
  const records = [
    { start_ms: 95, end_ms: 105, target_mode: "cpu-array", width: 7, height: 7 },
    { start_ms: 120, end_ms: 140, target_mode: "cpu-array", width: 7, height: 7 },
    { start_ms: 190, end_ms: 210, target_mode: "pbo-offset", width: 1, height: 1 },
  ];
  assert.deepEqual(summarizeReadPixels(records, 100, 200), [
    { target_mode: "cpu-array", width: 7, height: 7, calls: 2, wall_ms: 25, max_call_ms: 20, threw: 0 },
    { target_mode: "pbo-offset", width: 1, height: 1, calls: 1, wall_ms: 10, max_call_ms: 20, threw: 0 },
  ]);
});

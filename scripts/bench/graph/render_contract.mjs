/** Guards shared by the browser harness and its mutation-sensitive unit cases. */
export function mapBenchmarkBudget(scale) {
  if (scale === "2k") return { documents: 2_000, relations: 6_000 };
  if (scale === "10k") return { documents: 10_000, relations: 100_000 };
  throw new Error("--map-scale must be 2k or 10k");
}

/**
 * Collapse multiple native submissions into their observed RAF opportunity.
 * This is a frame-boundary proxy, not proof of compositor presentation. The
 * inputs are monotonic clocks captured on one page; no GPU query is needed.
 */
export function interactiveFrameBoundaries(rafTimestamps, nativeTimestamps, interactionWindows) {
  const windows = interactionWindows.map((window) => ({ start_ms: window.start_ms, end_ms: window.end_ms }))
    .sort((left, right) => left.start_ms - right.start_ms);
  const merged = [];
  for (const window of windows) {
    const previous = merged.at(-1);
    if (previous && window.start_ms <= previous.end_ms) previous.end_ms = Math.max(previous.end_ms, window.end_ms);
    else merged.push({ ...window });
  }
  const frames = [];
  let nativeIndex = 0;
  for (let index = 1; index < rafTimestamps.length; index += 1) {
    const previous = rafTimestamps[index - 1];
    const boundary = rafTimestamps[index];
    while (nativeIndex < nativeTimestamps.length && nativeTimestamps[nativeIndex] < previous) nativeIndex += 1;
    let lastDraw = null;
    while (nativeIndex < nativeTimestamps.length && nativeTimestamps[nativeIndex] < boundary) {
      lastDraw = nativeTimestamps[nativeIndex++];
    }
    if (lastDraw !== null && boundary > previous) frames.push({ at_ms: boundary, last_native_draw_at_ms: lastDraw });
  }
  const intervals = [];
  let windowIndex = 0;
  for (let index = 0; index < frames.length; index += 1) {
    const frame = frames[index];
    while (windowIndex < merged.length && merged[windowIndex].end_ms < frame.last_native_draw_at_ms) windowIndex += 1;
    const window = merged[windowIndex];
    if (!window || frame.last_native_draw_at_ms < window.start_ms) continue;
    const previous = frames[index - 1]?.at_ms ?? window.start_ms;
    intervals.push(frame.at_ms - Math.max(previous, window.start_ms));
  }
  const lowerBound = (value) => {
    let left = 0;
    let right = nativeTimestamps.length;
    while (left < right) {
      const middle = (left + right) >>> 1;
      if (nativeTimestamps[middle] < value) left = middle + 1;
      else right = middle;
    }
    return left;
  };
  const missing = windows.filter((window) => {
    const index = lowerBound(window.start_ms);
    return index >= nativeTimestamps.length || nativeTimestamps[index] > window.end_ms;
  });
  const submissions = [];
  windowIndex = 0;
  for (let index = 1; index < nativeTimestamps.length; index += 1) {
    const timestamp = nativeTimestamps[index];
    while (windowIndex < merged.length && merged[windowIndex].end_ms < timestamp) windowIndex += 1;
    const window = merged[windowIndex];
    if (window && timestamp >= window.start_ms) submissions.push(timestamp - nativeTimestamps[index - 1]);
  }
  return { rendered_raf_boundaries: frames, interactive_raf_intervals: intervals,
    missing_interaction_windows: missing, interactive_native_intervals: submissions };
}

export function assertExact10kMeasurement({ request, response, columns, native, expectedRelations }) {
  const budget = mapBenchmarkBudget("10k");
  if (request?.documents !== budget.documents || request?.relations !== budget.relations) {
    throw new Error("PC10K measurement requires the original UI request budget 10000/100000");
  }
  if (response?.representation !== "exact" || response?.completeness !== "complete"
    || response?.truncated !== false || response?.clusters !== 0 || response?.aggregated_edges !== 0) {
    throw new Error("PC10K measurement cannot accept an aggregated, capped or sampled scene");
  }
  if (response.nodes !== 10_000 || response.documents !== 10_000
    || !Number.isSafeInteger(response.edges) || response.edges < 0 || response.edges > budget.relations
    || response.candidate_documents !== 10_000 || response.candidate_relations !== response.edges
    || (expectedRelations !== null && expectedRelations !== undefined && response.edges !== expectedRelations)) {
    throw new Error("PC10K measurement does not contain the expected complete original corpus");
  }
  if (columns?.nodes !== response.nodes || columns?.edges !== response.edges) {
    throw new Error("PC10K visible columns omit original documents or relationships");
  }
  if (native?.node_vertices !== response.nodes || native?.edge_instances !== response.edges) {
    throw new Error("PC10K native default-framebuffer draws do not match the original API scene");
  }
}

export function assertFreshNativeSceneProof(proof, nodes, edges) {
  if (!proof || !Number.isFinite(proof.started_at_ms) || !Number.isFinite(proof.finished_at_ms)
    || proof.finished_at_ms <= proof.started_at_ms || !proof.node_vertex_counts?.includes(nodes)
    || !proof.edge_instance_counts?.includes(edges)) {
    throw new Error("Post-interaction native draw phase did not observe the complete original scene body and relationships");
  }
}

export async function beginNativeSceneProof(page) {
  await page.evaluate(() => {
    window.__dtxGraphRenderBench.nativeProofPhase = { started_at_ms: performance.now(),
      node_vertex_counts: new Set(), edge_instance_counts: new Set() };
  });
}

export async function finishNativeSceneProof(page) {
  return page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => {
    const state = window.__dtxGraphRenderBench;
    const phase = state.nativeProofPhase;
    state.nativeProofPhase = null;
    resolve({ started_at_ms: phase.started_at_ms, finished_at_ms: performance.now(),
      node_vertex_counts: [...phase.node_vertex_counts].sort((left, right) => left - right),
      edge_instance_counts: [...phase.edge_instance_counts].sort((left, right) => left - right) });
  }))));
}

export async function selectMapBenchmarkScale(page, scale, { startupDiagnostics = false } = {}) {
  if (scale === "2k") return;
  mapBenchmarkBudget(scale);
  const view = page.locator("details.workspace-graph-view-options");
  await view.locator("summary").click();
  const control = view.locator('select:has(option[value="10k"])');
  // A missing control is a failed PC benchmark, never a mobile override or
  // permission to continue recording the default aggregated 2K scene.
  await control.waitFor({ state: "visible" });
  await page.evaluate(() => {
    const state = window.__dtxGraphRenderBench;
    state.firstCanvasFrameAt = null;
    state.firstCanvasFramePending = false;
    state.requests = [];
    state.targetMapNodes = 10_000;
    state.targetMapEdges = null;
    state.nativeDraws = { node_vertices: null, edge_instances: null, first_scene_draw_at: null };
  });
  const responsePromise = page.waitForResponse((response) => {
    if (response.request().method() !== "POST" || !response.url().includes("/graph/overview/query")) return false;
    const budget = response.request().postDataJSON()?.budget;
    return budget?.documents === 10_000 && budget?.relations === 100_000;
  });
  // Keep both promises observed if a policy hides or rejects the selection.
  if (startupDiagnostics) await page.evaluate(() => window.__dtxGraphRenderBench.markStartup("scale-select-call"));
  await Promise.all([control.selectOption("10k"), responsePromise]);
  if (startupDiagnostics) await page.evaluate(() => window.__dtxGraphRenderBench.markStartup("view-close-call"));
  await view.locator("summary").click();
  if (startupDiagnostics) await page.evaluate(() => window.__dtxGraphRenderBench.markStartup("view-close-resolved"));
}

export function assertPc10kAcceptance(sample) {
  assertFreshNativeSceneProof(sample.final_native_proof, 10_000, sample.map_measurement?.response?.edges);
  const gpu = sample.gpu;
  const renderer = typeof gpu?.renderer === "string" ? gpu.renderer.trim() : "";
  // A name from another context or masked privacy metadata cannot establish
  // which implementation rendered this scene. Historical reports without
  // this provenance remain historical evidence, not newly qualified runs.
  const unknown = /\b(?:unknown|unavailable|unidentified|masked|generic)\b/i.test(renderer)
    || /^(?:(?:webkit|mozilla|angle)\s+)?webgl(?:\s*2)?(?:\s+renderer)?$/i.test(renderer)
    || /^(?:angle|webkit|mozilla|opengl(?: es)?(?: renderer)?)$/i.test(renderer);
  const software = /swiftshader|llvmpipe|softpipe|lavapipe|swrast|software|basic render driver|gdi generic|mesa offscreen/i.test(renderer);
  if (gpu?.source !== "current-graph-canvas" || gpu?.context_type !== "webgl2" || gpu?.webgl !== true
    || gpu?.identification !== "unmasked" || gpu?.unmasked_renderer !== gpu?.renderer
    || !renderer || unknown || software) throw new Error("PC10K performance acceptance requires an identified hardware GPU from the current graph WebGL2 context");
  if (!Number.isFinite(sample.graph_timing?.response_end_to_first_frame_ms)
    || sample.graph_timing.response_end_to_first_frame_ms < 0
    || sample.graph_timing.response_end_to_first_frame_ms > 2_000) throw new Error("PC10K first frame exceeds the 2000ms response-complete budget");
  if (!Number.isFinite(sample.motion?.interactive_raf_frame_interval_ms?.p95)
    || sample.motion.interactive_raf_frame_interval_ms.p95 > 33
    || sample.motion?.missing_interaction_windows?.length !== 0) {
    throw new Error("PC10K drawn RAF-frame p95 exceeds 33ms, lacks samples or contains an interaction window without a full-scene draw");
  }
  if (!sample.motion?.pan_trials?.length || sample.motion.pan_trials.some((trial) => !trial.changed)) {
    throw new Error("PC10K normal-motion run requires actual native camera changes during pan trials");
  }
  if (!sample.motion?.initial?.moved_node_count) throw new Error("PC10K normal-motion run did not observe actual GPU-backed node movement");
  for (const kind of ["hover", "selection"]) {
    const metric = sample.interaction?.[kind];
    const entries = sample.interaction?.samples?.filter((entry) => entry.kind === kind) ?? [];
    if (!metric || metric.attempted < 10 || metric.missed !== 0 || !Number.isFinite(metric.latency_ms?.p95)
      || metric.latency_ms.p95 < 0 || metric.latency_ms.p95 > 100
      || entries.length !== metric.attempted || entries.some((entry) => entry.status !== "hit"
        || !Number.isFinite(entry.latency_ms) || entry.latency_ms < 0)) {
      throw new Error(`PC10K ${kind} requires 10 actual hits, no misses and p95 <=100ms`);
    }
  }
}

/** Browser-only observer: use an existing native-draw context; never allocate one. */
export function readCurrentGraphGpuMetadata(readySelector) {
  const canvases = document.querySelector(readySelector)?.querySelectorAll("canvas") ?? [];
  let gl = null;
  let contextType = null;
  for (const canvas of canvases) {
    const context = window.__dtxGraphRenderBench?.nativeContexts?.get(canvas);
    if (!context || context.canvas !== canvas || !canvas.isConnected || context.isContextLost()) continue;
    if (typeof WebGL2RenderingContext !== "undefined" && context instanceof WebGL2RenderingContext) {
      gl = context; contextType = "webgl2";
      break;
    }
    // Local Sigma may use WebGL1 on a supported fallback device. Keep its
    // actual-context diagnostics, never qualify it as the PC10K WebGL2 path.
    if (typeof WebGLRenderingContext !== "undefined" && context instanceof WebGLRenderingContext) {
      gl = context; contextType = "webgl";
    }
  }
  const result = {
    source: "current-graph-canvas", context_type: null, identification: "unavailable",
    renderer: null, vendor: null, masked_renderer: null, masked_vendor: null,
    unmasked_renderer: null, unmasked_vendor: null, webgl: false,
    browser_environment: {
      user_agent: window.navigator.userAgent, platform: window.navigator.platform,
      device_pixel_ratio: window.devicePixelRatio,
      viewport: { width: window.innerWidth, height: window.innerHeight },
      reduced_motion: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    },
  };
  if (!gl) return result;
  result.context_type = contextType;
  result.webgl = true;
  const text = value => typeof value === "string" && value.trim() ? value : null;
  try {
    result.masked_renderer = text(gl.getParameter(gl.RENDERER));
    result.masked_vendor = text(gl.getParameter(gl.VENDOR));
    const extension = gl.getExtension("WEBGL_debug_renderer_info");
    if (extension) {
      result.unmasked_renderer = text(gl.getParameter(extension.UNMASKED_RENDERER_WEBGL));
      result.unmasked_vendor = text(gl.getParameter(extension.UNMASKED_VENDOR_WEBGL));
    }
    result.renderer = result.unmasked_renderer ?? result.masked_renderer;
    result.vendor = result.unmasked_vendor ?? result.masked_vendor;
    result.identification = result.unmasked_renderer ? "unmasked" : result.masked_renderer ? "masked" : "unavailable";
  } catch {
    // Failed metadata queries are not hardware evidence. Keep any masked
    // values for diagnostics without turning them into an acceptance claim.
    result.renderer = result.masked_renderer;
    result.vendor = result.masked_vendor;
    result.unmasked_renderer = null;
    result.unmasked_vendor = null;
    result.identification = "unavailable";
  }
  return result;
}

/** Browser-only initializer; Playwright serializes this function without imports. */
export function installGraphMeasurement() {
  const state = {
    firstCanvasFrameAt: null,
    firstCanvasFramePending: false,
    longTaskSupported: false,
    longTasks: [],
    requests: [],
    targetMapNodes: null,
    targetMapEdges: null,
    nativeDraws: { node_vertices: null, edge_instances: null, first_scene_draw_at: null },
    nativeContexts: new WeakMap(),
    nativeFrames: [],
    nativeProofPhase: null,
    pendingInteraction: null,
    startup: null,
    selectionPhaseDiagnostics: false,
    pointPublications: new WeakMap(),
  };
  Object.defineProperty(window, "__dtxGraphRenderBench", { value: state });
  const canvasIdentities = new WeakMap();
  let nextCanvasIdentity = 0;
  const canvasIdentity = (canvas) => {
    if (!canvas) return null;
    if (!canvasIdentities.has(canvas)) canvasIdentities.set(canvas, ++nextCanvasIdentity);
    return canvasIdentities.get(canvas);
  };
  const appendStartup = (key, value, limit) => {
    if (!state.startup) return;
    if (state.startup[key].length < limit) state.startup[key].push(value);
    else state.startup.lost_records += 1;
  };
  state.markStartup = (name) => appendStartup("events", { name, at_ms: performance.now() }, 32);
  state.captureStartupDom = () => {
    if (!state.startup) return;
    const frame = document.querySelector(".workspace-graph .graph-canvas-frame");
    const canvas = frame?.querySelector("canvas");
    const columns = document.querySelector(".workspace-graph [data-map-node-count]");
    const toggle = document.querySelector(".workspace-graph-orphan-toggle");
    const snapshot = { canvas_id: canvasIdentity(canvas), status: frame?.getAttribute?.("data-renderer-status") ?? null,
      node_count: columns?.getAttribute?.("data-map-node-count") ?? null,
      unlinked_pressed: toggle?.getAttribute?.("aria-pressed") ?? null };
    const previous = state.startup.dom_states.at(-1);
    if (!previous || Object.keys(snapshot).some(key => snapshot[key] !== previous[key])) {
      appendStartup("dom_states", { at_ms: performance.now(), ...snapshot }, 64);
    }
  };
  document.addEventListener("click", (event) => {
    if (!state.startup) return;
    const unlinked = event.target?.closest?.(".workspace-graph-orphan-toggle");
    const summary = event.target?.closest?.("details.workspace-graph-view-options > summary");
    if (unlinked || summary) appendStartup("events", {
      name: unlinked ? "unlinked-click-dispatch" : summary.closest("details").open ? "view-close-click-dispatch" : "view-open-click-dispatch",
      at_ms: performance.now(), trusted: event.isTrusted,
    }, 32);
  }, true);
  state.canIssueMotionWindow = (timestamp, next, end, panActive) => Number.isFinite(end)
    && timestamp >= next && timestamp + 250 <= end && !panActive;

  state.readInteractionDiagnostics = (nodeId, event = null) => {
    const at = performance.now();
    const labels = Array.from(document.querySelectorAll('[data-workspace-map-labels] span[data-node-id]'));
    const label = labels.find((candidate) => candidate.dataset.nodeId === nodeId);
    const publication = label ? state.pointPublications.get(label) : null;
    const point = label?.hasAttribute("data-point-x") && label?.hasAttribute("data-point-y")
      ? { x: Number(label.dataset.pointX), y: Number(label.dataset.pointY) } : null;
    const native = document.querySelector(".workspace-graph .graph-canvas canvas");
    const zoom = native?.__zoom;
    const bounds = native?.getBoundingClientRect();
    const priorityLabels = labels.filter((candidate) => candidate.dataset.fullTitle === "true"
      && candidate.getClientRects().length > 0).map((candidate) => ({
      node_id: candidate.dataset.nodeId, priority: candidate.dataset.labelPriority ?? null,
    }));
    const active = priorityLabels.filter((candidate) => candidate.priority === "active");
    const cursor = native?.style.cursor ?? null;
    const links = Array.from(document.querySelectorAll(".workspace-graph-details a[href]"))
      .filter((link) => link.getClientRects().length > 0).map((link) => link.getAttribute("href"));
    const scene = state.requests.find((request) => request.budget?.documents === 10_000)?.scene;
    const selected = scene?.nodes?.find((node) => links.includes(node.path));
    return {
      at_ms: at,
      target_node_id: nodeId,
      target_point: point,
      point_dom_published_at_ms: publication?.node_id === nodeId ? publication.at_ms : null,
      point_dom_age_ms: publication?.node_id === nodeId ? at - publication.at_ms : null,
      gpu_point_age_ms: null,
      gpu_point_age_reason: "The existing label DOM has no native GPU readback timestamp; DOM publication age is not GPU snapshot age.",
      native_draw_age_ms: state.nativeFrames.length ? at - state.nativeFrames.at(-1) : null,
      native_camera: zoom ? { x: zoom.x, y: zoom.y, k: zoom.k } : null,
      native_bounds: bounds ? { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height } : null,
      native_cursor: cursor,
      visible_priority_labels: priorityLabels,
      inferred_hovered_node_id: cursor === "grab" && active.length === 1 ? active[0].node_id : null,
      hover_identity_source: "Inference from the native grab cursor and sole active-priority label; no direct engine hover-ID hook.",
      selected_node_id: selected?.id ?? null,
      selected_path: selected?.path ?? null,
      event: event ? { type: event.type, trusted: event.isTrusted, client_x: event.clientX,
        client_y: event.clientY, offset_x: event.offsetX, offset_y: event.offsetY,
        native_canvas_target: event.target === native } : null,
    };
  };

  const recordRequest = (url, method, body) => {
    const request = { method: String(method || "GET").toUpperCase(), start_ms: performance.now(), url: String(url) };
    // Only the graph budget is retained. Auth bodies, headers and credentials
    // are never captured. The application owns the original response object.
    if (request.url.includes("/graph/") && typeof body === "string") {
      try { request.budget = JSON.parse(body).budget ?? null; } catch { /* no JSON budget */ }
    }
    state.requests.push(request);
    return request;
  };
  const scanForCanvas = () => {
    if (state.firstCanvasFrameAt !== null || state.firstCanvasFramePending) return;
    const frame = document.querySelector('.graph-canvas-frame[data-renderer-status="ready"]');
    const canvas = frame?.querySelector("canvas");
    if (!(canvas instanceof HTMLCanvasElement) || canvas.width <= 0 || canvas.height <= 0) return;
    if (state.targetMapNodes !== null && (state.nativeDraws.node_vertices !== state.targetMapNodes
      || state.nativeDraws.first_scene_draw_at === null)) return;
    state.firstCanvasFramePending = true;
    requestAnimationFrame(() => {
      if (state.firstCanvasFrameAt === null && (state.targetMapNodes === null
        || state.nativeDraws.node_vertices === state.targetMapNodes)) state.firstCanvasFrameAt = performance.now();
      state.firstCanvasFramePending = false;
    });
  };
  const scanForInteraction = () => {
    const pending = state.pendingInteraction;
    if (!pending || pending.start_ms === null || pending.end_ms !== null || pending.framePending) return;
    const visible = pending.kind === "hover"
      ? Array.from(document.querySelectorAll('[data-workspace-map-labels] span[data-full-title="true"]'))
        .some((label) => label.dataset.nodeId === pending.node_id && label.getClientRects().length > 0)
      : Array.from(document.querySelectorAll(".workspace-graph-details a[href]"))
        .some((link) => link.getAttribute("href") === pending.path && link.getClientRects().length > 0);
    if (!visible) return;
    pending.framePending = true;
    // A rAF timestamp is shared by the frame and can predate a pointer event
    // dispatched in that frame. Sample the monotonic clock in the callback.
    requestAnimationFrame(() => { if (state.pendingInteraction === pending) pending.end_ms = performance.now(); });
  };
  for (const [type, kind] of [["pointermove", "hover"], ["pointerdown", "selection"]]) {
    document.addEventListener(type, (event) => {
      const pending = state.pendingInteraction;
      if (pending?.kind === kind && pending.start_ms === null && event.target instanceof HTMLCanvasElement) {
        pending.start_ms = performance.now();
        pending.dispatch_diagnostics = state.readInteractionDiagnostics(pending.node_id, event);
        scanForInteraction();
      }
    }, true);
  }
  for (const type of ["pointerdown", "pointerup", "mouseup", "click"]) {
    for (const capture of [true, false]) document.addEventListener(type, (event) => {
      const pending = state.pendingInteraction;
      if (!state.selectionPhaseDiagnostics || pending?.kind !== "selection" || !(event.target instanceof HTMLCanvasElement)) return;
      pending.phase_events ??= [];
      if (pending.phase_events.length < 12) pending.phase_events.push({ phase: capture ? "capture" : "bubble",
        ...state.readInteractionDiagnostics(pending.node_id, event) });
      else pending.phase_events_lost = (pending.phase_events_lost ?? 0) + 1;
    }, capture);
  }
  const originalFetch = window.fetch;
  window.fetch = async function instrumentedFetch(input, init) {
    const request = recordRequest(typeof input === "string" ? input : input?.url,
      init?.method ?? (typeof input === "object" ? input?.method : null), init?.body);
    const response = await originalFetch.apply(this, arguments);
    request.headers_at_ms = performance.now();
    if (request.method === "POST" && request.url.includes("/graph/")) {
      const originalJson = response.json;
      response.json = async function instrumentedJson() {
        request.json_start_ms = performance.now();
        const payload = await originalJson.apply(this, arguments);
        request.json_end_ms = performance.now();
        if (state.startup && request.budget?.documents === 10_000) {
          state.startup.graph_json_end_ms = request.json_end_ms;
          state.markStartup("request-json-end");
        }
        // Retain a reference, not another deep copy or JSON parse on the path
        // being measured. Only counts are serialized into the final evidence.
        request.scene = payload.data ?? payload;
        if (state.targetMapNodes !== null && request.budget?.documents === state.targetMapNodes) {
          state.targetMapEdges = request.scene.edges?.length ?? null;
        }
        return payload;
      };
    }
    return response;
  };
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  const xhrRequests = new WeakMap();
  XMLHttpRequest.prototype.open = function instrumentedOpen(method, url) {
    xhrRequests.set(this, { method, url });
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function instrumentedSend(body) {
    const request = xhrRequests.get(this);
    if (request) recordRequest(request.url, request.method, body);
    return originalSend.apply(this, arguments);
  };

  // Observe submitted visible draws rather than trusting React's column-count
  // attributes. Offscreen simulation/picking draws are excluded without a GL
  // getParameter/readPixels call or GPU synchronization on each frame.
  if (typeof WebGL2RenderingContext !== "undefined") {
    const prototype = WebGL2RenderingContext.prototype;
    const framebuffers = new WeakMap();
    const originalBind = prototype.bindFramebuffer;
    prototype.bindFramebuffer = function measuredBind(target, framebuffer) {
      if (target === this.FRAMEBUFFER || target === this.DRAW_FRAMEBUFFER) framebuffers.set(this, framebuffer);
      return originalBind.apply(this, arguments);
    };
    const recordDraw = (context, mode, count, instances) => {
      if (framebuffers.get(context) != null || !context.canvas.closest?.(".graph-canvas")) return;
      state.nativeContexts.set(context.canvas, context);
      if (mode === context.POINTS && instances === 1) {
        state.nativeDraws.node_vertices = count;
        state.nativeProofPhase?.node_vertex_counts.add(count);
        if (count === state.targetMapNodes) state.nativeFrames.push(performance.now());
        if (state.startup && Number.isFinite(state.startup.graph_json_end_ms) && count > 1) {
          const canvasId = canvasIdentity(context.canvas);
          if (!state.startup.native_first_draws.some(draw => draw.node_count === count && draw.canvas_id === canvasId)) {
            appendStartup("native_first_draws", { at_ms: performance.now(), node_count: count,
              edge_instances: state.nativeDraws.edge_instances, canvas_id: canvasId }, 16);
          }
        }
      }
      if (mode === context.TRIANGLE_STRIP && instances > 1) {
        state.nativeDraws.edge_instances = instances;
        state.nativeProofPhase?.edge_instance_counts.add(instances);
      }
      const target = state.targetMapNodes;
      if (target !== null && state.nativeDraws.node_vertices === target
        && state.nativeDraws.edge_instances === state.targetMapEdges
        && state.nativeDraws.first_scene_draw_at === null) {
        state.nativeDraws.first_scene_draw_at = performance.now();
        scanForCanvas();
      }
    };
    for (const method of ["drawArrays", "drawArraysInstanced"]) {
      const originalDraw = prototype[method];
      prototype[method] = function measuredDraw(mode, first, count, instances = 1) {
        const result = originalDraw.apply(this, arguments);
        recordDraw(this, mode, count, instances);
        return result;
      };
    }
  }

  // Sigma's existing WebGL1 fallback is diagnostic-only here: observe its
  // original default-framebuffer draws without changing the WebGL2 count
  // meter or issuing another getContext/query/draw call.
  if (typeof WebGLRenderingContext !== "undefined") {
    const prototype = WebGLRenderingContext.prototype;
    const framebuffers = new WeakMap();
    const originalBind = prototype.bindFramebuffer;
    prototype.bindFramebuffer = function measuredLegacyBind(target, framebuffer) {
      if (target === this.FRAMEBUFFER) framebuffers.set(this, framebuffer);
      return originalBind.apply(this, arguments);
    };
    const recordLegacyContext = context => {
      if (framebuffers.get(context) == null && context.canvas.closest?.(".graph-canvas")) {
        state.nativeContexts.set(context.canvas, context);
      }
    };
    for (const method of ["drawArrays", "drawElements"]) {
      const originalDraw = prototype[method];
      if (typeof originalDraw !== "function") continue;
      prototype[method] = function measuredLegacyDraw() {
        const result = originalDraw.apply(this, arguments);
        recordLegacyContext(this);
        return result;
      };
    }
    // Sigma's shaped-node/edge programs use the existing ANGLE extension for
    // instancing on WebGL1. Observe that returned extension, not another query.
    const originalExtension = prototype.getExtension;
    const observedExtensions = new WeakSet();
    if (typeof originalExtension === "function") prototype.getExtension = function measuredLegacyExtension(name) {
      const extension = originalExtension.apply(this, arguments);
      if (name !== "ANGLE_instanced_arrays" || !extension || observedExtensions.has(extension)) return extension;
      observedExtensions.add(extension);
      const context = this;
      for (const method of ["drawArraysInstancedANGLE", "drawElementsInstancedANGLE"]) {
        const originalDraw = extension[method];
        if (typeof originalDraw !== "function") continue;
        extension[method] = function measuredLegacyInstancedDraw() {
          const result = originalDraw.apply(this, arguments);
          recordLegacyContext(context);
          return result;
        };
      }
      return extension;
    };
  }

  try {
    if ((PerformanceObserver.supportedEntryTypes ?? []).includes("longtask")) {
      state.longTaskSupported = true;
      const observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (entry.duration > 100) state.longTasks.push({ duration_ms: entry.duration, name: entry.name, start_ms: entry.startTime });
        }
      });
      observer.observe({ buffered: true, type: "longtask" });
    }
  } catch { state.longTaskSupported = false; }
  const startObserver = () => {
    scanForCanvas();
    new MutationObserver((records) => {
      for (const record of records) {
        if ((record.attributeName === "data-point-x" || record.attributeName === "data-point-y")
          && record.target.dataset?.nodeId) state.pointPublications.set(record.target,
          { at_ms: performance.now(), node_id: record.target.dataset.nodeId });
      }
      scanForCanvas();
      scanForInteraction();
      state.captureStartupDom();
    }).observe(document.documentElement,
      { attributes: true, childList: true, subtree: true });
  };
  if (document.documentElement) startObserver();
  else document.addEventListener("DOMContentLoaded", startObserver, { once: true });
}

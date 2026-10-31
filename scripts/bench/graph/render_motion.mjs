import { interactiveFrameBoundaries } from "./render_contract.mjs";
import { neutralizeNodeInteraction } from "./render_interaction_setup.mjs";

/** Observe normal motion and submitted frames without a production test hook. */
export async function measureNormalMotion(page, readySelector, seconds, { profileGate = false } = {}) {
  const observation = page.evaluate(({ selector, duration, gated }) => {
    const container = document.querySelector(`${selector} .graph-canvas`);
    const canvas = container?.querySelector("canvas");
    const toolbar = container?.closest(".workspace-graph-canvas-shell")?.querySelector(".graph-canvas-toolbar")
      ?? document.querySelector(".graph-canvas-toolbar");
    const buttons = Array.from(toolbar?.querySelectorAll("button") ?? []);
    const zoomIn = buttons.find((button) => /zoom in|확대/i.test(button.getAttribute("aria-label") ?? ""));
    const zoomOut = buttons.find((button) => /zoom out|축소/i.test(button.getAttribute("aria-label") ?? ""));
    if (!canvas || !zoomIn || !zoomOut) throw new Error("Normal-motion measurement needs the native canvas and Zoom controls");
    const start = performance.now();
    const initialEnd = start + 5_000;
    let stableStart = gated ? null : initialEnd;
    let end = stableStart === null ? null : stableStart + duration * 1_000;
    const first = new Map();
    const moved = new Set();
    let nextPositionRead = start;
    let nextZoom = initialEnd;
    let zooms = 0;
    const observed = [];
    const windows = [];
    window.__dtxGraphRenderBench.motionStartedAt = start;
    window.__dtxGraphRenderBench.motionStableAt = stableStart;
    window.__dtxGraphRenderBench.motionFinishedAt = null;
    window.__dtxGraphRenderBench.motionProfileWaiting = false;
    window.__dtxGraphRenderBench.motionProfileReleasedAt = null;
    return new Promise((resolve) => {
      const tick = (timestamp) => {
        if (stableStart === null && timestamp >= initialEnd) {
          window.__dtxGraphRenderBench.motionProfileWaiting = true;
          const released = window.__dtxGraphRenderBench.motionProfileReleasedAt;
          if (Number.isFinite(released)) {
            stableStart = released;
            end = stableStart + duration * 1_000;
            nextZoom = stableStart;
            window.__dtxGraphRenderBench.motionStableAt = stableStart;
          }
        }
        observed.push(timestamp);
        // A terminal tick must never issue another input whose response cannot
        // be observed. Previously its future window could become a false miss.
        if (end !== null && timestamp >= end) {
          window.__dtxGraphRenderBench.motionFinishedAt = timestamp;
          const native = window.__dtxGraphRenderBench.nativeFrames;
          resolve({
            initial: { duration_ms: 5_000, observed_node_count: first.size, moved_node_count: moved.size,
              native_draw_frames: native.filter((at) => at >= start && at < initialEnd).length },
            stable: { start_ms: stableStart, end_ms: timestamp, duration_ms: timestamp - stableStart, zoom_actions: zooms,
              native_draw_frames: native.filter((at) => at >= stableStart && at <= timestamp).length },
            initial_native_timestamps: native.filter((at) => at >= start && at < initialEnd),
            stable_native_timestamps: native.filter((at) => at >= stableStart && at <= timestamp),
            interaction_windows: windows,
            raf_timestamps: observed,
          });
          return;
        }
        if (timestamp < initialEnd && timestamp >= nextPositionRead) {
          const zoom = canvas.__zoom;
          if (zoom && Number.isFinite(zoom.k) && zoom.k > 0) {
            for (const label of container.querySelectorAll('[data-workspace-map-labels] span[data-node-id][data-point-x][data-point-y]')) {
              const x = (Number(label.dataset.pointX) - zoom.x) / zoom.k;
              const y = (Number(label.dataset.pointY) - zoom.y) / zoom.k;
              if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
              const id = label.dataset.nodeId;
              const previous = first.get(id);
              if (!previous) first.set(id, [x, y]);
              else if (Math.hypot(x - previous[0], y - previous[1]) > 0.01) moved.add(id);
            }
          }
          nextPositionRead = timestamp + 250;
        }
        if (window.__dtxGraphRenderBench.canIssueMotionWindow(timestamp, nextZoom, end,
          window.__dtxGraphRenderBench.motionPanActive)) {
          windows.push({ kind: "zoom", start_ms: timestamp, end_ms: timestamp + 250 });
          (zooms % 2 === 0 ? zoomIn : zoomOut).click();
          zooms += 1;
          nextZoom = timestamp + 250;
        }
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }, { selector: readySelector, duration: seconds, gated: profileGate });
  const panTrials = [];
  const pans = async () => {
    const canvas = page.locator(`${readySelector} canvas`).first();
    await canvas.scrollIntoViewIfNeeded();
    for (let index = 0; index < seconds; index += 1) {
      await page.waitForFunction((offset) => Number.isFinite(window.__dtxGraphRenderBench.motionStableAt)
        && performance.now() >= window.__dtxGraphRenderBench.motionStableAt + offset,
        index * 1_000, { polling: "raf", timeout: 10_000 });
      await page.evaluate(() => { window.__dtxGraphRenderBench.motionPanActive = true; });
      const before = await canvas.evaluate((element) => ({ ...element.__zoom, at: performance.now() }));
      const bounds = await canvas.boundingBox();
      if (!bounds) throw new Error("Native canvas disappeared during pan trial");
      const x = bounds.x + bounds.width / 2;
      const y = bounds.y + bounds.height / 2;
      await page.mouse.move(x, y);
      await page.keyboard.down("Space");
      await page.mouse.down();
      await page.mouse.move(x + (index % 2 === 0 ? 24 : -24), y + (index % 2 === 0 ? 12 : -12), { steps: 3 });
      await page.mouse.up();
      await page.keyboard.up("Space");
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const after = await canvas.evaluate((element) => ({ ...element.__zoom, at: performance.now() }));
      await page.evaluate(() => { window.__dtxGraphRenderBench.motionPanActive = false; });
      panTrials.push({ start_ms: before.at, end_ms: after.at,
        changed: Number.isFinite(before.x) && Number.isFinite(after.x) && (before.x !== after.x || before.y !== after.y),
        before: { x: before.x, y: before.y, k: before.k }, after: { x: after.x, y: after.y, k: after.k } });
    }
  };
  const [motion] = await Promise.all([observation, pans()]);
  motion.pan_trials = panTrials;
  motion.interaction_windows.push(...panTrials.map((trial) => ({ ...trial, kind: "pan" })));
  Object.assign(motion, interactiveFrameBoundaries(motion.raf_timestamps,
    motion.stable_native_timestamps, motion.interaction_windows));
  return motion;
}

/** DOM-only refresh for the SAME original ID/path; never choose a neighbor. */
function currentSameNodePoint(container, original) {
  const state = window.__dtxGraphRenderBench;
  const capture = state.readInteractionDiagnostics(original.node_id);
  const missing = (reason) => ({ reason, capture_diagnostics: capture });
  const scene = state.requests.find((request) => request.budget?.documents === 10_000)?.scene;
  const node = scene?.nodes.find((candidate) => candidate.id === original.node_id);
  if (!node || node.path !== original.path) return missing("original node identity/path no longer matches the scene");
  const label = [...container.querySelectorAll('[data-workspace-map-labels] span[data-node-id][data-point-x][data-point-y]')]
    .find((candidate) => candidate.dataset.nodeId === original.node_id);
  if (!label) return missing("original node has no current GPU-backed DOM point");
  // A successful hover promotes this same record to a full title. Its
  // data-point-x/y remain the GPU point, not the clamped title rectangle.
  const x = Number(label.dataset.pointX), y = Number(label.dataset.pointY);
  const bounds = container.getBoundingClientRect();
  if (![x, y, bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite)
    || x < 12 || y < 12 || x > bounds.width - 12 || y > bounds.height - 12) {
    return missing("original node point is nonfinite or outside the canvas interaction bounds");
  }
  const canvas = container.querySelector("canvas");
  if (!canvas || document.elementFromPoint(bounds.x + x, bounds.y + y) !== canvas) {
    return missing("original node point is blocked or no longer targets its native canvas");
  }
  return { target: { ...original, x: bounds.x + x, y: bounds.y + y, capture_diagnostics: capture } };
}

/** Each original ID gets one hover and one selection. Misses remain counted. */
export async function measureNodeInteractions(page, readySelector, count) {
  const samples = [];
  const canvas = page.locator(`${readySelector} canvas`).first();
  const container = page.locator(`${readySelector} .graph-canvas`);
  await canvas.scrollIntoViewIfNeeded();
  const admission = await neutralizeNodeInteraction(page, `${readySelector} .graph-canvas`);
  const admitted = admission.ready && admission.candidates.length >= count
    ? admission.candidates.slice(0, count).map(target => ({ ...target })) : [];
  const setupMiss = (kind, reason, target, setup) => ({ kind, status: "missed", reason, latency_ms: null,
    ...(target ? { node_id: target.node_id } : {}), diagnostics: { attempted_input: false,
      target: target ?? null, setup: setup.diagnostics, admission: { admitted_targets: admitted,
        initial: admission.diagnostics } } });
  for (let index = 0; index < count; index += 1) {
    const original = admitted[index];
    if (!original) {
      const reason = admission.ready ? "initial neutral publication has fewer distinct eligible original nodes than required"
        : "neutral publication preflight failed within 1000ms";
      samples.push(...["hover", "selection"].map(kind => setupMiss(kind, reason, null, admission)));
      continue;
    }
    const setup = index === 0 ? admission : await neutralizeNodeInteraction(page, `${readySelector} .graph-canvas`);
    if (!setup.ready) {
      samples.push(...["hover", "selection"].map(kind => setupMiss(kind, "neutral publication preflight failed within 1000ms", original, setup)));
      continue;
    }
    const refreshed = await container.evaluate(currentSameNodePoint, original);
    if (!refreshed.target) {
      samples.push(...["hover", "selection"].map(kind => setupMiss(kind, refreshed.reason, original, setup)));
      continue;
    }
    const target = refreshed.target;
    for (const kind of ["hover", "selection"]) {
      let inputTarget = target;
      if (kind === "selection") {
        const current = await page.locator(`${readySelector} .graph-canvas`).evaluate(currentSameNodePoint, target);
        if (!current.target) {
          samples.push({ kind, status: "missed", reason: current.reason, node_id: target.node_id, latency_ms: null,
            diagnostics: { target: { node_id: target.node_id, label: target.node_label, path: target.path,
              client_x: null, client_y: null }, capture: current.capture_diagnostics, dispatch: null,
            phase_events: [], phase_events_lost: 0, attempted_input: false, completion: current.capture_diagnostics } });
          await page.evaluate(() => { window.__dtxGraphRenderBench.pendingInteraction = null; });
          continue;
        }
        inputTarget = current.target;
      }
      await page.evaluate(({ targetNode, interactionKind }) => {
        window.__dtxGraphRenderBench.pendingInteraction = { ...targetNode, kind: interactionKind,
          start_ms: null, end_ms: null, framePending: false };
      }, { targetNode: inputTarget, interactionKind: kind });
      if (kind === "hover") await page.mouse.move(inputTarget.x, inputTarget.y);
      else await page.mouse.click(inputTarget.x, inputTarget.y);
      let status = "hit";
      try {
        await page.waitForFunction(() => window.__dtxGraphRenderBench.pendingInteraction?.end_ms !== null,
          undefined, { timeout: 1_000, polling: "raf" });
      } catch { status = "missed"; }
      const result = await page.evaluate(() => {
        const pending = window.__dtxGraphRenderBench.pendingInteraction;
        return { node_id: pending.node_id, latency_ms: pending.start_ms !== null && pending.end_ms !== null
          ? pending.end_ms - pending.start_ms : null,
        diagnostics: {
          target: { node_id: pending.node_id, label: pending.node_label, path: pending.path,
            client_x: pending.x, client_y: pending.y },
          capture: pending.capture_diagnostics,
          dispatch: pending.dispatch_diagnostics ?? null,
          phase_events: pending.phase_events ?? [],
          phase_events_lost: pending.phase_events_lost ?? 0,
          completion: window.__dtxGraphRenderBench.readInteractionDiagnostics(pending.node_id),
        } };
      });
      result.diagnostics.setup = setup.diagnostics;
      result.diagnostics.admission = { admitted_targets: admitted, initial: admission.diagnostics };
      samples.push({ kind, status, ...result });
    }
  }
  await page.evaluate(() => { window.__dtxGraphRenderBench.pendingInteraction = null; });
  return samples;
}

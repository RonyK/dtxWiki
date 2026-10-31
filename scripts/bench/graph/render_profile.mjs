import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

export function validateMotionProfileOptions(options) {
  if (!options.profileMotion) return;
  if (options.pc10kAcceptance) throw new Error("--profile-motion cannot be combined with --pc-10k-acceptance");
  if (options.mapScale !== "10k" || options.motionSeconds !== 5 || options.runs !== 1 || options.coldRuns !== 0
    || options.cpuRates.length !== 1 || options.cpuRates[0] !== 1
    || options.surfaces.length !== 1 || options.surfaces[0] !== "knowledge-map") {
    throw new Error("--profile-motion requires one 1x PC10K Knowledge Map run and exactly 5 stable motion seconds");
  }
}

export function validateStartupProfileOptions(options) {
  if (!options.profileStartup) return;
  if (options.pc10kAcceptance || options.profileMotion) {
    throw new Error("--profile-startup cannot be combined with acceptance or --profile-motion");
  }
  if (options.mapScale !== "10k" || ![0, 5].includes(options.motionSeconds) || options.runs !== 1 || options.coldRuns !== 0
    || options.cpuRates.length !== 1 || options.cpuRates[0] !== 1
    || options.surfaces.length !== 1 || options.surfaces[0] !== "knowledge-map") {
    throw new Error("--profile-startup requires one 1x PC10K Knowledge Map run and zero or 5 stable motion seconds");
  }
}

/** Diagnostic only: bracket the existing UI journey without subtracting it from the gate. */
export async function profileStartup({ page, cdp, runStartup, outputPrefix }) {
  await cdp.send("Profiler.enable");
  await cdp.send("Profiler.setSamplingInterval", { interval: 1_000 });
  await cdp.send("Performance.enable", { timeDomain: "timeTicks" });
  let started = false;
  let startupError = null;
  try {
    const before = await cdp.send("Performance.getMetrics");
    await cdp.send("Profiler.start");
    started = true;
    const start = await page.evaluate(() => {
      const state = window.__dtxGraphRenderBench;
      state.startup = { start_ms: performance.now(), events: [], native_first_draws: [], dom_states: [], lost_records: 0 };
      state.selectionPhaseDiagnostics = true;
      state.captureStartupDom();
      return state.startup.start_ms;
    });
    try { await runStartup(); } catch (error) { startupError = error; }
    const phases = await page.evaluate(() => {
      const state = window.__dtxGraphRenderBench;
      const result = { ...state.startup, end_ms: performance.now(),
        exact_first_native_ms: state.nativeDraws.first_scene_draw_at,
        exact_first_frame_ms: state.firstCanvasFrameAt };
      state.startup = null;
      return result;
    });
    const { profile } = await cdp.send("Profiler.stop");
    started = false;
    const after = await cdp.send("Performance.getMetrics");
    const baseline = new Map(before.metrics.map(metric => [metric.name, metric.value]));
    const names = new Set(["ScriptDuration", "LayoutDuration", "RecalcStyleDuration", "TaskDuration"]);
    const durations = Object.fromEntries(after.metrics.filter(metric => names.has(metric.name))
      .map(metric => [`${metric.name}_ms`, (metric.value - (baseline.get(metric.name) ?? metric.value)) * 1_000]));
    const body = JSON.stringify(profile);
    if (Buffer.byteLength(body) > 16 * 1024 * 1024) throw new Error("CPU profile exceeded its 16MiB diagnostic bound");
    const profileFile = `${outputPrefix}.cpuprofile`, summaryFile = `${outputPrefix}.profile.json`;
    const summary = {
      scope: "Diagnostic only: startup CPU sampling excludes performance acceptance; original responseEnd-to-full-10000 first-frame budget is unchanged.",
      page_window: { start_ms: start, end_ms: phases.end_ms, duration_ms: phases.end_ms - start },
      phases,
      cpu_profile_window_us: { start: profile.startTime, end: profile.endTime },
      performance_duration_deltas: durations,
      performance_duration_scope: "Categories overlap; profiler setup/stop add diagnostic overhead. Do not add categories or subtract UI actionability from the responseEnd gate.",
      startup_error: startupError ? String(startupError.message ?? startupError) : null,
      cpu_profile_file: profileFile,
    };
    await mkdir(path.dirname(profileFile), { recursive: true });
    await writeFile(profileFile, body, { flag: "wx" });
    await writeFile(summaryFile, `${JSON.stringify(summary, null, 2)}\n`, { flag: "wx" });
    if (phases.lost_records) throw new Error("Startup diagnostics exceeded their bounded record capacity");
    if (startupError) throw startupError;
    return { ...summary, summary_file: summaryFile };
  } finally {
    if (started) await cdp.send("Profiler.stop").catch(() => undefined);
    await page.evaluate(() => { window.__dtxGraphRenderBench.startup = null; }).catch(() => undefined);
    await cdp.send("Profiler.disable").catch(() => undefined);
    await cdp.send("Performance.disable").catch(() => undefined);
  }
}

export function summarizeReadPixels(records, startMs, endMs) {
  const groups = new Map();
  for (const record of records) {
    const overlap = Math.max(0, Math.min(record.end_ms, endMs) - Math.max(record.start_ms, startMs));
    if (record.start_ms >= endMs || record.end_ms <= startMs) continue;
    const key = `${record.target_mode}:${record.width}x${record.height}`;
    const group = groups.get(key) ?? { target_mode: record.target_mode, width: record.width, height: record.height,
      calls: 0, wall_ms: 0, max_call_ms: 0, threw: 0 };
    group.calls += 1;
    group.wall_ms += overlap;
    group.max_call_ms = Math.max(group.max_call_ms, record.end_ms - record.start_ms);
    if (record.threw) group.threw += 1;
    groups.set(key, group);
  }
  return [...groups.values()];
}

export function installReadPixelsTiming() {
  const state = window.__dtxGraphRenderBench;
  const prototype = WebGL2RenderingContext.prototype;
  const original = prototype.readPixels;
  const records = [];
  const probe = { active: false, records, lost_records: 0 };
  prototype.readPixels = function timedReadPixels(...args) {
    if (!probe.active || !this.canvas.closest?.(".graph-canvas")) return original.apply(this, args);
    const start = performance.now();
    let threw = true;
    try {
      const result = original.apply(this, args);
      threw = false;
      return result;
    } finally {
      const end = performance.now();
      if (records.length < 2_048) records.push({ start_ms: start, end_ms: end,
        width: args[2], height: args[3], threw,
        target_mode: ArrayBuffer.isView(args[6]) ? "cpu-array" : typeof args[6] === "number" ? "pbo-offset" : "other" });
      else probe.lost_records += 1;
    }
  };
  probe.restore = () => { prototype.readPixels = original; probe.active = false; };
  state.readPixelsProfile = probe;
}

/** Diagnostic capture only. Its profiler/wrapper overhead excludes acceptance. */
export async function profileStableMotion({ page, cdp, runMotion, outputPrefix }) {
  await cdp.send("Profiler.enable");
  await cdp.send("Profiler.setSamplingInterval", { interval: 1_000 });
  await cdp.send("Performance.enable", { timeDomain: "timeTicks" });
  let profilerStarted = false;
  const capture = async () => {
    await page.waitForFunction(() => window.__dtxGraphRenderBench.motionProfileWaiting === true);
    await page.evaluate(installReadPixelsTiming);
    const before = await cdp.send("Performance.getMetrics");
    await cdp.send("Profiler.start");
    profilerStarted = true;
    const start = await page.evaluate(() => {
      const state = window.__dtxGraphRenderBench;
      state.readPixelsProfile.active = true;
      state.motionProfileReleasedAt = performance.now();
      return state.motionProfileReleasedAt;
    });
    await page.waitForFunction(() => Number.isFinite(window.__dtxGraphRenderBench.motionFinishedAt));
    const end = await page.evaluate(() => {
      const state = window.__dtxGraphRenderBench;
      state.readPixelsProfile.active = false;
      return state.motionFinishedAt;
    });
    const { profile } = await cdp.send("Profiler.stop");
    profilerStarted = false;
    const after = await cdp.send("Performance.getMetrics");
    const pixels = await page.evaluate(() => {
      const probe = window.__dtxGraphRenderBench.readPixelsProfile;
      probe.restore();
      return { records: probe.records, lost_records: probe.lost_records };
    });
    if (pixels.lost_records) throw new Error("ReadPixels diagnostic exceeded its bounded record capacity");
    const baseline = new Map(before.metrics.map((metric) => [metric.name, metric.value]));
    const names = new Set(["ScriptDuration", "LayoutDuration", "RecalcStyleDuration", "TaskDuration"]);
    const durations = Object.fromEntries(after.metrics.filter((metric) => names.has(metric.name))
      .map((metric) => [`${metric.name}_ms`, (metric.value - (baseline.get(metric.name) ?? metric.value)) * 1_000]));
    const profileFile = `${outputPrefix}.cpuprofile`;
    const summaryFile = `${outputPrefix}.profile.json`;
    const profileBody = JSON.stringify(profile);
    if (Buffer.byteLength(profileBody) > 16 * 1024 * 1024) throw new Error("CPU profile exceeded its 16MiB diagnostic bound");
    const summary = {
      scope: "Diagnostic only: CPU sampling and readPixels timing overhead exclude this run from performance acceptance.",
      page_window: { start_ms: start, end_ms: end, duration_ms: end - start },
      cpu_profile_window_us: { start: profile.startTime, end: profile.endTime },
      performance_duration_deltas: durations,
      performance_duration_scope: "CDP snapshots bracket the stable window plus profiler setup/stop overhead; categories overlap and must not be added together.",
      read_pixels: { groups: summarizeReadPixels(pixels.records, start, end), records: pixels.records,
        scope: "Wall time of the existing native readPixels call only; no pixel contents, added GPU query, inferred label attribution or compositor presentation claim." },
      cpu_profile_file: profileFile,
    };
    await mkdir(path.dirname(profileFile), { recursive: true });
    await writeFile(profileFile, profileBody, { flag: "wx" });
    await writeFile(summaryFile, `${JSON.stringify(summary, null, 2)}\n`, { flag: "wx" });
    return { ...summary, summary_file: summaryFile };
  };
  try {
    const [motion, diagnostics] = await Promise.all([runMotion(), capture()]);
    return { motion, diagnostics };
  } finally {
    if (profilerStarted) await cdp.send("Profiler.stop").catch(() => undefined);
    await page.evaluate(() => window.__dtxGraphRenderBench.readPixelsProfile?.restore()).catch(() => undefined);
    await cdp.send("Profiler.disable").catch(() => undefined);
    await cdp.send("Performance.disable").catch(() => undefined);
  }
}

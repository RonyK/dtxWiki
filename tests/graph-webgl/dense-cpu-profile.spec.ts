import { expect, test, type CDPSession } from "@playwright/test";

import { createDenseCpuProfile, DENSE_CPU_PROFILE_LIMIT_BYTES, DENSE_CPU_PROFILE_WINDOW_MS,
  sanitizeCpuProfileUrl, serializeDenseCpuProfile } from "../e2e/workspace_graph_cpu_profile";

function sampleProfile() {
  return { startTime: 1_000, endTime: 8_000, nodes: [{ id: 1, callFrame: { functionName: "renderFrame", scriptId: "1",
    url: "https://user:password@cpu.e2e.invalid/assets/app.js?token=private#private", lineNumber: 1, columnNumber: 2 }, hitCount: 2 }],
    samples: [1, 1], timeDeltas: [1_000, 6_000] };
}

async function flush() { for (let index = 0; index < 24; index += 1) await Promise.resolve(); }

function harness() {
  let now = 100;
  let nextTimer = 1;
  const timers = new Map<number, { at: number; callback(): void }>();
  const calls: Array<{ method: string; parameters?: unknown }> = [];
  const handlers = new Map<string, () => Promise<unknown>>();
  const session = {
    send(method: string, parameters?: unknown) {
      calls.push({ method, parameters });
      return handlers.get(method)?.() ?? Promise.resolve(method === "Profiler.stop" ? { profile: sampleProfile() } : {});
    },
    detach() { calls.push({ method: "detach" }); return handlers.get("detach")?.() ?? Promise.resolve(); },
  } as unknown as CDPSession;
  const profile = createDenseCpuProfile(session, { now: () => now,
    setTimer(callback, delay) { const id = nextTimer++; timers.set(id, { at: now + delay, callback }); return id; },
    clearTimer(handle) { timers.delete(handle as number); },
  });
  return { profile, calls, handlers,
    async advance(duration: number) {
      const end = now + duration;
      while (true) {
        const next = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = next[1].at;
        timers.delete(next[0]);
        next[1].callback();
        await flush();
      }
      now = end;
      await flush();
    },
    timerCount: () => timers.size,
  };
}

test("dense CPU profiler starts only explicitly, then Node timer stops/detaches exactly once", async () => {
  const h = harness();
  expect(h.calls).toEqual([]);
  await h.profile.prepare();
  await h.profile.prepare();
  expect(h.calls).toEqual([{ method: "Profiler.enable", parameters: undefined },
    { method: "Profiler.setSamplingInterval", parameters: { interval: 1_000 } }]);
  await h.profile.start();
  await h.profile.start();
  await h.advance(DENSE_CPU_PROFILE_WINDOW_MS - 1);
  expect(h.calls.filter(call => call.method === "Profiler.stop")).toHaveLength(0);
  await h.advance(1);
  const first = h.profile.stop();
  expect(h.profile.stop()).toBe(first);
  const evidence = await first;
  expect(h.calls.map(call => call.method)).toEqual(["Profiler.enable", "Profiler.setSamplingInterval", "Profiler.start",
    "Profiler.stop", "Profiler.disable", "detach"]);
  expect(evidence.metadata).toMatchObject({ requestedWindowMs: 10_000, samplingIntervalUs: 1_000,
    startRequestedNodeMs: 100, startAcknowledgedNodeMs: 100, stopRequestedNodeMs: 10_100, stopCompletedNodeMs: 10_100,
    actualProfileDurationMs: 7, issues: [] });
  expect(JSON.parse(evidence.profileBody!).nodes[0].callFrame.url).toBe("https://cpu.e2e.invalid/assets/app.js");
  expect(h.timerCount()).toBe(0);
});

test("dense CPU profiler finalization preserves a partial failing-oracle window without waiting ten seconds", async () => {
  const h = harness();
  await h.profile.start();
  await h.advance(5_000);
  const evidence = await h.profile.stop();
  expect(evidence.metadata.stopRequestedNodeMs).toBe(5_100);
  expect(evidence.profileBody).not.toBeNull();
  await h.advance(20_000);
  expect(h.calls.filter(call => call.method === "Profiler.stop")).toHaveLength(1);
  expect(h.timerCount()).toBe(0);
  await h.profile.start();
  await h.profile.prepare();
  expect(h.calls.filter(call => call.method === "Profiler.start")).toHaveLength(1);
});

test("dense CPU stop deadline retains metadata and consumes late protocol rejection", async () => {
  const h = harness();
  let rejectStop: ((reason: Error) => void) | undefined;
  h.handlers.set("Profiler.stop", () => new Promise((_, reject) => { rejectStop = reject; }));
  await h.profile.start();
  const stopping = h.profile.stop();
  await flush();
  await h.advance(5_000);
  const evidence = await stopping;
  expect(evidence.profileBody).toBeNull();
  expect(evidence.metadata.issues).toEqual(["stop-deadline"]);
  expect(h.calls.at(-1)?.method).toBe("detach");
  rejectStop!(new Error("private protocol error not retained"));
  await flush();
  expect(JSON.stringify(evidence)).not.toContain("private protocol error");
  expect(h.timerCount()).toBe(0);
});

test("dense CPU prepare failure and cancelled late preparation never start sampling", async () => {
  const failed = harness();
  failed.handlers.set("Profiler.enable", () => Promise.reject(new Error("private setup error")));
  await failed.profile.start();
  const failure = await failed.profile.stop();
  expect(failure.metadata.issues).toEqual(["enable-failed"]);
  expect(failed.calls.map(call => call.method)).toEqual(["Profiler.enable", "Profiler.disable", "detach"]);
  expect(failure.profileBody).toBeNull();
  expect(JSON.stringify(failure)).not.toContain("private setup error");
  const cancelled = harness();
  let resolveEnable: ((value: unknown) => void) | undefined;
  cancelled.handlers.set("Profiler.enable", () => new Promise(resolve => { resolveEnable = resolve; }));
  const preparing = cancelled.profile.prepare();
  await cancelled.profile.stop();
  resolveEnable!({});
  await preparing;
  expect(cancelled.calls.map(call => call.method)).toEqual(["Profiler.enable", "Profiler.disable", "detach"]);
  expect(cancelled.timerCount()).toBe(0);
});

test("dense CPU late start is still stopped on the ten-second Node window", async () => {
  const h = harness();
  let rejectStart: ((reason: Error) => void) | undefined;
  h.handlers.set("Profiler.start", () => new Promise((_, reject) => { rejectStart = reject; }));
  await h.profile.prepare();
  const starting = h.profile.start();
  await flush();
  await h.advance(5_000);
  await starting;
  await h.advance(5_000);
  const evidence = await h.profile.stop();
  expect(evidence.metadata).toMatchObject({ startAcknowledgedNodeMs: null, stopRequestedNodeMs: 10_100,
    issues: ["start-deadline"] });
  rejectStart!(new Error("late private start error"));
  await flush();
  expect(h.calls.filter(call => call.method === "Profiler.stop")).toHaveLength(1);
  expect(h.timerCount()).toBe(0);
});

test("dense CPU frame URLs strip credentials/query/hash and redact opaque source strings", () => {
  for (const [input, output] of [
    ["", ""],
    ["https://user:password@cpu.e2e.invalid/assets/app.js?token=private#private", "https://cpu.e2e.invalid/assets/app.js"],
    ["http://user:password@cpu.e2e.invalid/worker.js?key=private#private", "http://cpu.e2e.invalid/worker.js"],
    ["file:///source/app.ts?key=private#private", "file:///source/app.ts"],
    ["blob:https://cpu.e2e.invalid/opaque?token=private#private", "blob:https://cpu.e2e.invalid"],
    ["data:text/javascript,private", "[redacted-url]"],
    ["javascript:private", "[redacted-url]"],
    ["not a URL private", "[redacted-url]"],
  ]) expect(sanitizeCpuProfileUrl(input)).toBe(output);
  const input = sampleProfile();
  const serialized = serializeDenseCpuProfile(input);
  expect(serialized.issue).toBeNull();
  expect(serialized.body).not.toContain("password");
  expect(serialized.body).not.toContain("private");
  expect(input.nodes[0].callFrame.url).toContain("password"); // Input is not mutated.
});

test("dense CPU profile size is bounded in UTF-8 bytes and invalid profiles are never attached", () => {
  expect(DENSE_CPU_PROFILE_LIMIT_BYTES).toBe(16 * 1024 * 1024);
  const input = sampleProfile();
  input.nodes[0].callFrame.functionName = "프레임";
  const serialized = serializeDenseCpuProfile(input);
  expect(Buffer.byteLength(serialized.body!)).toBeGreaterThan(serialized.body!.length);
  expect(serializeDenseCpuProfile(input, serialized.body!.length)).toEqual({ body: null, issue: "profile-over-byte-limit" });
  for (const value of [null, {}, { nodes: [], startTime: NaN, endTime: 1 }, { nodes: [], startTime: 2, endTime: 1 }]) {
    expect(serializeDenseCpuProfile(value)).toEqual({ body: null, issue: "invalid-profile" });
  }
});

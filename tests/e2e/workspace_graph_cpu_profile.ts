import type { CDPSession } from "@playwright/test";

export const DENSE_CPU_PROFILE_WINDOW_MS = 10_000;
export const DENSE_CPU_PROFILE_LIMIT_BYTES = 16 * 1024 * 1024;
const COMMAND_DEADLINE_MS = 5_000;

interface CpuProfile {
  nodes: Array<{ callFrame: { url: string; [key: string]: unknown }; [key: string]: unknown }>;
  startTime: number;
  endTime: number;
  [key: string]: unknown;
}

interface ProfileClock {
  now(): number;
  setTimer(callback: () => void, delay: number): unknown;
  clearTimer(handle: unknown): void;
}

const nodeClock: ProfileClock = {
  now: () => performance.now(),
  setTimer: (callback, delay) => setTimeout(callback, delay),
  clearTimer: handle => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export function sanitizeCpuProfileUrl(value: string): string {
  if (value === "") return "";
  try {
    const url = new URL(value);
    if (url.protocol === "blob:") return url.origin === "null" ? "[redacted-url]" : `blob:${url.origin}`;
    if (!["http:", "https:", "file:"].includes(url.protocol)) return "[redacted-url]";
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch { return "[redacted-url]"; }
}

/** No locals/arguments are in a V8 CPU profile; sanitize every frame URL before serialization. */
export function serializeDenseCpuProfile(value: unknown, byteLimit = DENSE_CPU_PROFILE_LIMIT_BYTES) {
  const profile = value as CpuProfile | undefined;
  if (!profile || !Array.isArray(profile.nodes) || !Number.isFinite(profile.startTime)
    || !Number.isFinite(profile.endTime) || profile.endTime < profile.startTime) {
    return { body: null, issue: "invalid-profile" };
  }
  try {
    const sanitized: CpuProfile = { ...profile, nodes: profile.nodes.map(node => ({ ...node,
      callFrame: { ...node.callFrame, url: typeof node.callFrame?.url === "string"
        ? sanitizeCpuProfileUrl(node.callFrame.url) : "[redacted-url]" },
    })) };
    const body = JSON.stringify(sanitized);
    return Buffer.byteLength(body) <= byteLimit ? { body, issue: null } : { body: null, issue: "profile-over-byte-limit" };
  } catch { return { body: null, issue: "invalid-profile" }; }
}

/** Node-owned capture: stopping/retention never depends on a page.evaluate completing. */
export function createDenseCpuProfile(session: Pick<CDPSession, "send" | "detach">, clock: ProfileClock = nodeClock) {
  const issues: string[] = [];
  let prepared = false;
  let started = false;
  let startRequestedNodeMs: number | null = null;
  let startAcknowledgedNodeMs: number | null = null;
  let stopRequestedNodeMs: number | null = null;
  let stopCompletedNodeMs: number | null = null;
  let windowTimer: unknown | null = null;
  let preparePromise: Promise<void> | null = null;
  let startPromise: Promise<void> | null = null;
  let stopPromise: Promise<{ metadata: Record<string, unknown>; profileBody: string | null }> | null = null;

  function bounded<T>(operation: Promise<T>): Promise<{ status: "returned"; value: T } | { status: "failed" | "deadline" }> {
    return new Promise(resolve => {
      let finished = false;
      const timer = clock.setTimer(() => finish({ status: "deadline" }), COMMAND_DEADLINE_MS);
      function finish(value: { status: "returned"; value: T } | { status: "failed" | "deadline" }) {
        if (finished) return;
        finished = true;
        clock.clearTimer(timer);
        resolve(value);
      }
      // Both handlers stay attached after a deadline, consuming late failures.
      operation.then(value => finish({ status: "returned", value }), () => finish({ status: "failed" }));
    });
  }

  function clearWindow() {
    if (windowTimer !== null) clock.clearTimer(windowTimer);
    windowTimer = null;
  }

  function prepare(): Promise<void> {
    if (stopPromise) return Promise.resolve();
    preparePromise ??= (async () => {
      const enabled = await bounded(session.send("Profiler.enable"));
      if (enabled.status !== "returned") { issues.push(`enable-${enabled.status}`); return; }
      if (stopPromise) return;
      const interval = await bounded(session.send("Profiler.setSamplingInterval", { interval: 1_000 }));
      if (interval.status !== "returned") { issues.push(`interval-${interval.status}`); return; }
      if (stopPromise) return;
      prepared = true;
    })();
    return preparePromise;
  }

  function start(): Promise<void> {
    if (stopPromise) return Promise.resolve();
    startPromise ??= (async () => {
      await prepare();
      if (!prepared || stopPromise) return;
      startRequestedNodeMs = clock.now();
      started = true; // A late start after a protocol deadline must still be stopped.
      windowTimer = clock.setTimer(() => { void stop(); }, DENSE_CPU_PROFILE_WINDOW_MS);
      const result = await bounded(session.send("Profiler.start"));
      if (result.status !== "returned") { issues.push(`start-${result.status}`); return; }
      startAcknowledgedNodeMs = clock.now();
    })();
    return startPromise;
  }

  function stop() {
    stopPromise ??= (async () => {
      clearWindow();
      if (startPromise) await startPromise;
      clearWindow();
      let profileBody: string | null = null;
      if (started) {
        stopRequestedNodeMs = clock.now();
        const result = await bounded(session.send("Profiler.stop"));
        stopCompletedNodeMs = clock.now();
        if (result.status === "returned") {
          const serialized = serializeDenseCpuProfile(result.value.profile);
          profileBody = serialized.body;
          if (serialized.issue) issues.push(serialized.issue);
        } else issues.push(`stop-${result.status}`);
      }
      const disabled = await bounded(session.send("Profiler.disable"));
      if (disabled.status !== "returned") issues.push(`disable-${disabled.status}`);
      const detached = await bounded(session.detach());
      if (detached.status !== "returned") issues.push(`detach-${detached.status}`);
      const profile = profileBody ? JSON.parse(profileBody) as CpuProfile : null;
      return { profileBody, metadata: {
        scope: "Opt-in V8 CPU sampling of the unchanged first dense DOM oracle. Diagnostic overhead excludes SLA/acceptance claims. No page evaluation is used for stop/retention; CDP command service can overshoot the Node timer.",
        requestedWindowMs: DENSE_CPU_PROFILE_WINDOW_MS, samplingIntervalUs: 1_000,
        profileLimitBytes: DENSE_CPU_PROFILE_LIMIT_BYTES, startRequestedNodeMs, startAcknowledgedNodeMs,
        stopRequestedNodeMs, stopCompletedNodeMs, profileStartUs: profile?.startTime ?? null,
        profileEndUs: profile?.endTime ?? null, actualProfileDurationMs: profile ? (profile.endTime - profile.startTime) / 1_000 : null,
        sanitizedProfileBytes: profileBody ? Buffer.byteLength(profileBody) : 0, issues,
      } };
    })();
    return stopPromise;
  }

  return { prepare, start, stop };
}

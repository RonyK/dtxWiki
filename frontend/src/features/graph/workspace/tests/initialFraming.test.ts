import { describe, expect, it } from "vitest";

import {
  createInitialWorkspaceFramingGate,
  CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY,
  INITIAL_WORKSPACE_FRAMING_INTERVAL_MS,
  INITIAL_WORKSPACE_FRAMING_WINDOW_MS,
} from "../initialFraming";

describe("initial Workspace framing gate", () => {
  it("keeps the behavioral window and maximum cadence explicit", () => {
    expect(INITIAL_WORKSPACE_FRAMING_WINDOW_MS).toBe(4_000);
    expect(INITIAL_WORKSPACE_FRAMING_INTERVAL_MS).toBe(200);
  });

  it("does not grant disabled scenes permission", () => {
    const gate = createInitialWorkspaceFramingGate(false);
    expect(gate.isActive()).toBe(false);
    for (const now of [0, 200, 4_000, 100_000, NaN]) expect(gate.claim(now)).toBe(false);
    gate.cancel();
    expect(gate.isActive()).toBe(false);
  });

  it("starts the lifetime lazily at the first valid request", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.isActive()).toBe(true);
    expect(gate.claim(100_000)).toBe(true);
    expect(gate.claim(103_999)).toBe(true);
    expect(gate.claim(104_000)).toBe(false);
    expect(gate.isActive()).toBe(false);
  });

  it("claims at the interval boundary, not just before it or twice at one timestamp", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(25)).toBe(true);
    expect(gate.claim(25)).toBe(false);
    expect(gate.claim(224.999)).toBe(false);
    expect(gate.claim(225)).toBe(true);
    expect(gate.claim(225)).toBe(false);
    expect(gate.claim(424.999)).toBe(false);
    expect(gate.claim(425)).toBe(true);
    expect(gate.isActive()).toBe(true);
  });

  it("allows the last instant before the exclusive deadline and permanently expires at it", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(1_000)).toBe(true);
    expect(gate.claim(4_600)).toBe(true);
    expect(gate.claim(4_999.999)).toBe(true);
    expect(gate.claim(5_000)).toBe(false);
    expect(gate.claim(5_200)).toBe(false);
    expect(gate.claim(0)).toBe(false);
    expect(gate.isActive()).toBe(false);
  });

  it("grants at most twenty claims across one four-second window", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    const claimed: number[] = [];
    for (let now = 0; now <= 4_000; now += 10) if (gate.claim(now)) claimed.push(now);
    expect(claimed).toEqual(Array.from({ length: 20 }, (_, index) => index * 200));
    expect(gate.isActive()).toBe(false);
  });

  it("permanently consumes cancellation before the first request", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    gate.cancel();
    gate.cancel();
    expect(gate.isActive()).toBe(false);
    expect(gate.claim(0)).toBe(false);
    expect(gate.claim(100_000)).toBe(false);
  });

  it("cannot rearm after cancellation of a started window", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(0)).toBe(true);
    gate.cancel();
    for (const now of [200, 4_000, 100_000]) expect(gate.claim(now)).toBe(false);
    expect(Object.keys(gate).sort()).toEqual(["cancel", "claim", "isActive"]);
    expect(gate.isActive()).toBe(false);
  });

  it.each([NaN, Infinity, -Infinity])("does not start a lifetime with invalid timestamp %s", (invalid) => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(invalid)).toBe(false);
    expect(gate.isActive()).toBe(true);
    expect(gate.claim(10_000)).toBe(true);
    expect(gate.claim(14_000)).toBe(false);
  });

  it.each([NaN, Infinity, -Infinity])("fails closed on invalid timestamp %s after starting", (invalid) => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(0)).toBe(true);
    expect(gate.claim(invalid)).toBe(false);
    expect(gate.isActive()).toBe(false);
    expect(gate.claim(200)).toBe(false);
  });

  it("fails closed when the clock moves backwards after a claim", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(1_000)).toBe(true);
    expect(gate.claim(999)).toBe(false);
    expect(gate.isActive()).toBe(false);
    expect(gate.claim(1_200)).toBe(false);
  });

  it("checks monotonicity against throttled requests, not only successful claims", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(1_000)).toBe(true);
    expect(gate.claim(1_150)).toBe(false);
    expect(gate.claim(1_100)).toBe(false);
    expect(gate.isActive()).toBe(false);
    expect(gate.claim(1_200)).toBe(false);
  });

  it("does not require a particular clock origin", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(-1_000)).toBe(true);
    expect(gate.claim(-801)).toBe(false);
    expect(gate.claim(-800)).toBe(true);
    expect(gate.claim(3_000)).toBe(false);
  });

  it("expires immediately on a large forward jump without restarting its window", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(0)).toBe(true);
    expect(gate.claim(Number.MAX_VALUE)).toBe(false);
    expect(gate.isActive()).toBe(false);
    expect(gate.claim(Number.MAX_VALUE)).toBe(false);
  });

  it("rejects elapsed overflow between finite timestamps", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(-1e308)).toBe(true);
    expect(gate.claim(1e308)).toBe(false);
    expect(gate.isActive()).toBe(false);
  });

  it("keeps independent scene gates isolated", () => {
    const first = createInitialWorkspaceFramingGate(true);
    const second = createInitialWorkspaceFramingGate(true);
    expect(first.claim(0)).toBe(true);
    first.cancel();
    expect(second.isActive()).toBe(true);
    expect(second.claim(10_000)).toBe(true);
    expect(first.claim(10_000)).toBe(false);
  });

  it("rechecks expiry of a queued claim without making another claim", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(0)).toBe(true);
    expect(gate.claim(3_999)).toBe(true);
    expect(gate.isActive(3_999.999)).toBe(true);
    expect(gate.isActive(4_000)).toBe(false);
    expect(gate.isActive()).toBe(false);
    expect(gate.claim(4_200)).toBe(false);
  });

  it("does not start the lazy lifetime through clock-aware queries", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.isActive(0)).toBe(true);
    expect(gate.isActive(100_000)).toBe(true);
    expect(gate.claim(100_000)).toBe(true);
    expect(gate.isActive(103_999)).toBe(true);
    expect(gate.isActive(104_000)).toBe(false);
  });

  it("does not consume the claim interval through frequent clock-aware queries", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(0)).toBe(true);
    for (const now of [0, 50, 100, 150, 199.999, 200]) expect(gate.isActive(now)).toBe(true);
    expect(gate.claim(200)).toBe(true);
    expect(gate.claim(200)).toBe(false);
    for (const now of [250, 300, 399.999, 400]) expect(gate.isActive(now)).toBe(true);
    expect(gate.claim(400)).toBe(true);
  });

  it("does not reactivate canceled permission through either query mode", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    gate.cancel();
    expect(gate.isActive()).toBe(false);
    for (const now of [0, 100_000, NaN, Infinity]) expect(gate.isActive(now)).toBe(false);
    expect(gate.claim(100_000)).toBe(false);
  });

  it.each([NaN, Infinity, -Infinity])("rejects invalid query %s before the first claim without consuming permission", (invalid) => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.isActive(invalid)).toBe(false);
    expect(gate.isActive()).toBe(true);
    expect(gate.isActive(10_000)).toBe(true);
    expect(gate.claim(20_000)).toBe(true);
    expect(gate.isActive(24_000)).toBe(false);
  });

  it.each([NaN, Infinity, -Infinity])("fails closed on invalid query %s after the first claim", (invalid) => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(0)).toBe(true);
    expect(gate.isActive(invalid)).toBe(false);
    expect(gate.isActive()).toBe(false);
    expect(gate.claim(200)).toBe(false);
  });

  it("shares monotonic clock validation between claims and queries", () => {
    const gate = createInitialWorkspaceFramingGate(true);
    expect(gate.claim(0)).toBe(true);
    expect(gate.isActive(100)).toBe(true);
    expect(gate.claim(99)).toBe(false);
    expect(gate.isActive()).toBe(false);

    const queried = createInitialWorkspaceFramingGate(true);
    expect(queried.claim(0)).toBe(true);
    expect(queried.claim(100)).toBe(false);
    expect(queried.isActive(99)).toBe(false);
    expect(queried.isActive()).toBe(false);
  });

  it("exposes one immutable CPU profile with a 30-second window and 500-ms cadence", () => {
    expect(CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY).toEqual({ windowMs: 30_000, intervalMs: 500 });
    expect(Object.isFrozen(CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY)).toBe(true);
  });

  it("grants at most sixty CPU claims and cannot rearm at forty seconds", () => {
    const gate = createInitialWorkspaceFramingGate(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    const claimed: number[] = [];
    for (let now = 0; now <= 40_000; now += 10) if (gate.claim(now)) claimed.push(now);
    expect(claimed).toEqual(Array.from({ length: 60 }, (_, index) => index * 500));
    expect(gate.isActive()).toBe(false);
    expect(gate.claim(100_000)).toBe(false);
  });

  it("anchors a CPU deadline at the first observed tick instead of a delayed first claim", () => {
    const gate = createInitialWorkspaceFramingGate(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    expect(gate.isActive(100)).toBe(true);
    expect(gate.claim(30_099)).toBe(true);
    expect(gate.claim(30_100)).toBe(false);
    expect(gate.claim(40_100)).toBe(false);
  });

  it("snapshots a bounded caller policy instead of retaining mutable options", () => {
    const policy = { windowMs: 30_000, intervalMs: 500 };
    const gate = createInitialWorkspaceFramingGate(true, policy);
    policy.windowMs = 1e9; policy.intervalMs = 1;
    expect(gate.claim(0)).toBe(true);
    expect(gate.claim(499)).toBe(false);
    expect(gate.claim(500)).toBe(true);
    expect(gate.isActive(30_000)).toBe(false);
  });

  it.each([
    { windowMs: 0, intervalMs: 500 }, { windowMs: -1, intervalMs: 500 },
    { windowMs: 30_001, intervalMs: 500 }, { windowMs: Infinity, intervalMs: 500 },
    { windowMs: NaN, intervalMs: 500 }, { windowMs: 30_000, intervalMs: 0 },
    { windowMs: 30_000, intervalMs: -1 }, { windowMs: 30_000, intervalMs: NaN },
    { windowMs: 30_000, intervalMs: Infinity }, { windowMs: 30_000, intervalMs: 499 },
  ])("rejects an invalid or unbounded explicit policy %s", policy => {
    expect(() => createInitialWorkspaceFramingGate(true, policy)).toThrow();
  });

  it.each([NaN, Infinity, -Infinity])("fails closed on CPU clock failure %s after its first observation", invalid => {
    const gate = createInitialWorkspaceFramingGate(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    expect(gate.isActive(0)).toBe(true);
    expect(gate.isActive(invalid)).toBe(false);
    expect(gate.claim(500)).toBe(false);
  });

  it("fails closed when CPU observation time reverses before the first claim", () => {
    const gate = createInitialWorkspaceFramingGate(true, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    expect(gate.isActive(100)).toBe(true);
    expect(gate.isActive(99)).toBe(false);
    expect(gate.claim(500)).toBe(false);
  });
});

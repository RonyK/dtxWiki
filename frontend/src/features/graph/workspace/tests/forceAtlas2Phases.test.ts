import { describe, expect, it } from "vitest";

import {
  createWorkspaceForceAtlas2PhaseGate,
  isWorkspaceForceAtlas2Progress,
  snapshotWorkspaceForceAtlas2Progress,
  type WorkspaceForceAtlas2PhaseInput,
  type WorkspaceForceAtlas2Progress,
} from "../forceAtlas2Phases";

function input(overrides: Partial<WorkspaceForceAtlas2PhaseInput> = {}): WorkspaceForceAtlas2PhaseInput {
  return { positions: Float32Array.from([0, 0, 100, 0]), radii: Float32Array.from([8, 8]),
    sources: Uint32Array.from([0, 0, 1]), targets: Uint32Array.from([1, 1, 1]), ...overrides };
}

function accepted(gate: ReturnType<typeof createWorkspaceForceAtlas2PhaseGate>, positions: Float32Array, count: number) {
  let progress = snapshotWorkspaceForceAtlas2Progress();
  for (let iteration = 0; iteration < count; iteration++) progress = gate.accept(positions);
  return progress;
}

describe("immutable ephemeral ForceAtlas2 progress", () => {
  it("starts in structure and snapshots rather than retaining caller state", () => {
    const original = { ...snapshotWorkspaceForceAtlas2Progress(), iterations: 8, structureIterations: 8 };
    const copy = snapshotWorkspaceForceAtlas2Progress(original);
    original.iterations = 99;
    expect(copy.iterations).toBe(8);
    expect(Object.isFrozen(copy)).toBe(true);
    expect(copy.profile).toBe("gephi-lite-2k-v1");
  });

  it.each([
    null, {}, { profile: "old-profile", phase: "structure", iterations: 0, structureIterations: 0, transitionReason: null },
    { ...snapshotWorkspaceForceAtlas2Progress(), iterations: -1, structureIterations: -1 },
    { ...snapshotWorkspaceForceAtlas2Progress(), iterations: .5, structureIterations: .5 },
    { ...snapshotWorkspaceForceAtlas2Progress(), iterations: NaN },
    { ...snapshotWorkspaceForceAtlas2Progress(), iterations: Infinity },
    { ...snapshotWorkspaceForceAtlas2Progress(), iterations: Number.MAX_SAFE_INTEGER + 1 },
    { ...snapshotWorkspaceForceAtlas2Progress(), iterations: 8, structureIterations: 9 },
    { ...snapshotWorkspaceForceAtlas2Progress(), iterations: 9, structureIterations: 8 },
    { ...snapshotWorkspaceForceAtlas2Progress(), iterations: 256, structureIterations: 256 },
    { ...snapshotWorkspaceForceAtlas2Progress(), transitionReason: "stable" },
    { ...snapshotWorkspaceForceAtlas2Progress(), phase: "contact", iterations: 8, structureIterations: 8, transitionReason: "stable" },
    { ...snapshotWorkspaceForceAtlas2Progress(), phase: "contact", iterations: 32, structureIterations: 32, transitionReason: null },
    { ...snapshotWorkspaceForceAtlas2Progress(), phase: "contact", iterations: 32, structureIterations: 32, transitionReason: "budget" },
    { ...snapshotWorkspaceForceAtlas2Progress(), phase: "contact", iterations: 300, structureIterations: 257, transitionReason: "stable" },
  ])("rejects unreachable or foreign progress: %s", value => {
    expect(isWorkspaceForceAtlas2Progress(value)).toBe(false);
    expect(() => snapshotWorkspaceForceAtlas2Progress(value as WorkspaceForceAtlas2Progress)).toThrow();
  });

  it("accepts reachable contact progress and preserves the original handoff reason", () => {
    for (const progress of [
      { profile: "gephi-lite-2k-v1", phase: "contact", iterations: 40, structureIterations: 32, transitionReason: "stable" },
      { profile: "gephi-lite-2k-v1", phase: "contact", iterations: 257, structureIterations: 256, transitionReason: "budget" },
    ] as const) {
      expect(isWorkspaceForceAtlas2Progress(progress)).toBe(true);
      const gate = createWorkspaceForceAtlas2PhaseGate(input({ progress }));
      expect(gate.accept(Float32Array.from([0, 0, 100, 0]))).toEqual({ ...progress, iterations: progress.iterations + 1 });
    }
  });
});

describe("official update structure/contact policy", () => {
  it("requires four stable eight-update intervals from an initial sample and at least 32 updates", () => {
    const columns = input();
    const gate = createWorkspaceForceAtlas2PhaseGate(columns);
    expect(accepted(gate, columns.positions, 31)).toMatchObject({ phase: "structure", iterations: 31, transitionReason: null });
    expect(gate.accept(columns.positions)).toMatchObject({ phase: "contact", iterations: 32,
      structureIterations: 32, transitionReason: "stable" });
  });

  it("clears consecutive stability after a moving checkpoint", () => {
    const columns = input(), gate = createWorkspaceForceAtlas2PhaseGate(columns);
    accepted(gate, columns.positions, 24);
    const moved = Float32Array.from([100, 0, 200, 0]);
    expect(accepted(gate, moved, 8)).toMatchObject({ phase: "structure", iterations: 32 });
    expect(accepted(gate, moved, 31)).toMatchObject({ phase: "structure", iterations: 63 });
    expect(gate.accept(moved)).toMatchObject({ phase: "contact", structureIterations: 64, transitionReason: "stable" });
  });

  it("does not mistake stable distributions for small movement", () => {
    const gate = createWorkspaceForceAtlas2PhaseGate(input());
    let progress = snapshotWorkspaceForceAtlas2Progress();
    for (let iteration = 1; iteration <= 32; iteration++) {
      progress = gate.accept(Float32Array.from([iteration * 4, 0, 100 + iteration * 4, 0]));
    }
    expect(progress.phase).toBe("structure");
  });

  it("rejects unstable extent/edge distribution even when movement is within the radius budget", () => {
    const columns = input({ positions: Float32Array.from([0, 0, 1000, 0]), radii: Float32Array.from([100, 100]) });
    const gate = createWorkspaceForceAtlas2PhaseGate(columns);
    let progress = snapshotWorkspaceForceAtlas2Progress();
    for (let iteration = 1; iteration <= 32; iteration++) {
      progress = gate.accept(Float32Array.from([0, 0, 1000 + iteration * 3, 0]));
    }
    expect(progress.phase).toBe("structure");
  });

  it("labels the 256-update cap budget, never convergence", () => {
    const gate = createWorkspaceForceAtlas2PhaseGate(input());
    let progress = snapshotWorkspaceForceAtlas2Progress();
    for (let iteration = 1; iteration <= 256; iteration++) {
      progress = gate.accept(Float32Array.from([iteration * 4, 0, 100 + iteration * 4, 0]));
      if (iteration < 256) expect(progress.phase).toBe("structure");
    }
    expect(progress).toEqual({ profile: "gephi-lite-2k-v1", phase: "contact", iterations: 256,
      structureIterations: 256, transitionReason: "budget" });
  });

  it("resets interval history on recreation while retaining published structure counters", () => {
    const columns = input({ progress: { ...snapshotWorkspaceForceAtlas2Progress(), iterations: 24, structureIterations: 24 } });
    const gate = createWorkspaceForceAtlas2PhaseGate(columns);
    expect(accepted(gate, columns.positions, 31)).toMatchObject({ phase: "structure", iterations: 55 });
    expect(gate.accept(columns.positions)).toMatchObject({ phase: "contact", iterations: 56, transitionReason: "stable" });
  });

  it("can hit the retained budget before a fresh eight-update interval", () => {
    const columns = input({ progress: { ...snapshotWorkspaceForceAtlas2Progress(), iterations: 255, structureIterations: 255 } });
    expect(createWorkspaceForceAtlas2PhaseGate(columns).accept(columns.positions))
      .toMatchObject({ phase: "contact", structureIterations: 256, transitionReason: "budget" });
  });

  it("does not include pinned rows in RMS while still measuring scene/edge distribution", () => {
    const columns = input({ pins: [0], positions: Float32Array.from([0, 0, 10000, 0]) });
    const gate = createWorkspaceForceAtlas2PhaseGate(columns);
    // Moving the pin is a policy-only probe: the bridge separately fixes it.
    // This scene/edge drift is below 1%, but RMS would
    // reject the 16-unit shift if the pinned row were incorrectly included.
    const moved = Float32Array.from([16, 0, 10000, 0]);
    expect(accepted(gate, moved, 32)).toMatchObject({ phase: "contact", transitionReason: "stable" });
  });

  it("snapshots original occurrence endpoints/radii/pins and observation rows", () => {
    const columns = input(), initial = columns.positions.slice();
    const gate = createWorkspaceForceAtlas2PhaseGate(columns);
    columns.radii.fill(1e9); columns.sources.fill(1); columns.targets.fill(1); columns.positions.fill(9000);
    const observed = initial.slice();
    accepted(gate, observed, 8); observed.fill(9999);
    expect(accepted(gate, initial, 24)).toMatchObject({ phase: "contact", transitionReason: "stable" });
  });

  it("handles a 2K no-edge scene with bounded metrics and keeps progress immutable", () => {
    const columns = input({ positions: new Float32Array(4000), radii: new Float32Array(2000).fill(1),
      sources: new Uint32Array(), targets: new Uint32Array() });
    const progress = accepted(createWorkspaceForceAtlas2PhaseGate(columns), columns.positions, 32);
    expect(progress.transitionReason).toBe("stable");
    expect(Object.isFrozen(progress)).toBe(true);
  });

  it("rejects malformed observations without advancing the accepted update count", () => {
    const columns = input(), gate = createWorkspaceForceAtlas2PhaseGate(columns);
    for (const rows of [new Float32Array(2), Float32Array.from([NaN, 0, 100, 0])]) {
      expect(() => gate.accept(rows)).toThrow();
    }
    expect(gate.accept(columns.positions).iterations).toBe(1);
  });
});

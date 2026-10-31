import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";
import { WORKSPACE_MAP_DEFAULT_BUDGET, WORKSPACE_MAP_PC_10K_BUDGET } from "../../shared/workspaceMapBudget";
import { createInitialWorkspaceFramingGate, CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY } from "../initialFraming";
import { workspaceInitialCameraPolicy } from "../initialCameraPolicy";

describe("initial camera scale policy", () => {
  it("never grants a camera policy beyond the renderer's admitted scale", () => {
    expect(workspaceInitialCameraPolicy(10_000, WORKSPACE_MAP_DEFAULT_BUDGET, false).enabled).toBe(false);
    expect(workspaceInitialCameraPolicy(10_000, WORKSPACE_MAP_PC_10K_BUDGET, false).enabled).toBe(true);
    expect(workspaceInitialCameraPolicy(10_001, { documents: 20_000, relations: 100_000 }, false).enabled).toBe(false);
  });

  it("preserves both accepted 2K framing lifetimes", () => {
    const cpu = workspaceInitialCameraPolicy(2_000, WORKSPACE_MAP_DEFAULT_BUDGET, true);
    expect(cpu.framingPolicy).toBe(CPU_STRUCTURE_WORKSPACE_FRAMING_POLICY);
    const native = workspaceInitialCameraPolicy(2_000, WORKSPACE_MAP_PC_10K_BUDGET, false);
    expect(native.framingPolicy).toBeUndefined();
  });

  it("grants one terminal Fit only to native PC maps above the 2K boundary", () => {
    expect(workspaceInitialCameraPolicy(10_000, WORKSPACE_MAP_PC_10K_BUDGET, false).terminalFit).toBeDefined();
    expect(workspaceInitialCameraPolicy(2_001, WORKSPACE_MAP_PC_10K_BUDGET, false).terminalFit).toBeDefined();
    // The CPU path ends with its own structure Fit; 2K-sized native scenes keep their accepted policy.
    expect(workspaceInitialCameraPolicy(2_000, WORKSPACE_MAP_DEFAULT_BUDGET, true).terminalFit).toBeUndefined();
    expect(workspaceInitialCameraPolicy(2_000, WORKSPACE_MAP_PC_10K_BUDGET, false).terminalFit).toBeUndefined();
  });

  it("places the cooled threshold where the pinned native engine's alpha has contracted the layout, about eight seconds in", () => {
    // The threshold is a tick count in disguise. Derive it from the installed SDK so a
    // changed decay or alpha floor fails here instead of silently moving the Fit.
    const map = JSON.parse(readFileSync(`${createRequire(import.meta.url).resolve("@cosmos.gl/graph")}.map`, "utf8")) as {
      sources: string[]; sourcesContent: string[];
    };
    const source = (name: string) => map.sourcesContent[map.sources.indexOf(name)];
    const alphaMin = Number(/export const ALPHA_MIN = ([\d.e-]+)/.exec(source("../src/modules/Store/index.ts"))?.[1]);
    const decay = Number(/simulationDecay: (\d+)/.exec(source("../src/variables.ts"))?.[1]);
    expect(source("../src/modules/Store/index.ts")).toContain("1 - Math.pow(ALPHA_MIN, 1 / decay)");
    const startAlpha = .35; // the alpha the adapter starts every native layout at
    const { terminalFit } = workspaceInitialCameraPolicy(10_000, WORKSPACE_MAP_PC_10K_BUDGET, false);
    const ticks = Math.log(terminalFit!.cooledAlpha / startAlpha) / Math.log(Math.pow(alphaMin, 1 / decay));
    expect(Number.isFinite(ticks)).toBe(true);
    const seconds = ticks / 60;
    expect(seconds).toBeGreaterThan(6);
    expect(seconds).toBeLessThan(12);
    // A 60 Hz display must reach it with room to spare inside the thirty-second lifetime.
    expect(seconds * 2).toBeLessThan(terminalFit!.windowMs / 1000);
  });

  it("expires PC initialization even when a pending native Fit blocks every claim", () => {
    const policy = workspaceInitialCameraPolicy(10_000, WORKSPACE_MAP_PC_10K_BUDGET, false);
    const gate = createInitialWorkspaceFramingGate(policy.enabled, policy.framingPolicy);
    expect(gate.isActive(1_000)).toBe(true);
    expect(gate.isActive(4_999)).toBe(true);
    expect(gate.isActive(5_000)).toBe(false);
    expect(gate.claim(60_000)).toBe(false);
  });
});

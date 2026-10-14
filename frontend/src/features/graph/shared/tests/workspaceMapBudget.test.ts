import { afterEach, describe, expect, it, vi } from "vitest";

import {
  assertWorkspaceMapWithinBudget, classifyWorkspaceMapDevice, isWorkspaceMapWithinBudget,
  readWorkspaceMapDeviceProfile, WORKSPACE_MAP_DEFAULT_BUDGET, WORKSPACE_MAP_PC_10K_BUDGET,
  workspaceMapBudget, type WorkspaceMapDeviceSignals,
} from "../workspaceMapBudget";

const pc: WorkspaceMapDeviceSignals = {
  userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", platform: "Win32",
  maxTouchPoints: 0, finePointer: true, hover: true,
};

afterEach(() => vi.unstubAllGlobals());

describe("Workspace Map device budget", () => {
  it.each([
    [{}, "desktop"],
    [{ userAgent: "Mozilla/5.0 (X11; Linux x86_64)", platform: "Linux x86_64" }, "desktop"],
    [{ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X)", platform: "MacIntel" }, "desktop"],
    [{ maxTouchPoints: 10 }, "desktop"],
    [{ mobileHint: true }, "mobile"],
    [{ userAgent: "Mozilla/5.0 (Linux; Android 13)", mobileHint: false }, "mobile"],
    [{ userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS)", mobileHint: false }, "mobile"],
    [{ userAgent: "Mozilla/5.0 (iPad; CPU OS)", platform: "iPad" }, "mobile"],
    [{ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X)", platform: "MacIntel", maxTouchPoints: 5 }, "mobile"],
    [{ userAgent: "private browser", mobileHint: false }, "unknown"],
    [{ userAgent: "" }, "unknown"],
    [{ finePointer: false }, "unknown"],
    [{ hover: false }, "unknown"],
    [{ maxTouchPoints: Number.NaN }, "unknown"],
    [{ maxTouchPoints: -1 }, "unknown"],
    [{ maxTouchPoints: 0.5 }, "unknown"],
  ] as const)("classifies signals %j without a viewport dependency", (change, expected) => {
    expect(classifyWorkspaceMapDevice({ ...pc, ...change })).toBe(expected);
  });

  it.each(["desktop", "mobile", "unknown"] as const)("keeps %s at 2K by default", (profile) => {
    expect(workspaceMapBudget(profile, "2k")).toEqual({ documents: 2_000, relations: 6_000 });
  });
  it.each(["mobile", "unknown"] as const)("cannot up-budget %s with an explicit 10K request", (profile) => {
    expect(workspaceMapBudget(profile, "10k")).toEqual(WORKSPACE_MAP_DEFAULT_BUDGET);
  });
  it("only permits explicit desktop 10K and returns an independent budget", () => {
    const budget = workspaceMapBudget("desktop", "10k");
    expect(budget).toEqual(WORKSPACE_MAP_PC_10K_BUDGET);
    budget.documents = 1;
    expect(workspaceMapBudget("desktop", "10k").documents).toBe(10_000);
    expect(workspaceMapBudget("desktop", "20k" as "10k")).toEqual(WORKSPACE_MAP_DEFAULT_BUDGET);
  });

  it.each([
    [0, 0, true], [2_000, 6_000, true], [2_001, 1, false], [1, 6_001, false],
    [-1, 0, false], [1.5, 0, false], [Number.NaN, 0, false], [1, Number.POSITIVE_INFINITY, false],
  ])("admits actual rendered count %s/%s within default budget: %s", (nodeCount, edgeCount, expected) => {
    expect(isWorkspaceMapWithinBudget({ nodeCount, edgeCount }, WORKSPACE_MAP_DEFAULT_BUDGET)).toBe(expected);
  });

  it("admits desktop 10K without admitting its nodes or edges on mobile", () => {
    const counts = { nodeCount: 10_000, edgeCount: 100_000 };
    expect(isWorkspaceMapWithinBudget(counts, workspaceMapBudget("desktop", "10k"))).toBe(true);
    expect(isWorkspaceMapWithinBudget(counts, workspaceMapBudget("mobile", "10k"))).toBe(false);
    expect(isWorkspaceMapWithinBudget({ nodeCount: 10_001, edgeCount: 0 }, WORKSPACE_MAP_PC_10K_BUDGET)).toBe(false);
    expect(isWorkspaceMapWithinBudget({ nodeCount: 1, edgeCount: 100_001 }, WORKSPACE_MAP_PC_10K_BUDGET)).toBe(false);
    expect(() => assertWorkspaceMapWithinBudget(counts, WORKSPACE_MAP_DEFAULT_BUDGET)).toThrow(/requested budget/);
  });

  it("does not mistake one represented cluster for its member count", () => {
    expect(() => assertWorkspaceMapWithinBudget({ nodeCount: 1, edgeCount: 0 }, WORKSPACE_MAP_DEFAULT_BUDGET)).not.toThrow();
    expect(isWorkspaceMapWithinBudget({ nodeCount: 0, edgeCount: 0 }, { documents: 0, relations: 1 })).toBe(false);
  });

  it("reads desktop signals once without reading window dimensions", () => {
    vi.stubGlobal("navigator", { ...pc, userAgentData: { mobile: false } });
    const queries: string[] = [];
    vi.stubGlobal("window", {
      get innerWidth(): never { throw new Error("viewport must not classify device"); },
      matchMedia(query: string) { queries.push(query); return { matches: true }; },
    });
    expect(readWorkspaceMapDeviceProfile()).toBe("desktop");
    expect(queries).toEqual(["(pointer: fine)", "(hover: hover)"]);
  });

  it("falls back to unknown for inaccessible browser hints", () => {
    vi.stubGlobal("navigator", { get userAgent(): never { throw new Error("private"); } });
    expect(readWorkspaceMapDeviceProfile()).toBe("unknown");
  });
});

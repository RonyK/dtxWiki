import { describe, expect, it } from "vitest";

import {
  contextPageHref,
  hasConflictingWorkspaceQuery,
  isValidWorkspaceRoute,
  legacyWorkspaceEntry,
  parseWorkspaceRoute,
  stableContextPageHref,
  workspaceIdFromPathname,
  workspacePath,
} from "./workspaceRoutes";

const WORKSPACE_ID = "22222222-2222-4222-8222-000000000001";
const PAGE_ID = "33333333-3333-4333-8333-333333333333";

describe("workspaceRoutes", () => {
  it("builds canonical page links and parses pathname identity", () => {
    expect(contextPageHref(WORKSPACE_ID.toUpperCase(), PAGE_ID, "#intro")).toBe(
      `/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}#intro`,
    );
    expect(() => contextPageHref(WORKSPACE_ID, "page-a")).toThrow("Context URLs require workspace and page UUIDs");
    expect(stableContextPageHref(WORKSPACE_ID, PAGE_ID)).toBe(`/p/${WORKSPACE_ID}/${PAGE_ID}`);
    expect(parseWorkspaceRoute(`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/edit`)).toMatchObject({
      workspaceId: WORKSPACE_ID,
      section: "context",
      targetId: PAGE_ID,
      targetKind: "page",
      edit: true,
    });
  });

  it("requires UUID identity and rejects a competing query target", () => {
    expect(() => workspacePath("workspace-a", "context")).toThrow("An explicit workspace UUID is required");
    const route = parseWorkspaceRoute(`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`);
    expect(route).not.toBeNull();
    expect(isValidWorkspaceRoute(route!, "?view=history")).toBe(true);
    expect(isValidWorkspaceRoute(route!, `?target=${PAGE_ID}`)).toBe(false);
    expect(isValidWorkspaceRoute({ ...route!, targetId: "not-a-uuid" }, "")).toBe(false);
    expect(hasConflictingWorkspaceQuery("?parent=" + PAGE_ID)).toBe(true);
  });

  it("extracts only a valid workspace UUID prefix for route-aware recovery", () => {
    expect(workspaceIdFromPathname(`/workspaces/${WORKSPACE_ID}/unknown/suffix`)).toBe(WORKSPACE_ID);
    expect(workspaceIdFromPathname("/workspaces/not-a-uuid/context")).toBeNull();
    expect(workspaceIdFromPathname("/context")).toBeNull();
  });

  it("accepts a parent only on target-free create routes and limits legacy landing hints", () => {
    const create = parseWorkspaceRoute(`/workspaces/${WORKSPACE_ID}/context/pages/new`);
    expect(create).not.toBeNull();
    expect(isValidWorkspaceRoute(create!, `?parent=${PAGE_ID}`)).toBe(true);
    expect(isValidWorkspaceRoute(create!, "?parent=not-a-uuid")).toBe(false);
    expect(legacyWorkspaceEntry("/context", "")).toBe("context");
    expect(legacyWorkspaceEntry("/context", `?target=${PAGE_ID}`)).toBeNull();
    expect(legacyWorkspaceEntry(`/context/pages/${PAGE_ID}`, "")).toBeNull();
  });
});

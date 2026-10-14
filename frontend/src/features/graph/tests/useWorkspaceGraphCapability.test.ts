import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceGraphCapability } from "../../../shared/api/workspaceGraph";
import {
  invalidateWorkspaceGraphCapability,
  useWorkspaceGraphCapability,
} from "../useWorkspaceGraphCapability";

const { authState, getCapability, workspaceState } = vi.hoisted(() => ({
  authState: { userId: null as string | null },
  getCapability: vi.fn(),
  workspaceState: { selectedWorkspace: null as { id: string; role: string | null } | null },
}));

vi.mock("../../../shared/api/workspaceGraph", () => ({
  getWorkspaceGraphCapability: getCapability,
}));
vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => workspaceState,
}));
vi.mock("../../../shared/auth/AuthContext", () => ({
  useOptionalAuth: () => ({ user: authState.userId ? { user_id: authState.userId } : null }),
}));

function capability(
  effective: boolean,
  reason: WorkspaceGraphCapability["workspace_map"]["reason"] = effective
    ? "enabled"
    : "disabled_by_workspace",
): WorkspaceGraphCapability {
  return {
    workspace_map: {
      effective_enabled: effective,
      workspace_enabled: reason !== "disabled_by_workspace",
      service_available: reason !== "service_unavailable",
      reason,
    },
    usage_relation_signals_enabled: true,
    can_manage: false,
  };
}

describe("useWorkspaceGraphCapability", () => {
  beforeEach(() => {
    invalidateWorkspaceGraphCapability();
    getCapability.mockReset();
    authState.userId = null;
    workspaceState.selectedWorkspace = null;
    // Any fetch a test did not script still gets a promise.
    getCapability.mockResolvedValue(capability(false));
  });
  afterEach(() => {
    // Unmount before dropping the cache: a still-mounted reader would fetch
    // again after this test's scripted answers ran out.
    cleanup();
    invalidateWorkspaceGraphCapability();
  });

  it("stays fail-closed until the server says the map is on", async () => {
    let resolve: (value: WorkspaceGraphCapability) => void = () => undefined;
    getCapability.mockReturnValue(new Promise((next) => { resolve = next; }));

    const { result } = renderHook(() => useWorkspaceGraphCapability("workspace-1"));

    expect(result.current).toMatchObject({ status: "loading", enabled: false });
    await act(async () => resolve(capability(true)));
    expect(result.current).toMatchObject({ status: "ready", enabled: true });
  });

  it("shares one request among every reader of the same workspace", async () => {
    getCapability.mockResolvedValue(capability(true));

    const first = renderHook(() => useWorkspaceGraphCapability("workspace-1"));
    const second = renderHook(() => useWorkspaceGraphCapability("workspace-1"));

    await waitFor(() => expect(first.result.current.enabled).toBe(true));
    expect(second.result.current.enabled).toBe(true);
    expect(getCapability).toHaveBeenCalledTimes(1);
  });

  it("asks again after invalidation, so a Settings save is followed", async () => {
    getCapability
      .mockResolvedValueOnce(capability(false))
      .mockResolvedValueOnce(capability(true));

    const { result } = renderHook(() => useWorkspaceGraphCapability("workspace-1"));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.enabled).toBe(false);

    act(() => invalidateWorkspaceGraphCapability("workspace-1"));

    await waitFor(() => expect(result.current.enabled).toBe(true));
    expect(getCapability).toHaveBeenCalledTimes(2);
  });

  it("treats a failed check as off and reports the error", async () => {
    getCapability.mockRejectedValue(new Error("offline"));

    const { result } = renderHook(() => useWorkspaceGraphCapability("workspace-1"));

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.enabled).toBe(false);
  });

  it("does not ask without a workspace", () => {
    const { result } = renderHook(() => useWorkspaceGraphCapability(null));

    expect(result.current).toMatchObject({ status: "idle", enabled: false });
    expect(getCapability).not.toHaveBeenCalled();
  });

  it("hides a cached owner capability immediately when a role refresh reports viewer", async () => {
    workspaceState.selectedWorkspace = { id: "workspace-1", role: "owner" };
    getCapability.mockResolvedValueOnce({ ...capability(true), can_manage: true });
    let resolveViewer: (value: WorkspaceGraphCapability) => void = () => undefined;
    getCapability.mockImplementationOnce(() => new Promise((resolve) => { resolveViewer = resolve; }));
    const viewerPaints: Array<WorkspaceGraphCapability | null> = [];
    const { result, rerender } = renderHook(() => {
      const state = useWorkspaceGraphCapability("workspace-1");
      useLayoutEffect(() => {
        if (workspaceState.selectedWorkspace?.role === "viewer") viewerPaints.push(state.capability);
      });
      return state;
    });
    await waitFor(() => expect(result.current.capability?.can_manage).toBe(true));
    workspaceState.selectedWorkspace = { id: "workspace-1", role: "viewer" };
    rerender();
    expect(viewerPaints[0]).toBeNull();
    expect(result.current).toMatchObject({ status: "loading", capability: null, enabled: false });
    expect(getCapability).toHaveBeenCalledTimes(2);
    await act(async () => resolveViewer(capability(true)));
    expect(result.current).toMatchObject({ enabled: true, capability: { can_manage: false } });
  });

  it("rejects pending authority answers across owner-viewer-owner role changes", async () => {
    workspaceState.selectedWorkspace = { id: "workspace-1", role: "owner" };
    let resolveOld: (value: WorkspaceGraphCapability) => void = () => undefined;
    getCapability.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    getCapability.mockResolvedValue(capability(false));
    const { result, rerender } = renderHook(() => useWorkspaceGraphCapability("workspace-1"));
    workspaceState.selectedWorkspace = { id: "workspace-1", role: "viewer" };
    rerender();
    await waitFor(() => expect(result.current.status).toBe("ready"));
    workspaceState.selectedWorkspace = { id: "workspace-1", role: "owner" };
    rerender();
    await waitFor(() => expect(getCapability).toHaveBeenCalledTimes(3));
    await act(async () => resolveOld({ ...capability(true), can_manage: true }));
    expect(result.current).toMatchObject({ enabled: false, capability: { can_manage: false } });
  });

  it.each(["actor-two", null])("does not expose a previous actor's capability to %s", async (actorId) => {
    authState.userId = "actor-one";
    workspaceState.selectedWorkspace = { id: "workspace-1", role: "owner" };
    getCapability.mockResolvedValueOnce({ ...capability(true), can_manage: true });
    getCapability.mockImplementationOnce(() => new Promise(() => {}));
    const { result, rerender } = renderHook(() => useWorkspaceGraphCapability("workspace-1"));
    await waitFor(() => expect(result.current.capability?.can_manage).toBe(true));
    authState.userId = actorId;
    rerender();
    expect(result.current).toMatchObject({ status: "loading", capability: null, enabled: false });
    expect(getCapability).toHaveBeenCalledTimes(2);
  });
});

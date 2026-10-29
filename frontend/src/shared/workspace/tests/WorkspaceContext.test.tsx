import { act, render, renderHook, screen } from "@testing-library/react";
import { useEffect, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  useWorkspaceContext,
  WorkspaceProvider,
  type WorkspaceRefreshResult,
} from "../WorkspaceContext";
import {
  deleteWorkspace,
  getCurrentWorkspaceId,
  setDefaultWorkspace,
  type WorkspaceSummary,
} from "../../api/workspace";

describe("WorkspaceProvider", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("keeps selectWorkspaceId identity stable across workspaces list updates", () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );

    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });
    const initialSelect = result.current.selectWorkspaceId;
    const initialAdd = result.current.addWorkspace;

    act(() => {
      result.current.addWorkspace(workspace("workspace-1", "Alpha"));
    });

    expect(result.current.workspaces).toHaveLength(1);
    expect(result.current.selectWorkspaceId).toBe(initialSelect);

    act(() => {
      result.current.addWorkspace(workspace("workspace-2", "Beta"));
    });

    expect(result.current.workspaces).toHaveLength(2);
    expect(result.current.selectWorkspaceId).toBe(initialSelect);
    expect(result.current.addWorkspace).toBe(initialAdd);
  });

  it("resolves the workspace using the latest list inside selectWorkspaceId", () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );

    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });
    const initialSelect = result.current.selectWorkspaceId;

    act(() => {
      result.current.addWorkspace(workspace("workspace-1", "Alpha"));
      result.current.addWorkspace(workspace("workspace-2", "Beta"));
    });

    let returned: WorkspaceSummary | null = null;
    act(() => {
      returned = initialSelect("workspace-2");
    });

    const resolved = returned as WorkspaceSummary | null;
    expect(resolved).not.toBeNull();
    expect(resolved?.id).toBe("workspace-2");
    expect(result.current.selectedId).toBe("workspace-2");
  });

  it("keeps provider and persisted selection aligned after leaving a verified workspace route", async () => {
    let routeWorkspaceId: string | null = null;
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider routeWorkspaceId={routeWorkspaceId}>
        {children}
      </WorkspaceProvider>
    );
    const { result, rerender } = renderHook(() => useWorkspaceContext(), {
      wrapper,
    });

    act(() => {
      result.current.addWorkspace(workspace("workspace-a", "Alpha"));
      result.current.addWorkspace(workspace("workspace-b", "Beta"));
      result.current.selectWorkspaceId("workspace-a");
    });
    expect(result.current.selectedId).toBe("workspace-a");

    routeWorkspaceId = "workspace-b";
    rerender();
    expect(result.current.selectedId).toBe("workspace-b");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-b",
    );

    routeWorkspaceId = null;
    rerender();
    expect(result.current.selectedId).toBe("workspace-b");
    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-b");
  });

  // RequireEditor gates on `error && !selectedWorkspace` (App.tsx); these tests
  // pin the provider contract that gate consumes against the real listWorkspaces
  // pipeline, with only the network boundary stubbed.
  it("keeps the cached workspaces and selection when a later refresh fails", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse(workspaceListBody("workspace-1", "Alpha")),
      )
      .mockResolvedValueOnce(
        jsonResponse(errorBody("workspace list unavailable"), 500),
      );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );

    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    await act(async () => {
      await result.current.refreshWorkspaces();
    });

    expect(result.current.initialized).toBe(true);
    expect(result.current.error).toBeNull();
    expect(result.current.selectedWorkspace?.id).toBe("workspace-1");

    await act(async () => {
      await result.current.refreshWorkspaces();
    });

    expect(result.current.error).toBe(
      "The server could not complete the request. Please try again.",
    );
    expect(result.current.initialized).toBe(true);
    expect(result.current.loading).toBe(false);
    expect(result.current.workspaces).toHaveLength(1);
    expect(result.current.selectedWorkspace?.id).toBe("workspace-1");
    expect(result.current.selectedId).toBe("workspace-1");
  });

  it("does not expose an arbitrary workspace transport error", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new Error("raw req-secret"),
    );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );

    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    await act(async () => {
      await result.current.refreshWorkspaces();
    });

    expect(result.current.error).toBe("Failed to load workspaces");
    expect(result.current.error).not.toContain("raw req-secret");
    expect(result.current.initialized).toBe(true);
    expect(result.current.loading).toBe(false);
  });

  it("marks a failed first load initialized with error and no workspace, then recovers on retry", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse(errorBody("workspace list unavailable"), 500),
      )
      .mockResolvedValueOnce(
        jsonResponse(workspaceListBody("workspace-1", "Alpha")),
      );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );

    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    await act(async () => {
      await result.current.refreshWorkspaces();
    });

    expect(result.current.initialized).toBe(true);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBe(
      "The server could not complete the request. Please try again.",
    );
    expect(result.current.workspaces).toHaveLength(0);
    expect(result.current.selectedWorkspace).toBeNull();

    await act(async () => {
      await result.current.refreshWorkspaces();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.initialized).toBe(true);
    expect(result.current.workspaces).toHaveLength(1);
    expect(result.current.selectedWorkspace?.id).toBe("workspace-1");
  });

  it("keeps multiple unanchored memberships unresolved until the user chooses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        data: {
          current_workspace_id: null,
          default_workspace_id: null,
          workspaces: [
            workspace("workspace-1", "Alpha"),
            workspace("workspace-2", "Beta"),
          ],
        },
      }),
    );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );
    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    await act(async () => {
      await result.current.refreshWorkspaces();
    });

    expect(result.current.initialized).toBe(true);
    expect(result.current.workspaces).toHaveLength(2);
    expect(result.current.selectedId).toBe("");
    expect(result.current.selectedWorkspace).toBeNull();
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBeNull();
  });

  it("honors a server-provided fallback during an authoritative refresh", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        data: {
          current_workspace_id: "removed-workspace",
          default_workspace_id: "fallback-workspace",
          workspaces: [workspace("fallback-workspace", "Fallback")],
        },
      }),
    );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );
    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    let selected: WorkspaceSummary | null = null;
    await act(async () => {
      const refreshed =
        await result.current.refreshWorkspaces("fallback-workspace");
      selected = refreshed?.selectedWorkspace ?? null;
    });

    expect((selected as WorkspaceSummary | null)?.id).toBe("fallback-workspace");
    expect(result.current.selectedId).toBe("fallback-workspace");
    expect(result.current.selectedWorkspace?.id).toBe("fallback-workspace");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "fallback-workspace",
    );
  });

  it("does not select the first remaining membership after an ambiguous removal", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        data: {
          current_workspace_id: null,
          default_workspace_id: null,
          workspaces: [
            workspace("workspace-1", "Alpha"),
            workspace("workspace-2", "Beta"),
            workspace("workspace-3", "Gamma"),
          ],
        },
      }),
    );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );
    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    await act(async () => {
      await result.current.refreshWorkspaces();
    });
    act(() => {
      result.current.selectWorkspaceId("workspace-2");
    });

    let fallback: WorkspaceSummary | null = null;
    act(() => {
      fallback = result.current.removeWorkspace("workspace-2");
    });

    expect(fallback).toBeNull();
    expect(result.current.workspaces).toHaveLength(2);
    expect(result.current.selectedWorkspace).toBeNull();
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBeNull();
  });

  it("persists an authoritative fallback that was absent from stale local state", () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );
    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    act(() => {
      result.current.addWorkspace(workspace("workspace-a", "Alpha"));
      result.current.removeWorkspace("workspace-a", {
        fallbackWorkspaceId: "workspace-b",
      });
    });

    expect(result.current.workspaces).toHaveLength(0);
    expect(result.current.selectedId).toBe("workspace-b");
    expect(result.current.selectedWorkspace).toBeNull();
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-b",
    );
  });

  it("clears stale local memberships when the authoritative fallback is null", () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );
    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    act(() => {
      result.current.addWorkspace(workspace("workspace-a", "Alpha"));
      result.current.addWorkspace(workspace("workspace-stale", "Stale"));
    });
    act(() => {
      result.current.removeWorkspace("workspace-a", {
        fallbackWorkspaceId: null,
      });
    });

    expect(result.current.workspaces).toHaveLength(0);
    expect(result.current.selectedId).toBe("");
    expect(result.current.selectedWorkspace).toBeNull();
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBeNull();
  });

  it("keeps the pinned default when an unrelated workspace is removed", async () => {
    // Nothing automatically refreshes the list after a delete (Shell only
    // fetches on mount), so removeWorkspace must not wipe a still-valid
    // default_workspace_id — that would drop the "Default" badge until a
    // manual reload.
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          default_workspace_id: "workspace-1",
          workspaces: [
            workspace("workspace-1", "Alpha"),
            workspace("workspace-2", "Beta"),
          ],
        },
      }),
    );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );

    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    await act(async () => {
      await result.current.refreshWorkspaces();
    });
    expect(result.current.defaultWorkspaceId).toBe("workspace-1");

    act(() => {
      result.current.removeWorkspace("workspace-2");
    });

    expect(result.current.workspaces).toHaveLength(1);
    expect(result.current.defaultWorkspaceId).toBe("workspace-1");
    expect(result.current.selectedWorkspace?.id).toBe("workspace-1");
  });

  it("clears the pinned default when the default workspace itself is removed", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          default_workspace_id: "workspace-2",
          workspaces: [
            workspace("workspace-1", "Alpha"),
            workspace("workspace-2", "Beta"),
          ],
        },
      }),
    );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );

    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    await act(async () => {
      await result.current.refreshWorkspaces();
    });
    expect(result.current.defaultWorkspaceId).toBe("workspace-2");

    act(() => {
      result.current.removeWorkspace("workspace-2");
    });

    expect(result.current.workspaces).toHaveLength(1);
    expect(result.current.defaultWorkspaceId).toBeNull();
    expect(result.current.selectedWorkspace?.id).toBe("workspace-1");
  });

  it("resets on logout and account switch but not on the initial session resolution (WS-05)", async () => {
    // `renderHook`'s `wrapper` option only ever receives `children` — the
    // hook's own per-render props are NOT forwarded to it — so a real
    // `sessionUserId` prop change on WorkspaceProvider has to go through a
    // plain component render/rerender instead of renderHook's wrapper.
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse(workspaceListBody("workspace-1", "Alpha")),
    );
    const { rerender } = render(
      <WorkspaceProvider sessionUserId={null}>
        <WorkspaceProbe />
      </WorkspaceProvider>,
    );

    await act(async () => {
      await lastProbe?.refreshWorkspaces();
    });
    expect(screen.getByTestId("workspace-count")).toHaveTextContent("1");

    // null -> "user-a" is the initial session check resolving; Shell's
    // mount-only refreshWorkspaces already populated the list by the time
    // that resolves in the real app, so this transition must not wipe it.
    rerender(
      <WorkspaceProvider sessionUserId="user-a">
        <WorkspaceProbe />
      </WorkspaceProvider>,
    );
    expect(screen.getByTestId("workspace-count")).toHaveTextContent("1");
    expect(screen.getByTestId("initialized")).toHaveTextContent("true");

    // "user-a" -> "user-b" is an account switch in the same browser tab and
    // must clear the previous user's workspace list immediately.
    rerender(
      <WorkspaceProvider sessionUserId="user-b">
        <WorkspaceProbe />
      </WorkspaceProvider>,
    );
    expect(screen.getByTestId("workspace-count")).toHaveTextContent("0");
    expect(screen.getByTestId("selected-id")).toHaveTextContent("none");
    expect(screen.getByTestId("initialized")).toHaveTextContent("false");

    // Refill for user-b, then log out ("user-b" -> null) and confirm that
    // clears too.
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse(workspaceListBody("workspace-2", "Beta")),
    );
    await act(async () => {
      await lastProbe?.refreshWorkspaces();
    });
    expect(screen.getByTestId("workspace-count")).toHaveTextContent("1");
    rerender(
      <WorkspaceProvider sessionUserId={null}>
        <WorkspaceProbe />
      </WorkspaceProvider>,
    );
    expect(screen.getByTestId("workspace-count")).toHaveTextContent("0");
    expect(screen.getByTestId("initialized")).toHaveTextContent("false");
  });

  it("ignores a prior account's workspace response after an account switch", async () => {
    const first = deferredResponse();
    const second = deferredResponse();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const { rerender } = render(
      <WorkspaceProvider sessionUserId="user-a">
        <WorkspaceProbe />
      </WorkspaceProvider>,
    );

    let firstRefresh!: Promise<WorkspaceRefreshResult | null>;
    act(() => {
      firstRefresh = lastProbe!.refreshWorkspaces();
    });

    rerender(
      <WorkspaceProvider sessionUserId="user-b">
        <WorkspaceProbe />
      </WorkspaceProvider>,
    );
    expect(screen.getByTestId("workspace-count")).toHaveTextContent("0");
    expect(screen.getByTestId("initialized")).toHaveTextContent("false");
    expect(lastProbe?.loading).toBe(false);

    let secondRefresh!: Promise<WorkspaceRefreshResult | null>;
    act(() => {
      secondRefresh = lastProbe!.refreshWorkspaces();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    second.resolve(jsonResponse(workspaceListBody("workspace-b", "Beta")));
    await act(async () => {
      await secondRefresh;
    });
    expect(screen.getByTestId("selected-id")).toHaveTextContent("workspace-b");
    expect(screen.getByTestId("workspace-count")).toHaveTextContent("1");
    expect(screen.getByTestId("initialized")).toHaveTextContent("true");

    first.resolve(jsonResponse(workspaceListBody("workspace-a", "Alpha")));
    await act(async () => {
      await firstRefresh;
    });
    expect(screen.getByTestId("selected-id")).toHaveTextContent("workspace-b");
    expect(screen.getByTestId("workspace-count")).toHaveTextContent("1");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-b",
    );
  });

  it("does not restore a removed workspace from a late pre-delete refresh", async () => {
    const preDeleteList = deferredResponse();
    const postDeleteList = deferredResponse();
    let workspaceListCalls = 0;
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input, init) => {
        const url = input.toString();
        if (
          url === "http://127.0.0.1:21080/api/v1/workspaces" &&
          (init?.method ?? "GET") === "GET"
        ) {
          workspaceListCalls += 1;
          return workspaceListCalls === 1
            ? preDeleteList.promise
            : postDeleteList.promise;
        }
        if (
          url === "http://127.0.0.1:21080/api/v1/workspaces/workspace-a" &&
          init?.method === "DELETE"
        ) {
          return Promise.resolve(
            jsonResponse({ data: { deleted_workspace_id: "workspace-a" } }),
          );
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );
    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    act(() => {
      result.current.addWorkspace(workspace("workspace-a", "Alpha"));
    });
    let staleRefresh!: Promise<WorkspaceRefreshResult | null>;
    act(() => {
      staleRefresh = result.current.refreshWorkspaces();
    });

    await act(async () => {
      await deleteWorkspace("workspace-a");
      result.current.removeWorkspace("workspace-a");
    });
    let authoritativeRefresh!: Promise<WorkspaceRefreshResult | null>;
    act(() => {
      authoritativeRefresh = result.current.refreshWorkspaces();
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);

    postDeleteList.resolve(
      jsonResponse({
        data: {
          current_workspace_id: null,
          default_workspace_id: null,
          workspaces: [],
        },
      }),
    );
    await act(async () => {
      await authoritativeRefresh;
    });
    expect(result.current.workspaces).toHaveLength(0);
    expect(result.current.selectedWorkspace).toBeNull();

    preDeleteList.resolve(
      jsonResponse(workspaceListBody("workspace-a", "Alpha")),
    );
    await act(async () => {
      await staleRefresh;
    });

    expect(result.current.workspaces).toHaveLength(0);
    expect(result.current.selectedId).toBe("");
    expect(result.current.selectedWorkspace).toBeNull();
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBeNull();
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug"),
    ).toBeNull();
  });

  it("keeps the newest refresh when a pre-mutation response arrives last", async () => {
    const staleList = deferredResponse();
    const authoritativeList = deferredResponse();
    let workspaceListCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = input.toString();
      if (
        url === "http://127.0.0.1:21080/api/v1/workspaces" &&
        (init?.method ?? "GET") === "GET"
      ) {
        workspaceListCalls += 1;
        return workspaceListCalls === 1
          ? staleList.promise
          : authoritativeList.promise;
      }
      if (
        url === "http://127.0.0.1:21080/api/v1/users/me/default-workspace" &&
        init?.method === "PATCH"
      ) {
        return Promise.resolve(
          jsonResponse({ data: { default_workspace_id: "workspace-b" } }),
        );
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <WorkspaceProvider>{children}</WorkspaceProvider>
    );
    const { result } = renderHook(() => useWorkspaceContext(), { wrapper });

    let firstRefresh!: Promise<WorkspaceRefreshResult | null>;
    act(() => {
      firstRefresh = result.current.refreshWorkspaces();
    });
    await act(async () => {
      await setDefaultWorkspace("workspace-b");
    });
    let secondRefresh!: Promise<WorkspaceRefreshResult | null>;
    act(() => {
      secondRefresh = result.current.refreshWorkspaces();
    });

    authoritativeList.resolve(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-b",
          default_workspace_id: "workspace-b",
          workspaces: [
            workspace("workspace-a", "Alpha"),
            workspace("workspace-b", "Beta"),
          ],
        },
      }),
    );
    await act(async () => {
      await secondRefresh;
    });
    expect(result.current.defaultWorkspaceId).toBe("workspace-b");
    expect(result.current.selectedWorkspace?.id).toBe("workspace-b");

    staleList.resolve(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-a",
          default_workspace_id: "workspace-a",
          workspaces: [
            workspace("workspace-a", "Alpha"),
            workspace("workspace-b", "Beta"),
          ],
        },
      }),
    );
    await act(async () => {
      await firstRefresh;
    });

    expect(result.current.defaultWorkspaceId).toBe("workspace-b");
    expect(result.current.selectedWorkspace?.id).toBe("workspace-b");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-b",
    );
  });
});

// Set by the most recently rendered WorkspaceProbe so the test can drive
// refreshWorkspaces() from outside the component tree between rerenders.
let lastProbe: ReturnType<typeof useWorkspaceContext> | null = null;

function WorkspaceProbe() {
  const context = useWorkspaceContext();
  // Not read during rendering (only by the test driver, between act()
  // blocks), so this belongs in an effect rather than the render body --
  // reassigning an outer-scope variable while rendering is a side effect
  // React (and this project's lint rule) treats as impure, even though it's
  // harmless here since render() flushes effects before returning control.
  useEffect(() => {
    lastProbe = context;
  });
  return (
    <div>
      <span data-testid="workspace-count">{context.workspaces.length}</span>
      <span data-testid="selected-id">{context.selectedId || "none"}</span>
      <span data-testid="initialized">
        {context.initialized ? "true" : "false"}
      </span>
    </div>
  );
}

function workspace(id: string, title: string): WorkspaceSummary {
  return {
    id,
    slug: id,
    title,
    name: title,
    goal: "",
    default_language: "en",
    visibility: "private",
  };
}

function workspaceListBody(id: string, title: string) {
  return {
    data: {
      current_workspace_id: id,
      default_workspace_id: null,
      workspaces: [workspace(id, title)],
    },
  };
}

function errorBody(message: string) {
  return {
    error_code: "internal_error",
    message,
    request_id: "req-1",
    warnings: [],
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

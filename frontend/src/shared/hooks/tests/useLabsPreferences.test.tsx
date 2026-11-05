import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { invalidateWorkspaceLabsPreferences, useLabsPreferences } from "../useLabsPreferences";

const state = vi.hoisted(() => ({
  auth: { status: "authenticated", user: { user_id: "owner" } },
  workspaceId: "workspace-one",
  workspaces: [{ id: "workspace-one", role: "owner" }, { id: "workspace-two", role: "owner" }],
  load: vi.fn(),
}));
vi.mock("../../auth/AuthContext", () => ({ useOptionalAuth: () => state.auth }));
vi.mock("../../lib/workspaceRoutes", () => ({ useWorkspaceRoute: () => ({ workspaceId: state.workspaceId }) }));
vi.mock("../../workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => ({ selectedId: "workspace-one", workspaces: state.workspaces }),
}));
vi.mock("../../api/operations", () => ({ getSettingsPolicies: state.load }));

function Consumer({ name = "labs" }: { name?: string }) {
  const { preferences, ready } = useLabsPreferences();
  return <output data-testid={name}>{JSON.stringify({ ...preferences, ready })}</output>;
}
function policy(ask = false, agent = false, knowledgeMap = false) {
  return { labs: { policy: { ask_enabled: ask, agent_enabled: agent, workspace_map_enabled: knowledgeMap } } };
}
function expectPreferences(name: string, ask: boolean, agent: boolean, knowledgeMap = false, ready = true) {
  expect(screen.getByTestId(name).textContent).toBe(JSON.stringify({ ask, agent, knowledgeMap, ready }));
}
beforeEach(() => {
  invalidateWorkspaceLabsPreferences();
  state.auth = { status: "authenticated", user: { user_id: "owner" } };
  state.workspaceId = "workspace-one";
  state.workspaces = [{ id: "workspace-one", role: "owner" }, { id: "workspace-two", role: "owner" }];
  state.load.mockReset().mockResolvedValue(policy());
  window.localStorage.clear();
});
afterEach(() => { cleanup(); });

describe("useLabsPreferences", () => {
  it("shares server state between readers and revalidates after saving", async () => {
    state.load.mockResolvedValueOnce(policy(true, false, true));
    render(<><Consumer name="navigation" /><Consumer name="route" /></>);
    await waitFor(() => expectPreferences("navigation", true, false, true));
    expectPreferences("route", true, false, true);
    expect(state.load).toHaveBeenCalledTimes(1);
    state.load.mockResolvedValueOnce(policy(false, true));
    act(() => invalidateWorkspaceLabsPreferences("workspace-one"));
    await waitFor(() => expectPreferences("navigation", false, true));
    expectPreferences("route", false, true);
    expect(state.load).toHaveBeenCalledTimes(2);
    state.load.mockResolvedValueOnce(policy(true, false, true));
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expectPreferences("navigation", true, false, true));
    expectPreferences("route", true, false, true);
    expect(state.load).toHaveBeenCalledTimes(3);
  });

  it("ignores retired browser preferences and loads saved server settings after remount", async () => {
    window.localStorage.setItem("dtxwiki:labs:owner", JSON.stringify({ ask: true, agent: true, knowledgeMap: true }));
    state.load.mockResolvedValue(policy(false, true));
    const first = render(<Consumer />);
    await waitFor(() => expectPreferences("labs", false, true));
    first.unmount();
    render(<Consumer />);
    expectPreferences("labs", false, true);
    expect(state.load).toHaveBeenCalledTimes(1);
  });

  it("fences workspace changes and invalidated in-flight responses", async () => {
    let resolveOld: (value: ReturnType<typeof policy>) => void = () => {};
    state.load.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    state.load.mockResolvedValueOnce(policy(false, true));
    const view = render(<Consumer />);
    expectPreferences("labs", false, false, false, false);
    state.workspaceId = "workspace-two";
    view.rerender(<Consumer />);
    await waitFor(() => expectPreferences("labs", false, true));
    act(() => invalidateWorkspaceLabsPreferences("workspace-one"));
    await act(async () => resolveOld(policy(true, false, true)));
    expectPreferences("labs", false, true);
    expect(state.load).toHaveBeenNthCalledWith(2, "workspace-two");
  });

  it("hides privileges immediately on account change and membership revocation", async () => {
    state.load.mockResolvedValueOnce(policy(true, true, true));
    const view = render(<Consumer />);
    await waitFor(() => expectPreferences("labs", true, true, true));
    state.auth = { status: "authenticated", user: { user_id: "other-owner" } };
    view.rerender(<Consumer />);
    expectPreferences("labs", false, false, false, false);
    await waitFor(() => expectPreferences("labs", false, false));
    state.load.mockResolvedValueOnce(policy(true, true, true));
    act(() => invalidateWorkspaceLabsPreferences("workspace-one"));
    await waitFor(() => expectPreferences("labs", true, true, true));
    state.workspaces = [{ id: "workspace-one", role: "editor" }];
    view.rerender(<Consumer />);
    expectPreferences("labs", false, false, false, false);
    expect(state.load).toHaveBeenCalledTimes(3);
    state.auth = { status: "anonymous", user: { user_id: "other-owner" } };
    view.rerender(<Consumer />);
    state.workspaces = [{ id: "workspace-one", role: "owner" }];
    state.auth = { status: "authenticated", user: { user_id: "other-owner" } };
    state.load.mockResolvedValueOnce(policy(false, true));
    view.rerender(<Consumer />);
    await waitFor(() => expectPreferences("labs", false, true));
    expect(state.load).toHaveBeenCalledTimes(4);
  });

  it("does not request Labs settings for members or unauthenticated accounts", async () => {
    state.workspaces = [{ id: "workspace-one", role: "viewer" }];
    const view = render(<Consumer />);
    expectPreferences("labs", false, false, false, false);
    state.workspaces = [{ id: "workspace-one", role: "owner" }];
    state.auth = { status: "anonymous", user: { user_id: "owner" } };
    view.rerender(<Consumer />);
    expectPreferences("labs", false, false, false, false);
    expect(state.load).not.toHaveBeenCalled();
  });

  it("fails closed on a failed load and permits an explicit retry", async () => {
    state.load.mockRejectedValueOnce(new Error("offline"));
    const view = render(<Consumer />);
    await waitFor(() => expect(state.load).toHaveBeenCalledOnce());
    view.rerender(<Consumer />);
    expectPreferences("labs", false, false, false, false);
    state.load.mockResolvedValueOnce(policy(true));
    act(() => invalidateWorkspaceLabsPreferences("workspace-one"));
    await waitFor(() => expectPreferences("labs", true, false));
  });
});

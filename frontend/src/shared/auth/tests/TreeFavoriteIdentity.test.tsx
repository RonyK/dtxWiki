import { MemoryRouter } from "react-router-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceScope } from "../../../app/App";
import {
  beginTreeFavoriteRead,
  clearTreeFavoriteCache,
  getTreeFavoriteSnapshot,
  reconcileTreeFavorites,
  setTreeFavoriteActor,
  toggleTreeFavorite,
} from "../../api/treeFavorites";
import { listTreeData, type TreeNodePayload } from "../../api/workspace";
import { TreeRail } from "../../components/TreeRail";
import { AuthProvider, useAuth } from "../AuthContext";

const workspaceId = "shared-workspace";
const page: TreeNodePayload = {
  id: "node-1", section: "context", title: "Shared page", slug: "shared-page",
  node_kind: "page_ref", parent_id: null, sort_key: "0001",
  target_kind: "context_page", target_id: "page-1", is_system_node: false,
  can_have_children: false, primary_placement: true, visibility_status: "visible",
  children_count: 0, breadcrumb: [],
};

function user(actorId: string) {
  return { user_id: actorId, email: `${actorId}@example.com`, display_name: actorId, status: "active" };
}

function json(data: unknown) {
  return new Response(JSON.stringify({ data }), { status: 200 });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

function AuthControls() {
  const { user: actor, refreshSession, setAuthenticatedSession } = useAuth();
  return <>
    <output data-testid="actor">{actor?.user_id ?? "none"}</output>
    {["A", "B"].map((actorId) => <button key={actorId} type="button" onClick={() => setAuthenticatedSession({
      user: user(actorId), expires_at: "2026-12-01T00:00:00Z",
    })}>Authenticate {actorId}</button>)}
    <button type="button" onClick={() => void refreshSession()}>Refresh session</button>
  </>;
}

function LoadedRail() {
  const { user: actor } = useAuth();
  const [nodes, setNodes] = useState<TreeNodePayload[]>([]);
  // Mirrors route loaders whose query remains the same shared workspace. The
  // identity boundary must remount them even when that query does not change.
  useEffect(() => {
    let cancelled = false;
    void listTreeData(workspaceId, "context").then((data) => { if (!cancelled) setNodes(data.nodes); });
    return () => { cancelled = true; };
  }, []);
  return <>
    <input aria-label="Route state" defaultValue="" />
    <TreeRail title="Context" nodes={nodes} favoriteScope={actor ? { actorId: actor.user_id, workspaceId } : undefined} />
  </>;
}

function IdentityHarness() {
  const { user: actor } = useAuth();
  return <>
    <AuthControls />
    {actor ? <WorkspaceScope><LoadedRail /></WorkspaceScope> : null}
  </>;
}

describe("TreeRail favorite identity boundaries", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    setTreeFavoriteActor(null);
    clearTreeFavoriteCache();
  });

  it("fences delayed A reads/writes across refreshSession A → B → A and remounts raw tree props", async () => {
    const oldRead = deferred<Response>();
    const oldMutation = deferred<Response>();
    const bTree = deferred<Response>();
    let serverActor = "A";
    let delayNextRead = false;
    let treeRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = new URL(String(input)).pathname;
      if (path === "/api/v1/auth/me") return Promise.resolve(json(user(serverActor)));
      if (init?.method === "PUT") return oldMutation.promise;
      if (path.endsWith("/tree")) {
        treeRequests += 1;
        if (delayNextRead) { delayNextRead = false; return oldRead.promise; }
        if (serverActor === "B") return bTree.promise;
        return Promise.resolve(json({ nodes: [{ ...page, is_favorite: true }], policies: {} }));
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<MemoryRouter initialEntries={[`/workspaces/${workspaceId}/context`]}><AuthProvider skipInitialSessionCheck><IdentityHarness /></AuthProvider></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Authenticate A" }));
    const aRow = await screen.findByRole("treeitem", { name: page.title });
    expect(aRow.querySelector(".tree-row-favorite")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Route state" }), { target: { value: "A's local state" } });
    // A normal refresh of the same actor preserves the mounted route.
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Refresh session" })); });
    expect(screen.getByTestId("actor")).toHaveTextContent("A");
    expect(screen.getByRole("textbox", { name: "Route state" })).toHaveValue("A's local state");
    expect(treeRequests).toBe(1);
    delayNextRead = true;
    const staleRead = listTreeData(workspaceId, "context");
    fireEvent.click(within(aRow).getByRole("button", { name: `Open actions for ${page.title}` }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Remove from my favorites" }));
    expect(aRow.querySelector(".tree-row-favorite")).not.toBeInTheDocument();
    serverActor = "B";
    fireEvent.click(screen.getByRole("button", { name: "Refresh session" }));
    await waitFor(() => expect(screen.getByTestId("actor")).toHaveTextContent("B"));
    // B has not received its tree yet. A's node props and local state have
    // already gone, even though both actors use the very same workspace.
    expect(screen.queryByRole("treeitem")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Route state" })).toHaveValue("");
    expect(getTreeFavoriteSnapshot().size).toBe(0);
    await act(async () => { bTree.resolve(json({ nodes: [{ ...page, is_favorite: false }], policies: {} })); });
    expect(screen.getByRole("treeitem").querySelector(".tree-row-favorite")).not.toBeInTheDocument();
    serverActor = "A";
    fireEvent.click(screen.getByRole("button", { name: "Authenticate A" }));
    await waitFor(() => expect(screen.getByRole("treeitem").querySelector(".tree-row-favorite")).toBeInTheDocument());
    // Even returning to the same actor cannot revive requests from its old
    // session generation. The old removal must not clear the new A favorite.
    await act(async () => {
      oldMutation.resolve(json({ target_kind: "context_page", target_id: page.target_id, is_favorite: false }));
      oldRead.resolve(json({ nodes: [{ ...page, is_favorite: false }], policies: {} }));
      await staleRead;
    });
    expect(getTreeFavoriteSnapshot().size).toBe(0);
    expect(screen.getByRole("treeitem").querySelector(".tree-row-favorite")).toBeInTheDocument();
    expect(treeRequests).toBe(4);
  });

  it("binds every read and mutation to its explicit current actor", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ target_kind: "context_page", target_id: page.target_id, is_favorite: true }));
    setTreeFavoriteActor("A");
    await toggleTreeFavorite({ actorId: "A", workspaceId }, { ...page, is_favorite: false });
    const read = beginTreeFavoriteRead();
    expect(read.actorId).toBe("A");
    reconcileTreeFavorites(workspaceId, [{ ...page, is_favorite: false }], { ...read, actorId: "B" });
    expect([...getTreeFavoriteSnapshot().values()][0].isFavorite).toBe(true);
    const fetchCount = vi.mocked(globalThis.fetch).mock.calls.length;
    await toggleTreeFavorite({ actorId: "B", workspaceId }, page);
    expect(vi.mocked(globalThis.fetch).mock.calls).toHaveLength(fetchCount);
    setTreeFavoriteActor("B");
    expect(beginTreeFavoriteRead().actorId).toBe("B");
    expect(getTreeFavoriteSnapshot().size).toBe(0);
  });
});

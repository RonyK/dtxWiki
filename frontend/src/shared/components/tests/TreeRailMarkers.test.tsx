import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearTreeFavoriteCache, getTreeFavoriteSnapshot, setTreeFavoriteActor } from "../../api/treeFavorites";
import { listTreeData, type TreeNodePayload } from "../../api/workspace";
import { i18n } from "../../i18n";
import { TreeRail } from "../TreeRail";
import { TREE_RECENT_CHANGE_MS } from "../treeNodeStatus";

const NOW = Date.parse("2026-10-20T12:00:00Z");
const scope = { actorId: "viewer-1", workspaceId: "workspace-1" };
const isoAgo = (ms: number) => new Date(NOW - ms).toISOString();

function node(overrides: Partial<TreeNodePayload> = {}): TreeNodePayload {
  return {
    id: "node-1", section: "context", title: "A page", slug: "a-page",
    node_kind: "page_ref", parent_id: null, sort_key: "0001",
    target_kind: "context_page", target_id: "page-1", is_system_node: false,
    can_have_children: false, primary_placement: true, visibility_status: "visible",
    children_count: 0, breadcrumb: [], ...overrides,
  };
}

describe("TreeRail document status and personal favorites", () => {
  beforeEach(() => { clearTreeFavoriteCache(); setTreeFavoriteActor(scope.actorId); });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it.each(["context", "library", "agent"] as const)("renders all six status/favorite combinations in %s without an extra control", (section) => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const kind = section === "context" ? "context_page" : section === "library" ? "library_item" : "agent_page";
    const nodes = ["normal", "recent", "review"].flatMap((state) => [false, true].map((favorite) => node({
      id: `${state}-${favorite}`, target_id: `${state}-${favorite}`, title: `${state} ${favorite}`,
      section, target_kind: kind, is_favorite: favorite,
      content_changed_at: isoAgo(state === "normal" ? TREE_RECENT_CHANGE_MS : 1_000),
      review_status: state === "review" ? "pending" : null,
    })));
    render(<TreeRail title={section} accent={section} nodes={nodes} />);
    for (const row of screen.getAllByRole("treeitem")) {
      const expected = row.dataset.nodeId?.split("-")[0];
      expect(row).toHaveAttribute("data-document-state", expected);
      expect(row.querySelector(".tree-row-state-dot")).toHaveAttribute("data-target-group", section);
      expect(row.querySelector(".tree-row-favorite") !== null).toBe(row.dataset.nodeId?.endsWith("true"));
      expect(row.querySelectorAll("button")).toHaveLength(0);
      expect(row.querySelector(".tree-row-document-icon")).toHaveAttribute("aria-hidden", "true");
      if (expected === "review") {
        expect(row.querySelector(".tree-row-review-clock")).toHaveAttribute("width", "7");
        expect(row.querySelector(".tree-row-review-clock")).toHaveAttribute("height", "7");
        expect(row.querySelector(".tree-row-review-clock circle")).toHaveAttribute("fill", "currentColor");
        expect(row.querySelector(".tree-row-review-clock path")).toHaveAttribute("stroke", "white");
        expect(row.querySelector(".tree-row-review-clock path")).toHaveAttribute("stroke-width", "2.2");
      }
    }
  });

  it("expires at exactly 72h while open, keeps reads unchanged, and recomputes after review resolution", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const onSelect = vi.fn();
    const page = node({ content_changed_at: isoAgo(TREE_RECENT_CHANGE_MS - 1) });
    const { rerender } = render(<TreeRail title="Context" nodes={[page]} onSelectTarget={onSelect} />);
    const row = screen.getByRole("treeitem", { name: "A page" });
    expect(row).toHaveAttribute("data-document-state", "recent");
    fireEvent.click(row);
    expect(onSelect).toHaveBeenCalledWith(page);
    expect(row).toHaveAttribute("data-document-state", "recent");
    act(() => { vi.advanceTimersByTime(1); });
    expect(row).toHaveAttribute("data-document-state", "normal");
    rerender(<TreeRail title="Context" nodes={[{ ...page, review_status: "pending", review_requested_at: isoAgo(10 * TREE_RECENT_CHANGE_MS) }]} />);
    expect(row).toHaveAttribute("data-document-state", "review");
    act(() => { vi.setSystemTime(NOW + 10 * TREE_RECENT_CHANGE_MS); vi.advanceTimersByTime(30_000); });
    expect(row).toHaveAttribute("data-document-state", "review");
    rerender(<TreeRail title="Context" nodes={[page]} />);
    expect(row).toHaveAttribute("data-document-state", "normal");
    const updated = { ...page, content_changed_at: new Date(Date.now()).toISOString() };
    rerender(<TreeRail title="Context" nodes={[{ ...updated, review_status: "pending" }]} />);
    expect(row).toHaveAttribute("data-document-state", "review");
    rerender(<TreeRail title="Context" nodes={[updated]} />);
    expect(row).toHaveAttribute("data-document-state", "recent");
  });

  it("uses review-request time separately from content time in row, tooltip, and mobile menu descriptions", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const page = node({ title: "A long full title", is_favorite: true,
      content_changed_at: isoAgo(12 * 86_400_000), review_status: "changes_requested", review_requested_at: isoAgo(2 * 86_400_000) });
    const { rerender } = render(<TreeRail title="Context" nodes={[page]} favoriteScope={scope} />);
    const row = screen.getByRole("treeitem", { name: page.title });
    expect(row).toHaveAccessibleDescription("Changes requested · Review requested 2 days ago · My favorite");
    fireEvent.mouseEnter(row);
    expect(screen.getByRole("tooltip")).toHaveTextContent(page.title);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Review requested 2 days ago");
    fireEvent.mouseLeave(row);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.focus(row);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    fireEvent.keyDown(row, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.click(within(row).getByRole("button", { name: "Open actions for A long full title" }));
    expect(screen.getByRole("menu").querySelector(".tree-node-menu-summary")).toHaveTextContent("Changes requested · Review requested 2 days ago · My favorite");
    expect(screen.getByRole("menuitem", { name: "Remove from my favorites" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    rerender(<TreeRail title="Context" nodes={[{ ...page, review_status: null }]} favoriteScope={scope} />);
    expect(row).toHaveAccessibleDescription("Normal · Last content change 12 days ago · My favorite");
  });

  it("dismisses a stale hover tooltip after its hovered chevron is replaced without a mouseleave", () => {
    const folder = node({ node_kind: "folder", target_kind: null, target_id: null,
      title: "Inbox", can_have_children: true, children_count: 1 });
    const onExpandedNodeIdsChange = vi.fn();
    const { rerender } = render(<TreeRail title="Context" nodes={[folder]} expandedNodeIds={[]}
      onExpandedNodeIdsChange={onExpandedNodeIdsChange} />);
    const row = screen.getByRole("treeitem", { name: "Inbox" });
    fireEvent.mouseEnter(row);
    act(() => { within(row).getByRole("button", { name: "Expand Inbox" }).focus(); });
    fireEvent.click(within(row).getByRole("button", { name: "Expand Inbox" }));
    rerender(<TreeRail title="Context" nodes={[folder]} expandedNodeIds={[folder.id]}
      onExpandedNodeIdsChange={onExpandedNodeIdsChange} />);
    act(() => { screen.getByRole("searchbox").focus(); });
    expect(screen.getByRole("tooltip")).toHaveTextContent("Inbox");
    fireEvent.pointerMove(document.body);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("keeps a keyboard focus tooltip when the pointer leaves and dismisses it when focus leaves", () => {
    render(<TreeRail title="Context" nodes={[node()]} />);
    const row = screen.getByRole("treeitem", { name: "A page" });
    act(() => { row.focus(); });
    fireEvent.pointerMove(document.body);
    expect(screen.getByRole("tooltip")).toHaveTextContent("A page");
    act(() => { screen.getByRole("searchbox").focus(); });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("announces absent/invalid timestamps without invented elapsed times, including Korean", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const { rerender } = render(<TreeRail title="Context" nodes={[node({ content_changed_at: "invalid" })]} />);
    expect(screen.getByRole("treeitem")).toHaveAccessibleDescription("Normal · Content change time unavailable");
    await act(async () => { await i18n.changeLanguage("ko"); });
    rerender(<TreeRail title="Context" nodes={[node({ review_status: "pending", review_requested_at: null })]} />);
    expect(screen.getByRole("treeitem")).toHaveAccessibleDescription("리뷰 대기 · 리뷰 요청 시각 없음");
  });

  it("optimistically updates every placement/rail for a viewer and rolls back failures with a live announcement", async () => {
    let respond: (response: Response) => void = () => {};
    const fetchMock = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { respond = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    const first = node();
    const second = node({ id: "placement-2", title: "Second placement" });
    render(<><TreeRail title="Context" nodes={[first, second]} favoriteScope={scope} /><TreeRail title="Another rail" nodes={[node({ id: "placement-3", title: "Third placement" })]} favoriteScope={scope} /></>);
    const row = screen.getByRole("treeitem", { name: "A page" });
    fireEvent.click(within(row).getByRole("button", { name: "Open actions for A page" }));
    expect(screen.queryByRole("menuitem", { name: "Rename" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Add to my favorites" }));
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(3);
    fireEvent.click(within(row).getByRole("button", { name: "Open actions for A page" }));
    expect(screen.getByRole("menuitem", { name: "Remove from my favorites" })).toBeDisabled();
    expect(screen.getByRole("menu")).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(new URL(fetchMock.mock.calls[0][0]).pathname).toBe("/api/v1/workspaces/workspace-1/favorites/context_page/page-1");
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({ method: "PUT", credentials: "include", body: JSON.stringify({ is_favorite: true }) }));
    await act(async () => { respond(new Response(JSON.stringify({ data: { target_kind: "context_page", target_id: "page-1", is_favorite: true } }), { status: 200 })); });
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    fireEvent.click(within(row).getByRole("button", { name: "Open actions for A page" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Remove from my favorites" }));
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(0);
    await act(async () => {});
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(3);
    expect(screen.getAllByRole("status")[0]).toHaveTextContent("Could not update favorites for A page.");
  });

  it("isolates actor/workspace state, dismisses an old menu, and fences late responses after session reset", async () => {
    let respond: (response: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { respond = resolve; })));
    const props = { title: "Context", nodes: [node()], favoriteScope: scope };
    const { rerender } = render(<TreeRail {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Open actions for A page" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Add to my favorites" }));
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Open actions for A page" }));
    rerender(<TreeRail {...props} favoriteScope={{ ...scope, actorId: "viewer-2" }} />);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(0);
    rerender(<TreeRail {...props} favoriteScope={{ ...scope, workspaceId: "workspace-2" }} />);
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(0);
    act(() => { clearTreeFavoriteCache(); });
    await act(async () => { respond(new Response(JSON.stringify({ data: { target_kind: "context_page", target_id: "page-1", is_favorite: true } }), { status: 200 })); });
    expect(getTreeFavoriteSnapshot().size).toBe(0);
    rerender(<TreeRail {...props} />);
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(0);
  });

  it("accepts authoritative favorite refreshes while fencing reads started before mutation settlement", async () => {
    const responses: Array<(response: Response) => void> = [];
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { responses.push(resolve); }));
    vi.stubGlobal("fetch", fetchMock);
    render(<TreeRail title="Context" nodes={[node(), node({ id: "second-placement", title: "Second placement" })]} favoriteScope={scope} />);
    // This request begins before the personal action and returns afterward.
    const staleRead = listTreeData(scope.workspaceId, "context");
    fireEvent.click(screen.getByRole("button", { name: "Open actions for A page" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Add to my favorites" }));
    const duringMutationRead = listTreeData(scope.workspaceId, "context");
    const json = (data: unknown) => new Response(JSON.stringify({ data }), { status: 200 });
    await act(async () => { responses[1](json({ target_kind: "context_page", target_id: "page-1", is_favorite: true })); });
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(2);
    await act(async () => {
      responses[0](json({ nodes: [node({ is_favorite: false })], policies: {} }));
      responses[2](json({ nodes: [node({ is_favorite: false })], policies: {} }));
      await Promise.all([staleRead, duringMutationRead]);
    });
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(2);
    // A fresh server read (e.g. another device removed it) updates every
    // placement, including one whose original nodes prop has not reloaded.
    const authoritativeRead = listTreeData(scope.workspaceId, "context");
    await act(async () => {
      responses[3](json({ nodes: [node({ is_favorite: false })], policies: {} }));
      await authoritativeRead;
    });
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Open actions for Second placement" }));
    expect(screen.getByRole("menuitem", { name: "Add to my favorites" })).toBeInTheDocument();
  });

  it("does not offer favorites for folders/artifacts and renders persisted server favorites after remount", () => {
    const { unmount } = render(<TreeRail title="Library" favoriteScope={scope} nodes={[
      node({ id: "folder", node_kind: "folder", title: "Folder", target_kind: null, target_id: null }),
      node({ id: "artifact", title: "Artifact", target_kind: "library_artifact" }),
    ]} />);
    expect(screen.queryByRole("button", { name: /Open actions/ })).not.toBeInTheDocument();
    unmount();
    render(<TreeRail title="Library" favoriteScope={scope} nodes={[node({ section: "library", target_kind: "library_item", is_favorite: true })]} />);
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(1);
  });
});

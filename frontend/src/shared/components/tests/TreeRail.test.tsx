import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TreeNodePayload } from "../../api/workspace";
import { TreeRail } from "../TreeRail";

describe("TreeRail", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("scrolls a late selected row nearest without stealing focus", async () => {
    const scrollIntoView = vi.spyOn(HTMLElement.prototype, "scrollIntoView");
    const page = treeNode({
      id: "node-page",
      title: "Page",
      target_kind: "context_page",
      target_id: "page-id",
    });
    const { rerender } = render(
      <TreeRail title="Context" activeNodeId={null} nodes={[]} />,
    );
    const outside = document.createElement("button");
    document.body.append(outside);
    outside.focus();
    rerender(
      <TreeRail
        title="Context"
        activeNodeId="node-page"
        activeTargetId="different-page-id"
        nodes={[page]}
      />,
    );
    await waitFor(() =>
      expect(scrollIntoView).toHaveBeenCalledWith({
        block: "nearest",
        inline: "nearest",
      }),
    );
    expect(document.activeElement).toBe(outside);
    outside.remove();
    scrollIntoView.mockRestore();
  });

  it("tints the folder icon so it reads as a folder even without children (L-5)", () => {
    const folder = treeNode({ id: "node-folder", title: "Empty Folder", node_kind: "folder" });
    const page = treeNode({ id: "node-page", title: "Page", node_kind: "page_ref" });
    render(<TreeRail title="Context" nodes={[folder, page]} />);

    const folderIcon = within(
      screen.getByRole("treeitem", { name: "Empty Folder" }),
    ).getByText("", { selector: "svg.lucide-folder" });
    const pageIcon = within(
      screen.getByRole("treeitem", { name: "Page" }),
    ).getByText("", { selector: "svg" });

    expect(folderIcon).toHaveClass("tree-row-icon-folder");
    expect(pageIcon).not.toHaveClass("tree-row-icon-folder");
  });

  it("only renders an action overlay when permitted and persists it while its menu is open", async () => {
    const node = treeNode({ id: "node-page", title: "Page" });
    const { rerender } = render(<TreeRail title="Context" nodes={[node]} />);
    expect(screen.getByRole("treeitem")).not.toContainElement(
      document.querySelector(".tree-row-actions"),
    );
    rerender(
      <TreeRail
        title="Context"
        nodes={[node]}
        onCopyNodeLink={() => undefined}
      />,
    );
    const row = screen.getByRole("treeitem");
    const trigger = within(row).getByRole("button", {
      name: "Open actions for Page",
    });
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(trigger);
    expect(row).toHaveAttribute("data-actions-open", "true");
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("restores keyboard focus after copying a node link", async () => {
    const onCopyNodeLink = vi.fn();
    const node = treeNode({ id: "node-page", title: "Page" });
    render(
      <TreeRail
        title="Context"
        nodes={[node]}
        onCopyNodeLink={onCopyNodeLink}
      />,
    );

    const row = screen.getByRole("treeitem", { name: "Page" });
    row.focus();
    await userEvent.keyboard("{Shift>}{F10}{/Shift}");
    await userEvent.click(screen.getByRole("menuitem", { name: "Copy link" }));

    expect(onCopyNodeLink).toHaveBeenCalledWith(node);
    await waitFor(() => expect(row).toHaveFocus());
  });

  it("filters locally while preserving the active path", async () => {
    const activeRoot = treeNode({
      id: "active-root",
      title: "Active Root",
      slug: "active-root",
      node_kind: "folder",
      is_system_node: true,
      can_have_children: true,
      children_count: 1,
    });
    const activePage = treeNode({
      id: "active-page-node",
      title: "Active Page",
      node_kind: "page_ref",
      parent_id: activeRoot.id,
      target_kind: "context_page",
      target_id: "page-active",
      breadcrumb: [breadcrumb(activeRoot)],
    });
    const matchRoot = treeNode({
      id: "match-root",
      title: "Match Root",
      slug: "match-root",
      node_kind: "folder",
      can_have_children: true,
      children_count: 1,
    });
    const matchPage = treeNode({
      id: "match-page-node",
      title: "Needle Result",
      node_kind: "page_ref",
      parent_id: matchRoot.id,
      target_kind: "context_page",
      target_id: "page-match",
      breadcrumb: [breadcrumb(matchRoot)],
    });
    const unrelated = treeNode({
      id: "archive-root",
      title: "Archive",
      slug: "archive",
      node_kind: "folder",
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId="page-active"
        expandedNodeIds={[activeRoot.id]}
        forceActivePathExpanded={false}
        onExpandedNodeIdsChange={() => undefined}
        nodes={[activeRoot, activePage, matchRoot, matchPage, unrelated]}
      />,
    );

    expect(
      screen.getByRole("treeitem", { name: "Active Page" }),
    ).toHaveAttribute("data-active-path", "true");
    expect(
      screen.queryByRole("treeitem", { name: "Needle Result" }),
    ).toBeNull();

    await userEvent.type(
      screen.getByLabelText("Filter Context tree"),
      "Needle",
    );

    expect(
      screen.getByRole("treeitem", { name: "Active Root" }),
    ).toHaveAttribute("data-active-path", "true");
    expect(
      screen.getByRole("treeitem", { name: "Active Page" }),
    ).toHaveAttribute("data-active-path", "true");
    expect(
      screen.getByRole("treeitem", { name: "Match Root" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("treeitem", { name: "Needle Result" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: "Archive" })).toBeNull();
  });

  it("selects page-as-parent rows and expands them from the chevron", async () => {
    const onSelectTarget = vi.fn();
    const onExpandedNodeIdsChange = vi.fn();
    const parentPage = treeNode({
      id: "parent-page-node",
      title: "Parent Page",
      node_kind: "page_ref",
      target_kind: "context_page",
      target_id: "page-parent",
      can_have_children: true,
      children_count: 1,
    });
    const childPage = treeNode({
      id: "child-page-node",
      title: "Nested Child",
      node_kind: "page_ref",
      parent_id: parentPage.id,
      target_kind: "context_page",
      target_id: "page-child",
      breadcrumb: [breadcrumb(parentPage)],
    });

    function Harness() {
      const [expandedNodeIds, setExpandedNodeIds] = useState<string[]>([]);
      return (
        <TreeRail
          title="Context"
          activeTargetId={null}
          expandedNodeIds={expandedNodeIds}
          onExpandedNodeIdsChange={(nodeIds) => {
            onExpandedNodeIdsChange(nodeIds);
            setExpandedNodeIds(nodeIds);
          }}
          onSelectTarget={onSelectTarget}
          nodes={[parentPage, childPage]}
        />
      );
    }

    render(<Harness />);

    const parentRow = screen.getByRole("treeitem", { name: "Parent Page" });
    expect(parentRow).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(parentRow);

    expect(onSelectTarget).toHaveBeenCalledWith(
      expect.objectContaining({ target_id: "page-parent" }),
    );
    expect(onExpandedNodeIdsChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("treeitem", { name: "Nested Child" })).toBeNull();

    await userEvent.click(
      screen.getByRole("button", { name: "Expand Parent Page" }),
    );

    expect(onExpandedNodeIdsChange).toHaveBeenCalledWith(["parent-page-node"]);
    expect(onSelectTarget).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("treeitem", { name: "Nested Child" }),
    ).toBeInTheDocument();
  });

  it("selects folder rows without toggling expansion", async () => {
    const onSelectTarget = vi.fn();
    const onExpandedNodeIdsChange = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
      children_count: 1,
    });
    const childPage = treeNode({
      id: "child-page-node",
      title: "Nested Child",
      node_kind: "page_ref",
      parent_id: folder.id,
      target_kind: "context_page",
      target_id: "page-child",
      breadcrumb: [breadcrumb(folder)],
    });

    function Harness() {
      const [expandedNodeIds, setExpandedNodeIds] = useState<string[]>([]);
      return (
        <TreeRail
          title="Context"
          activeTargetId={null}
          expandedNodeIds={expandedNodeIds}
          onExpandedNodeIdsChange={(nodeIds) => {
            onExpandedNodeIdsChange(nodeIds);
            setExpandedNodeIds(nodeIds);
          }}
          onSelectTarget={onSelectTarget}
          nodes={[folder, childPage]}
        />
      );
    }

    render(<Harness />);

    const folderRow = screen.getByRole("treeitem", { name: "Folder" });

    await userEvent.click(folderRow);

    expect(onSelectTarget).toHaveBeenCalledWith(
      expect.objectContaining({ id: "folder-node", target_id: null }),
    );
    expect(onExpandedNodeIdsChange).not.toHaveBeenCalled();
    expect(folderRow).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("treeitem", { name: "Nested Child" })).toBeNull();

    await userEvent.click(
      screen.getByRole("button", { name: "Expand Folder" }),
    );

    expect(onExpandedNodeIdsChange).toHaveBeenCalledWith(["folder-node"]);
    expect(
      screen.getByRole("treeitem", { name: "Nested Child" }),
    ).toBeInTheDocument();
  });

  it("exposes folder creation actions for root and parent nodes", async () => {
    const onCreateFolder = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
      children_count: 0,
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        canCreateFolder
        onCreateFolder={onCreateFolder}
        nodes={[folder]}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Create Context folder" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Create folder under Folder" }),
    );

    expect(onCreateFolder).toHaveBeenNthCalledWith(1, null);
    expect(onCreateFolder).toHaveBeenNthCalledWith(2, "folder-node");
  });

  it("exposes item creation actions for root and parent nodes", async () => {
    const onCreateItem = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
      children_count: 0,
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        canCreateFolder
        canCreatePage
        onCreateItem={onCreateItem}
        nodes={[folder]}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Create Context item" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Create item under Folder" }),
    );

    expect(onCreateItem).toHaveBeenNthCalledWith(1, {
      itemType: "page",
      parentNodeId: null,
    });
    expect(onCreateItem).toHaveBeenNthCalledWith(2, {
      itemType: "page",
      parentNodeId: "folder-node",
    });
  });

  it("exposes refresh as a direct header button when reload is available", async () => {
    const onRefresh = vi.fn();

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onRefresh={onRefresh}
        nodes={[treeNode({ title: "Context root" })]}
      />,
    );

    // Refresh is a direct icon button in the rail header (no "⋯" overflow menu).
    expect(
      screen.queryByRole("button", { name: "More Context actions" }),
    ).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: "Refresh Context tree" }),
    );

    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("renders create, refresh, widen, and hide as direct header buttons", () => {
    const { container } = render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        canCreateFolder
        onClose={() => undefined}
        onCreateFolder={() => undefined}
        onExpandWidth={() => undefined}
        onRefresh={() => undefined}
        nodes={[treeNode({ title: "Context root" })]}
      />,
    );

    const headerActions = container.querySelector(".tree-rail-header-actions");
    expect(headerActions).not.toBeNull();
    // All rail controls are direct buttons in a consistent order: create, then
    // the shared chrome trio refresh -> widen -> hide. No "⋯" overflow trigger.
    expect(
      within(headerActions as HTMLElement)
        .getAllByRole("button")
        .map((button) => button.getAttribute("aria-label")),
    ).toEqual([
      "Create Context folder",
      "Refresh Context tree",
      "Widen Context explorer",
      "Hide Context explorer",
    ]);
    expect(
      screen.queryByRole("button", { name: "More Context actions" }),
    ).toBeNull();
  });

  it("swaps the refresh button label and announces completion via live region", async () => {
    function Harness({ refreshing }: { refreshing: boolean }) {
      return (
        <TreeRail
          title="Context"
          activeTargetId={null}
          onRefresh={() => undefined}
          refreshing={refreshing}
          nodes={[treeNode({ title: "Context root" })]}
        />
      );
    }

    const { rerender } = render(<Harness refreshing={false} />);

    expect(
      screen.getByRole("button", { name: "Refresh Context tree" }),
    ).toBeInTheDocument();

    rerender(<Harness refreshing={true} />);

    const refreshingButton = screen.getByRole("button", {
      name: "Refreshing Context tree…",
    });
    expect(refreshingButton).toHaveAttribute("aria-busy", "true");
    expect(refreshingButton).toBeDisabled();

    rerender(<Harness refreshing={false} />);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Context tree refreshed",
      );
    });
  });

  it("marks the widen button pressed at max width", () => {
    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        expandWidthAtMax
        onExpandWidth={() => undefined}
        nodes={[treeNode({ title: "Context root" })]}
      />,
    );

    const widenButton = screen.getByRole("button", {
      name: "Reset Context width",
    });
    expect(widenButton).toHaveAttribute("aria-pressed", "true");
  });

  it("marks no row active when neither node nor target is selected (C1 regression)", () => {
    const nodes = [
      treeNode({
        id: "folder-a",
        title: "Inbox",
        node_kind: "folder",
        can_have_children: true,
      }),
      treeNode({
        id: "folder-b",
        title: "Ask Captures",
        node_kind: "folder",
        can_have_children: true,
      }),
    ];

    // /pages/new with no destination chosen: activeNodeId and activeTargetId are
    // both null and every folder's target_id is null, so no row may match.
    const { container, rerender } = render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        activeNodeId={null}
        nodes={nodes}
      />,
    );
    expect(container.querySelectorAll(".tree-row.active")).toHaveLength(0);

    // Choosing a destination marks only that single row active.
    rerender(
      <TreeRail
        title="Context"
        activeTargetId={null}
        activeNodeId="folder-b"
        nodes={nodes}
      />,
    );
    const active = container.querySelectorAll(".tree-row.active");
    expect(active).toHaveLength(1);
    expect(active[0].textContent).toContain("Ask Captures");
  });

  it("applies a pressing visual class while a long-press is in progress", () => {
    vi.useFakeTimers();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onRenameNode={() => undefined}
        nodes={[folder]}
      />,
    );

    const row = screen.getByRole("treeitem", { name: "Folder" });
    expect(row).not.toHaveClass("tree-row-pressing");

    fireEvent.pointerDown(row, {
      button: 0,
      clientX: 48,
      clientY: 56,
      pointerType: "touch",
    });

    expect(row).toHaveClass("tree-row-pressing");

    act(() => {
      vi.advanceTimersByTime(550);
    });

    expect(row).not.toHaveClass("tree-row-pressing");
  });

  it("opens node action menu from right-click and Shift+F10", async () => {
    const onRenameNode = vi.fn();
    const onMoveNode = vi.fn();
    const onDeleteNode = vi.fn();
    const onCopyNodeLink = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
      children_count: 0,
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        canCreateFolder
        canCreatePage
        onCopyNodeLink={onCopyNodeLink}
        onDeleteNode={onDeleteNode}
        onMoveNode={onMoveNode}
        onRenameNode={onRenameNode}
        nodes={[folder]}
      />,
    );

    const row = screen.getByRole("treeitem", { name: "Folder" });
    fireEvent.contextMenu(row, { clientX: 40, clientY: 40 });
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(row).toHaveFocus());

    fireEvent.contextMenu(row, { clientX: 40, clientY: 40 });
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    expect(onRenameNode).toHaveBeenCalledWith(
      expect.objectContaining({ id: "folder-node" }),
    );

    row.focus();
    await userEvent.keyboard("{Shift>}{F10}{/Shift}");
    await userEvent.click(screen.getByRole("menuitem", { name: "Move" }));
    expect(onMoveNode).toHaveBeenCalledWith(
      expect.objectContaining({ id: "folder-node" }),
    );
  });

  it("moves draggable rows onto valid parent nodes by drop", () => {
    const onDropNode = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
    });
    const page = treeNode({
      id: "page-node",
      title: "Page",
      node_kind: "page_ref",
      target_kind: "context_page",
      target_id: "page-target",
    });
    const dataTransfer = createDataTransfer();

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onDropNode={onDropNode}
        nodes={[folder, page]}
      />,
    );

    const folderRow = screen.getByRole("treeitem", { name: "Folder" });
    const pageRow = screen.getByRole("treeitem", { name: "Page" });
    expect(pageRow).toHaveAttribute("draggable", "true");

    fireEvent.dragStart(pageRow, { dataTransfer });
    expect(pageRow).toHaveAttribute("aria-grabbed", "true");

    fireEvent.dragOver(folderRow, { dataTransfer });
    expect(folderRow).toHaveAttribute("data-drop-target", "true");

    fireEvent.drop(folderRow, { dataTransfer });

    expect(onDropNode).toHaveBeenCalledWith(
      expect.objectContaining({ id: "page-node" }),
      expect.objectContaining({ id: "folder-node" }),
    );
  });

  it("uses folder outer thirds for sibling insertion and the middle for reparent", async () => {
    const onDropNode = vi.fn();
    const onReorderNodeAt = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
    });
    const page = treeNode({
      id: "page-node",
      title: "Page",
      target_kind: "context_page",
      target_id: "page-target",
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onDropNode={onDropNode}
        onReorderNodeAt={onReorderNodeAt}
        nodes={[folder, page]}
      />,
    );

    const folderRow = screen.getByRole("treeitem", { name: "Folder" });
    const pageRow = screen.getByRole("treeitem", { name: "Page" });
    vi.spyOn(folderRow, "getBoundingClientRect").mockReturnValue(
      rowBounds({ top: 100, height: 90 }),
    );

    let dataTransfer = createDataTransfer();
    fireEvent.dragStart(pageRow, { dataTransfer });
    fireTreeDragEvent(folderRow, "dragover", { clientY: 110, dataTransfer });
    expect(folderRow).toHaveAttribute("data-drop-placement", "before");
    expect(folderRow).toHaveClass("tree-row-insert-before");
    fireTreeDragEvent(folderRow, "drop", { clientY: 110, dataTransfer });
    await waitFor(() =>
      expect(onReorderNodeAt).toHaveBeenCalledWith(page, folder, "before"),
    );

    dataTransfer = createDataTransfer();
    fireEvent.dragStart(pageRow, { dataTransfer });
    fireTreeDragEvent(folderRow, "dragover", { clientY: 180, dataTransfer });
    expect(folderRow).toHaveAttribute("data-drop-placement", "after");
    expect(folderRow).toHaveClass("tree-row-insert-after");
    fireTreeDragEvent(folderRow, "drop", { clientY: 180, dataTransfer });
    await waitFor(() =>
      expect(onReorderNodeAt).toHaveBeenCalledWith(page, folder, "after"),
    );

    dataTransfer = createDataTransfer();
    fireEvent.dragStart(pageRow, { dataTransfer });
    fireTreeDragEvent(folderRow, "dragover", { clientY: 145, dataTransfer });
    expect(folderRow).toHaveAttribute("data-drop-placement", "inside");
    fireTreeDragEvent(folderRow, "drop", { clientY: 145, dataTransfer });
    await waitFor(() => expect(onDropNode).toHaveBeenCalledWith(page, folder));
  });

  it("splits a page row into before/after only and never reparents into it", async () => {
    const onDropNode = vi.fn();
    const onReorderNodeAt = vi.fn();
    const moving = treeNode({
      id: "moving-node",
      title: "Moving",
      sort_key: "0001",
    });
    const anchor = treeNode({
      id: "anchor-node",
      title: "Anchor",
      sort_key: "0002",
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onDropNode={onDropNode}
        onReorderNodeAt={onReorderNodeAt}
        nodes={[moving, anchor]}
      />,
    );

    const movingRow = screen.getByRole("treeitem", { name: "Moving" });
    const anchorRow = screen.getByRole("treeitem", { name: "Anchor" });
    vi.spyOn(anchorRow, "getBoundingClientRect").mockReturnValue(
      rowBounds({ top: 100, height: 90 }),
    );
    const dataTransfer = createDataTransfer();
    fireEvent.dragStart(movingRow, { dataTransfer });
    fireTreeDragEvent(anchorRow, "dragover", { clientY: 145, dataTransfer });
    expect(anchorRow).toHaveAttribute("data-drop-placement", "after");
    fireTreeDragEvent(anchorRow, "drop", { clientY: 145, dataTransfer });

    await waitFor(() =>
      expect(onReorderNodeAt).toHaveBeenCalledWith(moving, anchor, "after"),
    );
    expect(onDropNode).not.toHaveBeenCalled();
  });

  it("drops beside a row under another parent and at the top of an expanded folder", async () => {
    const onReorderNodeAt = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
      children_count: 2,
      sort_key: "0001",
    });
    const first = treeNode({
      id: "first-node",
      title: "First",
      parent_id: folder.id,
      sort_key: "0001",
      breadcrumb: [breadcrumb(folder)],
    });
    const second = treeNode({
      id: "second-node",
      title: "Second",
      parent_id: folder.id,
      sort_key: "0002",
      breadcrumb: [breadcrumb(folder)],
    });
    const outside = treeNode({ id: "outside-node", title: "Outside", sort_key: "0002" });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        expandedNodeIds={[folder.id]}
        onDropNode={vi.fn()}
        onReorderNodeAt={onReorderNodeAt}
        nodes={[folder, first, second, outside]}
      />,
    );
    const row = (name: string) => screen.getByRole("treeitem", { name });
    for (const name of ["Folder", "First", "Second"]) {
      vi.spyOn(row(name), "getBoundingClientRect").mockReturnValue(
        rowBounds({ top: 100, height: 90 }),
      );
    }

    let dataTransfer = createDataTransfer();
    fireEvent.dragStart(row("Outside"), { dataTransfer });
    fireTreeDragEvent(row("Second"), "dragover", { clientY: 110, dataTransfer });
    expect(row("Second")).toHaveAttribute("data-drop-placement", "before");
    fireTreeDragEvent(row("Second"), "drop", { clientY: 110, dataTransfer });
    await waitFor(() =>
      expect(onReorderNodeAt).toHaveBeenCalledWith(outside, second, "before"),
    );

    // The gap under an expanded folder is its children's top, not the slot
    // after the folder's whole subtree.
    dataTransfer = createDataTransfer();
    fireEvent.dragStart(row("Outside"), { dataTransfer });
    fireTreeDragEvent(row("Folder"), "dragover", { clientY: 180, dataTransfer });
    expect(row("Folder")).toHaveAttribute("data-drop-placement", "first-child");
    expect(row("Folder")).toHaveClass("tree-row-insert-first-child");
    fireTreeDragEvent(row("Folder"), "drop", { clientY: 180, dataTransfer });
    await waitFor(() =>
      expect(onReorderNodeAt).toHaveBeenCalledWith(outside, first, "before"),
    );

    dataTransfer = createDataTransfer();
    fireEvent.dragStart(row("Folder"), { dataTransfer });
    fireTreeDragEvent(row("Second"), "dragover", { clientY: 110, dataTransfer });
    expect(row("Second")).not.toHaveAttribute("data-drop-placement");
  });

  it("gives touch drags wide insertion bands, a drop hint, and closes the long-press menu", () => {
    vi.useFakeTimers();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
    });
    const page = treeNode({ id: "page-node", title: "Page", sort_key: "0002" });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onDropNode={vi.fn()}
        onRenameNode={vi.fn()}
        onReorderNodeAt={vi.fn()}
        nodes={[folder, page]}
      />,
    );
    const folderRow = screen.getByRole("treeitem", { name: "Folder" });
    const pageRow = screen.getByRole("treeitem", { name: "Page" });
    vi.spyOn(folderRow, "getBoundingClientRect").mockReturnValue(
      rowBounds({ top: 100, height: 48 }),
    );

    fireEvent.pointerDown(pageRow, { button: 0, clientX: 48, clientY: 56, pointerType: "touch" });
    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(screen.getByRole("menu")).toBeInTheDocument();

    const dataTransfer = createDataTransfer();
    fireEvent.dragStart(pageRow, { dataTransfer });
    expect(screen.queryByRole("menu")).toBeNull();

    // 17px down a 48px folder row: a mouse drag would nest here, a finger
    // still inserts before it.
    fireTreeDragEvent(folderRow, "dragover", { clientY: 117, dataTransfer });
    expect(folderRow).toHaveAttribute("data-drop-placement", "before");
    expect(document.querySelector(".tree-drop-hint")).toHaveTextContent("Before Folder");
    fireTreeDragEvent(folderRow, "dragover", { clientY: 124, dataTransfer });
    expect(document.querySelector(".tree-drop-hint")).toHaveTextContent("Into Folder");
  });

  it("disables sibling insertion while the tree filter is active", async () => {
    const onReorderNodeAt = vi.fn();
    const moving = treeNode({
      id: "moving-node",
      title: "Node Moving",
      sort_key: "0001",
    });
    const anchor = treeNode({
      id: "anchor-node",
      title: "Node Anchor",
      sort_key: "0002",
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onReorderNodeAt={onReorderNodeAt}
        nodes={[moving, anchor]}
      />,
    );
    await userEvent.type(screen.getByLabelText("Filter Context tree"), "Node");

    const movingRow = screen.getByRole("treeitem", { name: "Node Moving" });
    const anchorRow = screen.getByRole("treeitem", { name: "Node Anchor" });
    vi.spyOn(anchorRow, "getBoundingClientRect").mockReturnValue(
      rowBounds({ top: 100, height: 90 }),
    );
    const dataTransfer = createDataTransfer();
    fireEvent.dragStart(movingRow, { dataTransfer });
    fireTreeDragEvent(anchorRow, "dragover", { clientY: 110, dataTransfer });
    fireTreeDragEvent(anchorRow, "drop", { clientY: 110, dataTransfer });

    expect(anchorRow).not.toHaveAttribute("data-drop-placement");
    expect(onReorderNodeAt).not.toHaveBeenCalled();
  });

  it("reorders from Shift+F10 and announces the explicit landing", async () => {
    const onReorderNodeAt = vi.fn();
    const alpha = treeNode({
      id: "alpha-node",
      title: "Alpha",
      sort_key: "0001",
    });
    const bravo = treeNode({
      id: "bravo-node",
      title: "Bravo",
      sort_key: "0002",
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onReorderNodeAt={onReorderNodeAt}
        nodes={[alpha, bravo]}
      />,
    );

    const bravoRow = screen.getByRole("treeitem", { name: "Bravo" });
    bravoRow.focus();
    await userEvent.keyboard("{Shift>}{F10}{/Shift}");
    await userEvent.click(screen.getByRole("menuitem", { name: "Move up" }));

    await waitFor(() =>
      expect(onReorderNodeAt).toHaveBeenCalledWith(bravo, alpha, "before"),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Bravo moved before Alpha.",
    );
    await waitFor(() => expect(bravoRow).toHaveFocus());
  });

  it("blocks tree drops onto descendant nodes", () => {
    const onDropNode = vi.fn();
    const parent = treeNode({
      id: "parent-node",
      title: "Parent",
      node_kind: "folder",
      can_have_children: true,
      children_count: 1,
    });
    const child = treeNode({
      id: "child-node",
      title: "Child",
      node_kind: "folder",
      parent_id: parent.id,
      can_have_children: true,
      breadcrumb: [breadcrumb(parent)],
    });
    const dataTransfer = createDataTransfer();

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        expandedNodeIds={[parent.id]}
        onDropNode={onDropNode}
        nodes={[parent, child]}
      />,
    );

    const parentRow = screen.getByRole("treeitem", { name: "Parent" });
    const childRow = screen.getByRole("treeitem", { name: "Child" });

    fireEvent.dragStart(parentRow, { dataTransfer });
    fireEvent.dragOver(childRow, { dataTransfer });
    fireEvent.drop(childRow, { dataTransfer });

    expect(childRow).toHaveAttribute("data-drop-target", "false");
    expect(onDropNode).not.toHaveBeenCalled();
  });

  it("keeps nested action button keyboard events from selecting the row", async () => {
    const onRenameNode = vi.fn();
    const onSelectTarget = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
      children_count: 0,
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onRenameNode={onRenameNode}
        onSelectTarget={onSelectTarget}
        nodes={[folder]}
      />,
    );

    screen.getByRole("button", { name: "Open actions for Folder" }).focus();
    await userEvent.keyboard("{Enter}");

    expect(
      screen.getByRole("menuitem", { name: "Rename" }),
    ).toBeInTheDocument();
    expect(onSelectTarget).not.toHaveBeenCalled();
  });

  it("opens node action menu from touch long-press without selecting the row", async () => {
    vi.useFakeTimers();
    const onRenameNode = vi.fn();
    const onSelectTarget = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
      can_have_children: true,
      children_count: 0,
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onRenameNode={onRenameNode}
        onSelectTarget={onSelectTarget}
        nodes={[folder]}
      />,
    );

    const row = screen.getByRole("treeitem", { name: "Folder" });
    fireEvent.pointerDown(row, {
      button: 0,
      clientX: 48,
      clientY: 56,
      pointerType: "touch",
    });
    act(() => {
      vi.advanceTimersByTime(549);
    });
    expect(screen.queryByRole("menu")).toBeNull();

    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(
      screen.getByRole("menuitem", { name: "Rename" }),
    ).toBeInTheDocument();
    fireEvent.pointerUp(row, { pointerType: "touch" });
    fireEvent.click(row);

    expect(onSelectTarget).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    expect(onRenameNode).toHaveBeenCalledWith(
      expect.objectContaining({ id: "folder-node" }),
    );
  });

  it("cancels touch long-press context menu when the pointer moves away", () => {
    vi.useFakeTimers();
    const onRenameNode = vi.fn();
    const folder = treeNode({
      id: "folder-node",
      title: "Folder",
      node_kind: "folder",
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onRenameNode={onRenameNode}
        nodes={[folder]}
      />,
    );

    const row = screen.getByRole("treeitem", { name: "Folder" });
    fireEvent.pointerDown(row, {
      button: 0,
      clientX: 48,
      clientY: 56,
      pointerType: "touch",
    });
    fireEvent.pointerMove(row, {
      clientX: 70,
      clientY: 56,
      pointerType: "touch",
    });
    act(() => {
      vi.advanceTimersByTime(550);
    });

    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("hides folder creation actions when the section policy disables them", () => {
    render(
      <TreeRail
        title="Library"
        activeTargetId={null}
        canCreateFolder={false}
        onCreateFolder={() => undefined}
        nodes={[
          treeNode({
            id: "folder-node",
            title: "Folder",
            node_kind: "folder",
            can_have_children: true,
          }),
        ]}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "Create Library folder" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Create folder under Folder" }),
    ).toBeNull();
  });

  it("expands unselected folders directly from the chevron", async () => {
    function Harness() {
      const [expandedNodeIds, setExpandedNodeIds] = useState<string[]>([]);
      return (
        <TreeRail
          title="Context"
          activeTargetId={null}
          expandedNodeIds={expandedNodeIds}
          onExpandedNodeIdsChange={setExpandedNodeIds}
          nodes={[
            treeNode({
              id: "folder-node",
              title: "Folder",
              node_kind: "folder",
              can_have_children: true,
              children_count: 1,
            }),
            treeNode({
              id: "child-node",
              title: "Child",
              node_kind: "page_ref",
              parent_id: "folder-node",
              target_kind: "context_page",
              target_id: "page-child",
            }),
          ]}
        />
      );
    }

    render(<Harness />);

    await userEvent.click(
      screen.getByRole("button", { name: "Expand Folder" }),
    );

    expect(screen.getByRole("treeitem", { name: "Child" })).toBeInTheDocument();
  });

  it("hides section system roots and promotes their children", async () => {
    const root = treeNode({
      id: "context-root",
      title: "Context",
      slug: "context",
      node_kind: "folder",
      is_system_node: true,
      can_have_children: true,
      children_count: 1,
    });
    const inbox = treeNode({
      id: "inbox-node",
      title: "Inbox",
      slug: "inbox",
      node_kind: "folder",
      parent_id: root.id,
      is_system_node: true,
      can_have_children: true,
      children_count: 1,
      breadcrumb: [breadcrumb(root)],
    });
    const page = treeNode({
      id: "page-node",
      title: "Draft Page",
      node_kind: "page_ref",
      parent_id: inbox.id,
      target_kind: "context_page",
      target_id: "page-1",
      breadcrumb: [breadcrumb(root), breadcrumb(inbox)],
    });

    function Harness() {
      const [expandedNodeIds, setExpandedNodeIds] = useState<string[]>([]);
      return (
        <TreeRail
          title="Context"
          activeTargetId={null}
          expandedNodeIds={expandedNodeIds}
          hideSystemRootNodes
          onExpandedNodeIdsChange={setExpandedNodeIds}
          nodes={[root, inbox, page]}
        />
      );
    }

    render(<Harness />);

    expect(screen.queryByRole("treeitem", { name: "Context" })).toBeNull();
    const inboxRow = screen.getByRole("treeitem", { name: "Inbox" });
    expect(inboxRow).toHaveAttribute("aria-level", "1");
    expect(inboxRow.querySelectorAll(".tree-row-indent-guide")).toHaveLength(0);
    expect(inboxRow).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("treeitem", { name: "Draft Page" })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Expand Inbox" }));

    const pageRow = screen.getByRole("treeitem", { name: "Draft Page" });
    expect(pageRow).toHaveAttribute("aria-level", "2");
    expect(pageRow.querySelectorAll(".tree-row-indent-guide")).toHaveLength(1);
  });

  it("hides a legacy Content system root on the Context rail", () => {
    const root = treeNode({
      id: "content-root",
      title: "Content",
      slug: "content",
      node_kind: "folder",
      is_system_node: true,
      can_have_children: true,
      children_count: 1,
    });
    const page = treeNode({
      id: "page-node",
      title: "Visible Context Page",
      node_kind: "page_ref",
      parent_id: root.id,
      target_kind: "context_page",
      target_id: "page-1",
      breadcrumb: [breadcrumb(root)],
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        hideSystemRootNodes
        nodes={[root, page]}
      />,
    );

    expect(screen.queryByRole("treeitem", { name: "Content" })).toBeNull();
    expect(
      screen.getByRole("treeitem", { name: "Visible Context Page" }),
    ).toHaveAttribute("aria-level", "1");
  });

  it("hides the legacy Content root with a Korean surface label", () => {
    const root = treeNode({
      id: "content-root-ko",
      title: "Content",
      slug: "content",
      node_kind: "folder",
      is_system_node: true,
      can_have_children: true,
      children_count: 1,
    });
    const page = treeNode({
      id: "page-node-ko",
      title: "한국어 문서",
      node_kind: "page_ref",
      parent_id: root.id,
      target_kind: "context_page",
      target_id: "page-ko",
      breadcrumb: [breadcrumb(root)],
    });

    render(
      <TreeRail
        title="컨텍스트"
        activeTargetId="page-ko"
        hideSystemRootNodes
        nodes={[root, page]}
      />,
    );

    expect(screen.queryByRole("treeitem", { name: "Content" })).toBeNull();
    expect(
      screen.getByRole("treeitem", { name: "한국어 문서" }),
    ).toHaveAttribute("aria-level", "1");
  });

  it("collapses an active child ancestor from the chevron without selecting the folder", async () => {
    const activeRoot = treeNode({
      id: "active-root",
      title: "Active Root",
      slug: "active-root",
      node_kind: "folder",
      can_have_children: true,
      children_count: 1,
    });
    const activePage = treeNode({
      id: "active-page-node",
      title: "Active Page",
      node_kind: "page_ref",
      parent_id: activeRoot.id,
      target_kind: "context_page",
      target_id: "page-active",
      breadcrumb: [breadcrumb(activeRoot)],
    });

    function Harness() {
      const [expandedNodeIds, setExpandedNodeIds] = useState<string[]>([
        activeRoot.id,
      ]);
      return (
        <TreeRail
          title="Context"
          activeTargetId="page-active"
          expandedNodeIds={expandedNodeIds}
          onExpandedNodeIdsChange={setExpandedNodeIds}
          nodes={[activeRoot, activePage]}
        />
      );
    }

    render(<Harness />);

    expect(
      screen.getByRole("treeitem", { name: "Active Root" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("treeitem", { name: "Active Page" }),
    ).toHaveAttribute("aria-selected", "true");

    await userEvent.click(
      screen.getByRole("button", { name: "Collapse Active Root" }),
    );

    expect(
      screen.getByRole("treeitem", { name: "Active Root" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("treeitem", { name: "Active Page" })).toBeNull();
  });

  it("uses one tree tab stop and supports arrow-key navigation", async () => {
    function Harness() {
      const [expandedNodeIds, setExpandedNodeIds] = useState<string[]>([
        "root-node",
      ]);
      return (
        <TreeRail
          title="Context"
          activeTargetId="page-child"
          expandedNodeIds={expandedNodeIds}
          onExpandedNodeIdsChange={setExpandedNodeIds}
          onSelectTarget={() => undefined}
          nodes={[
            treeNode({
              id: "root-node",
              title: "Root",
              node_kind: "folder",
              can_have_children: true,
              children_count: 1,
            }),
            treeNode({
              id: "child-node",
              title: "Child",
              node_kind: "page_ref",
              parent_id: "root-node",
              target_kind: "context_page",
              target_id: "page-child",
              breadcrumb: [
                breadcrumb(treeNode({ id: "root-node", title: "Root" })),
              ],
            }),
            treeNode({
              id: "sibling-node",
              title: "Sibling",
              node_kind: "page_ref",
              target_kind: "context_page",
              target_id: "page-sibling",
            }),
          ]}
        />
      );
    }

    render(<Harness />);

    const root = screen.getByRole("treeitem", { name: "Root" });
    const child = screen.getByRole("treeitem", { name: "Child" });
    const sibling = screen.getByRole("treeitem", { name: "Sibling" });
    expect(root).toHaveAttribute("tabindex", "-1");
    expect(child).toHaveAttribute("tabindex", "0");
    expect(sibling).toHaveAttribute("tabindex", "-1");

    child.focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(sibling).toHaveFocus();
    expect(sibling).toHaveAttribute("tabindex", "0");
    expect(child).toHaveAttribute("tabindex", "-1");

    await userEvent.keyboard("{Home}");
    expect(root).toHaveFocus();

    await userEvent.keyboard("{ArrowRight}");
    expect(child).toHaveFocus();

    await userEvent.keyboard("{ArrowLeft}");
    expect(root).toHaveFocus();

    await userEvent.keyboard("{ArrowLeft}");
    expect(root).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("treeitem", { name: "Child" })).toBeNull();
  });

  it("renders system badges, child counts, and target-kind dots", () => {
    const systemRoot = treeNode({
      id: "system-root",
      title: "System Root",
      slug: "system-root",
      node_kind: "folder",
      is_system_node: true,
      can_have_children: true,
      children_count: 1,
    });
    const contextPage = treeNode({
      id: "context-page-node",
      title: "Context Page",
      node_kind: "page_ref",
      parent_id: systemRoot.id,
      target_kind: "context_page",
      target_id: "context-page",
      breadcrumb: [breadcrumb(systemRoot)],
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        expandedNodeIds={[systemRoot.id]}
        onExpandedNodeIdsChange={() => undefined}
        nodes={[systemRoot, contextPage]}
      />,
    );

    const systemRow = screen.getByRole("treeitem", { name: "System Root" });
    expect(systemRow).toHaveAttribute("data-system-node", "true");
    expect(
      systemRow.querySelector(".tree-row-system-badge"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("System-managed folder")).toBeInTheDocument();
    expect(systemRow.querySelector(".tree-row-child-count")).toHaveTextContent(
      "1",
    );

    const contextRow = screen.getByRole("treeitem", { name: "Context Page" });
    expect(contextRow).toHaveAttribute("data-target-kind", "context_page");
    expect(contextRow.querySelector(".tree-row-state-dot")).toHaveClass(
      "tree-row-state-normal",
    );
  });

  it("keeps folder-only branches expandable without showing a count badge", () => {
    const parent = treeNode({
      id: "folder-only-parent",
      title: "Folder only",
      node_kind: "folder",
      can_have_children: true,
      children_count: 0,
      has_children: true,
    });
    const childFolder = treeNode({
      id: "child-folder",
      title: "Child folder",
      node_kind: "folder",
      parent_id: parent.id,
      can_have_children: true,
      children_count: 0,
      has_children: false,
      breadcrumb: [breadcrumb(parent)],
    });

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        expandedNodeIds={[]}
        onExpandedNodeIdsChange={() => undefined}
        nodes={[parent, childFolder]}
      />,
    );

    const row = screen.getByRole("treeitem", { name: "Folder only" });
    expect(row).toHaveAttribute("aria-expanded", "false");
    expect(row.querySelector(".tree-row-chevron")).toBeInTheDocument();
    expect(row.querySelector(".tree-row-child-count")).not.toBeInTheDocument();
  });

  it("renders a library target-kind dot for nodes whose target_kind starts with 'library'", () => {
    const libraryItem = treeNode({
      id: "library-item-node",
      title: "Library Item",
      node_kind: "page_ref",
      target_kind: "library_item",
      target_id: "lib-1",
    });
    const libraryArtifact = treeNode({
      id: "library-artifact-node",
      title: "Library Artifact",
      node_kind: "page_ref",
      target_kind: "library_artifact",
      target_id: "lib-2",
    });
    const libraryItemRef = treeNode({
      id: "library-item-ref-node",
      title: "Library Item Ref",
      node_kind: "page_ref",
      target_kind: "library_item_ref",
      target_id: "lib-3",
    });

    render(
      <TreeRail
        title="Library"
        activeTargetId={null}
        nodes={[libraryItem, libraryArtifact, libraryItemRef]}
      />,
    );

    const itemRow = screen.getByRole("treeitem", { name: "Library Item" });
    expect(itemRow).toHaveAttribute("data-target-kind", "library_item");
    expect(itemRow.querySelector(".tree-row-state-dot")).toHaveClass(
      "tree-row-state-normal",
    );
    expect(itemRow.querySelector("[data-target-group]")).toHaveAttribute(
      "data-target-group",
      "library",
    );

    const artifactRow = screen.getByRole("treeitem", {
      name: "Library Artifact",
    });
    expect(artifactRow.querySelector(".tree-row-state-dot")).toHaveClass(
      "lc-dot-library",
    );

    const refRow = screen.getByRole("treeitem", { name: "Library Item Ref" });
    expect(refRow.querySelector(".tree-row-state-dot")).toHaveClass(
      "lc-dot-library",
    );
  });

  it("applies the requested data-space accent to the rail", () => {
    render(
      <TreeRail
        title="Library"
        accent="library"
        activeTargetId={null}
        nodes={[treeNode({ title: "Library root" })]}
      />,
    );

    expect(screen.getByLabelText("Library tree")).toHaveAttribute(
      "data-accent",
      "library",
    );
  });

  it("renders the full row title and lets CSS clip it at the rail width", () => {
    const longTitle = "Codex API Markdown 1779209673276 Long Context Draft";
    const getRect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue(new DOMRect(0, 100, 280, 36));

    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        nodes={[
          treeNode({
            id: "long-title-node",
            title: longTitle,
            target_kind: "context_page",
            target_id: "long-title-page",
          }),
        ]}
      />,
    );

    const row = screen.getByRole("treeitem", { name: longTitle });
    const title = row.querySelector(".tree-row-title");
    fireEvent.focus(row);
    expect(screen.getByRole("tooltip")).toHaveTextContent(longTitle);
    expect(title).toHaveClass("tree-row-title");
    expect(title).toHaveAttribute("aria-label", longTitle);
    // The JS middle-ellipsis was dropped: the full title is in the DOM (no "...")
    // so .tree-row-title can ellipsize responsively at the actual rail width.
    expect(title).toHaveTextContent(longTitle);
    expect(title?.textContent).not.toContain("...");
    getRect.mockRestore();
  });

  it("orders duplicate-sort-key siblings by codepoint to match backend reorder order", () => {
    const alpha = treeNode({
      id: "alpha-node",
      title: "alpha",
      target_kind: "context_page",
      target_id: "page-alpha",
      sort_key: "0001",
    });
    const beta = treeNode({
      id: "beta-node",
      title: "Beta",
      target_kind: "context_page",
      target_id: "page-beta",
      sort_key: "0001",
    });

    render(
      <TreeRail title="Context" activeTargetId={null} nodes={[alpha, beta]} />,
    );

    // Python tuple comparison sorts "Beta" (U+0042) before "alpha" (U+0061);
    // locale collation would flip them and desync Move up/down swaps.
    expect(
      screen.getAllByRole("treeitem").map((row) => row.dataset.nodeId),
    ).toEqual(["beta-node", "alpha-node"]);
  });

  it("counts only content nodes in the header badge, with and without a filter", async () => {
    const folder = treeNode({
      id: "guides-folder",
      title: "Guides",
      slug: "guides",
      node_kind: "folder",
      can_have_children: true,
      children_count: 1,
    });
    const nestedPage = treeNode({
      id: "alpha-page-node",
      title: "Alpha Page",
      node_kind: "page_ref",
      parent_id: folder.id,
      target_kind: "context_page",
      target_id: "page-alpha",
      breadcrumb: [breadcrumb(folder)],
    });
    const rootPage = treeNode({
      id: "beta-page-node",
      title: "Beta Page",
      node_kind: "page_ref",
      target_kind: "context_page",
      target_id: "page-beta",
    });

    const { container } = render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        expandedNodeIds={[]}
        onExpandedNodeIdsChange={() => undefined}
        nodes={[folder, nestedPage, rootPage]}
      />,
    );

    // The count moved out of the icon-button cluster to sit beside the title,
    // as the shared CountChip rather than a bare number.
    const countBadge = container.querySelector(".tree-rail-header .count-chip");
    expect(countBadge).toHaveTextContent("2");

    await userEvent.type(screen.getByLabelText("Filter Context tree"), "Alpha");

    expect(countBadge).toHaveTextContent("1/2");
  });

  it("renders nodes in hierarchy order even when API rows are flat-sorted", () => {
    const root = treeNode({
      id: "ask-root",
      title: "Ask",
      slug: "ask",
      node_kind: "folder",
      can_have_children: true,
      children_count: 1,
      sort_key: "0001",
    });
    const year = treeNode({
      id: "year-2026",
      title: "2026",
      slug: "ask-2026",
      node_kind: "folder",
      parent_id: root.id,
      can_have_children: true,
      children_count: 1,
      sort_key: "2026",
      breadcrumb: [breadcrumb(root)],
    });
    const month = treeNode({
      id: "month-05",
      title: "05",
      slug: "ask-2026-05",
      node_kind: "folder",
      parent_id: year.id,
      can_have_children: true,
      children_count: 1,
      sort_key: "05",
      breadcrumb: [breadcrumb(root), breadcrumb(year)],
    });
    const day = treeNode({
      id: "day-19",
      title: "19",
      slug: "ask-2026-05-19",
      node_kind: "folder",
      parent_id: month.id,
      can_have_children: true,
      children_count: 1,
      sort_key: "19",
      breadcrumb: [breadcrumb(root), breadcrumb(year), breadcrumb(month)],
    });
    const session = treeNode({
      id: "session-node",
      title: "New Ask",
      node_kind: "ask_session_ref",
      parent_id: day.id,
      target_kind: "ask_session",
      target_id: "session-1",
      sort_key: "0001",
      breadcrumb: [
        breadcrumb(root),
        breadcrumb(year),
        breadcrumb(month),
        breadcrumb(day),
      ],
    });

    render(
      <TreeRail
        title="Ask"
        expandedNodeIds={[root.id, year.id, month.id, day.id]}
        onExpandedNodeIdsChange={() => undefined}
        nodes={[root, day, session, month, year]}
      />,
    );

    expect(
      screen.getAllByRole("treeitem").map((row) => row.dataset.nodeId),
    ).toEqual(["ask-root", "year-2026", "month-05", "day-19", "session-node"]);
  });

  it("renders one depth guide per ancestor level and never marks a row's path", () => {
    const root = treeNode({
      id: "guide-root",
      title: "Root",
      node_kind: "folder",
      can_have_children: true,
      children_count: 1,
    });
    const folder = treeNode({
      id: "guide-folder",
      title: "Folder",
      node_kind: "folder",
      parent_id: root.id,
      can_have_children: true,
      children_count: 1,
      breadcrumb: [breadcrumb(root)],
    });
    const child = treeNode({
      id: "guide-child",
      title: "Child",
      parent_id: folder.id,
      target_kind: "context_page",
      target_id: "guide-page",
      breadcrumb: [breadcrumb(root), breadcrumb(folder)],
    });

    render(
      <TreeRail
        title="Context"
        expandedNodeIds={[root.id, folder.id]}
        onExpandedNodeIdsChange={() => undefined}
        nodes={[root, folder, child]}
      />,
    );

    const rootRow = screen.getByRole("treeitem", { name: "Root" });
    const folderRow = screen.getByRole("treeitem", { name: "Folder" });
    const childRow = screen.getByRole("treeitem", { name: "Child" });
    expect(rootRow).toHaveAttribute("aria-level", "1");
    expect(folderRow).toHaveAttribute("aria-level", "2");
    expect(childRow).toHaveAttribute("aria-level", "3");
    expect(rootRow.querySelectorAll(".tree-row-indent-guide")).toHaveLength(0);
    expect(folderRow.querySelectorAll(".tree-row-indent-guide")).toHaveLength(1);
    expect(childRow.querySelectorAll(".tree-row-indent-guide")).toHaveLength(2);
    expect(childRow.querySelector(".tree-row-indent-guides"))
      .toHaveAttribute("aria-hidden", "true");
    expect(childRow.querySelector(".tree-row-indent-guides"))
      .toHaveClass("tree-row-indent-guides");

    // User-directed 2026-09-13: no row state marks the depth guides any more.
    // Hovering or focusing a row used to stamp `data-tree-guide-path` on that
    // row and every ancestor, which the stylesheet repainted in the rail accent
    // -- and selecting a row focuses it, so picking any item left its whole
    // ancestor chain lit. Nothing may mark the chain again.
    const rows = [rootRow, folderRow, childRow];
    const expectNoGuidePathMarking = () => {
      for (const row of rows) {
        expect(row).not.toHaveAttribute("data-tree-guide-path");
      }
    };

    fireEvent.pointerEnter(childRow, { pointerType: "mouse" });
    expectNoGuidePathMarking();

    fireEvent.pointerLeave(childRow, { pointerType: "mouse" });
    expectNoGuidePathMarking();

    act(() => {
      childRow.focus();
    });
    expectNoGuidePathMarking();

    fireEvent.click(childRow);
    expectNoGuidePathMarking();

    fireEvent.pointerEnter(rootRow, { pointerType: "mouse" });
    expectNoGuidePathMarking();
    fireEvent.pointerLeave(rootRow, { pointerType: "mouse" });
    expectNoGuidePathMarking();
  });
});

function treeNode(overrides: Partial<TreeNodePayload>): TreeNodePayload {
  return {
    id: "node",
    section: "context",
    title: "Node",
    slug: null,
    node_kind: "page_ref",
    parent_id: null,
    sort_key: "0001",
    target_kind: null,
    target_id: null,
    is_system_node: false,
    can_have_children: false,
    primary_placement: true,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: [],
    ...overrides,
  };
}

function breadcrumb(node: TreeNodePayload) {
  return {
    id: node.id,
    section: node.section,
    title: node.title,
    slug: node.slug,
    node_kind: node.node_kind,
  };
}

function createDataTransfer() {
  const values = new Map<string, string>();
  return {
    dropEffect: "none",
    effectAllowed: "all",
    getData: vi.fn((type: string) => values.get(type) ?? ""),
    setData: vi.fn((type: string, value: string) => {
      values.set(type, value);
    }),
  };
}

function rowBounds({ top, height }: { top: number; height: number }): DOMRect {
  return {
    bottom: top + height,
    height,
    left: 0,
    right: 240,
    top,
    width: 240,
    x: 0,
    y: top,
    toJSON: () => ({}),
  };
}

function fireTreeDragEvent(
  element: HTMLElement,
  type: "dragover" | "drop",
  {
    clientY,
    dataTransfer,
  }: {
    clientY: number;
    dataTransfer: ReturnType<typeof createDataTransfer>;
  },
) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperties(event, {
    clientY: { value: clientY },
    dataTransfer: { value: dataTransfer },
  });
  fireEvent(element, event);
}

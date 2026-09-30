import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TreeNodePayload } from "../../api/workspace";
import { TreeNodeContextMenu } from "../TreeNodeContextMenu";

describe("TreeNodeContextMenu", () => {
  it("emits the selected node action with the source node", () => {
    const onAction = vi.fn();
    const node = treeNode({
      id: "folder-node",
      title: "Folder",
      can_have_children: true,
      children_count: 0,
    });

    render(
      <TreeNodeContextMenu
        canCopyLink
        canCreateFolder
        canCreatePage
        canDelete
        canMove
        canRename
        node={node}
        position={{ left: 40, top: 56 }}
        onAction={onAction}
        onClose={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("menuitem", { name: "New page" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Move" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));

    expect(onAction).toHaveBeenNthCalledWith(1, {
      itemType: "page",
      node,
      type: "new-item",
    });
    expect(onAction).toHaveBeenNthCalledWith(2, { node, type: "move" });
    expect(onAction).toHaveBeenNthCalledWith(3, { node, type: "delete" });
  });

  it("hides child creation actions for leaf nodes and closes on Escape", () => {
    const onClose = vi.fn();

    render(
      <TreeNodeContextMenu
        canCopyLink={false}
        canCreateFolder
        canCreatePage
        canDelete={false}
        canMove={false}
        canRename
        node={treeNode({ can_have_children: false })}
        position={{ left: 0, top: 0 }}
        onAction={vi.fn()}
        onClose={onClose}
      />,
    );

    expect(screen.queryByRole("menuitem", { name: "New page" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Rename" })).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes when the user clicks outside the menu", () => {
    const onClose = vi.fn();

    render(
      <TreeNodeContextMenu
        canCopyLink={false}
        canCreateFolder={false}
        canCreatePage={false}
        canDelete={false}
        canMove={false}
        canRename
        node={treeNode({})}
        position={{ left: 0, top: 0 }}
        onAction={vi.fn()}
        onClose={onClose}
      />,
    );

    fireEvent.pointerDown(document.body);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("focuses the first menuitem on mount", () => {
    render(
      <TreeNodeContextMenu
        canCopyLink
        canCreateFolder
        canCreatePage
        canDelete
        canMove
        canRename
        node={treeNode({ can_have_children: true })}
        position={{ left: 0, top: 0 }}
        onAction={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(document.activeElement).toBe(
      screen.getByRole("menuitem", { name: "New page" }),
    );
  });

  it("moves focus with ArrowDown/ArrowUp/Home/End and wraps at boundaries", () => {
    render(
      <TreeNodeContextMenu
        canCopyLink
        canCreateFolder
        canCreatePage
        canDelete
        canMove
        canRename
        node={treeNode({ can_have_children: true })}
        position={{ left: 0, top: 0 }}
        onAction={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    const menu = screen.getByRole("menu");
    const items = screen.getAllByRole("menuitem");

    // first item focused on mount
    expect(document.activeElement).toBe(items[0]);

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);

    fireEvent.keyDown(menu, { key: "End" });
    expect(document.activeElement).toBe(items[items.length - 1]);

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    // wraps to first
    expect(document.activeElement).toBe(items[0]);

    fireEvent.keyDown(menu, { key: "ArrowUp" });
    // wraps to last
    expect(document.activeElement).toBe(items[items.length - 1]);

    fireEvent.keyDown(menu, { key: "Home" });
    expect(document.activeElement).toBe(items[0]);
  });

  it("closes when Tab is pressed inside the menu", () => {
    const onClose = vi.fn();

    render(
      <TreeNodeContextMenu
        canCopyLink
        canCreateFolder
        canCreatePage
        canDelete
        canMove
        canRename
        node={treeNode({ can_have_children: true })}
        position={{ left: 0, top: 0 }}
        onAction={vi.fn()}
        onClose={onClose}
      />,
    );

    fireEvent.keyDown(screen.getByRole("menu"), { key: "Tab" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("fits a full action menu inside a short viewport and keeps Delete reachable", async () => {
    const originalWidth = window.innerWidth;
    const originalHeight = window.innerHeight;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 240 });
    const rectSpy = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function getMenuBounds(this: HTMLElement) {
        if (this.classList.contains("tree-node-menu")) {
          return {
            bottom: 360,
            height: 360,
            left: 0,
            right: 200,
            top: 0,
            width: 200,
            x: 0,
            y: 0,
            toJSON: () => ({}),
          };
        }
        return new DOMRect();
      });

    try {
      render(
        <TreeNodeContextMenu
          canCopyLink
          canCreateFolder
          canCreatePage
          canDelete
          canMove
          canMoveDown
          canMoveUp
          canRename
          node={treeNode({ can_have_children: true })}
          position={{ left: 380, top: 230 }}
          onAction={vi.fn()}
          onClose={vi.fn()}
        />,
      );

      const menu = screen.getByRole("menu");
      await waitFor(() => {
        expect(menu).toHaveStyle({ left: "182px", top: "8px" });
      });
      fireEvent.keyDown(menu, { key: "End" });
      expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    } finally {
      rectSpy.mockRestore();
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: originalWidth,
      });
      Object.defineProperty(window, "innerHeight", {
        configurable: true,
        value: originalHeight,
      });
    }
  });

  it("renders at the document root so a clipped rail drawer cannot slice it", () => {
    // At mobile widths the rail sits in an off-canvas drawer that is
    // transformed and hides its overflow. A menu rendered inside that subtree
    // resolves its fixed position against the drawer instead of the viewport
    // and gets cut off at the drawer's trailing edge.
    const rail = document.createElement("div");
    rail.className = "tree-rail";
    document.body.appendChild(rail);

    render(
      <TreeNodeContextMenu
        canCopyLink
        canCreateFolder
        canCreatePage
        canDelete
        canMove
        canRename
        node={treeNode({ can_have_children: true })}
        position={{ left: 40, top: 56 }}
        onAction={vi.fn()}
        onClose={vi.fn()}
      />,
      { container: rail },
    );

    const menu = screen.getByRole("menu");
    expect(rail.contains(menu)).toBe(false);
    expect(menu.parentElement).toBe(document.body);
  });
});

function treeNode(overrides: Partial<TreeNodePayload>): TreeNodePayload {
  return {
    id: "node",
    section: "context",
    title: "Node",
    slug: null,
    node_kind: "folder",
    parent_id: null,
    sort_key: "0001",
    target_kind: null,
    target_id: null,
    is_system_node: false,
    can_have_children: true,
    primary_placement: true,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: [],
    ...overrides,
  };
}

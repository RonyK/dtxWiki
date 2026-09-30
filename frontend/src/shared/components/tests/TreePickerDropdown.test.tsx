import * as floating from "@floating-ui/dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TreeNodePayload } from "../../api/workspace";
import { Dialog } from "../Dialog";
import { TreePickerDropdown } from "../TreePickerDropdown";

vi.mock("@floating-ui/dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@floating-ui/dom")>();
  return { ...actual, computePosition: vi.fn(actual.computePosition) };
});

function makeNode(overrides: Partial<TreeNodePayload> & { id: string; title: string }): TreeNodePayload {
  return {
    id: overrides.id,
    section: overrides.section ?? "context",
    title: overrides.title,
    slug: overrides.slug ?? null,
    node_kind: overrides.node_kind ?? "folder",
    parent_id: overrides.parent_id ?? null,
    sort_key: overrides.sort_key ?? "a",
    target_kind: overrides.target_kind ?? null,
    target_id: overrides.target_id ?? null,
    is_system_node: overrides.is_system_node ?? false,
    can_have_children: overrides.can_have_children ?? true,
    primary_placement: overrides.primary_placement ?? true,
    visibility_status: overrides.visibility_status ?? "active",
    children_count: overrides.children_count ?? 0,
    breadcrumb: overrides.breadcrumb ?? [],
  };
}

const ROOT: TreeNodePayload = makeNode({ id: "root", title: "Root", parent_id: null });
const CHILD: TreeNodePayload = makeNode({
  id: "child",
  title: "Child",
  parent_id: "root",
  breadcrumb: [
    { id: "root", section: "context", title: "Root", slug: null, node_kind: "folder" },
  ],
});
const GRANDCHILD: TreeNodePayload = makeNode({
  id: "grandchild",
  title: "Grandchild",
  parent_id: "child",
  breadcrumb: [
    { id: "root", section: "context", title: "Root", slug: null, node_kind: "folder" },
    { id: "child", section: "context", title: "Child", slug: null, node_kind: "folder" },
  ],
});

describe("TreePickerDropdown", () => {
  it("renders an empty-label trigger when nothing is selected and no nodes exist", () => {
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[]}
        onChange={() => {}}
        selectedId=""
      />,
    );
    const trigger = screen.getByRole("button", { name: "Pick folder" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveTextContent("No folders available");
  });

  it("shows the empty-state copy inside the menu when there are no root nodes", async () => {
    const user = userEvent.setup();
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        emptyLabel="Nothing here"
        nodes={[]}
        onChange={() => {}}
        selectedId=""
      />,
    );
    await user.click(screen.getByRole("button", { name: "Pick folder" }));
    const tree = await screen.findByRole("tree", { name: "Pick folder tree" });
    expect(tree).toHaveTextContent("Nothing here");
  });

  it("shows the selected node's breadcrumb path in the trigger label", () => {
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT, CHILD, GRANDCHILD]}
        onChange={() => {}}
        selectedId="grandchild"
      />,
    );
    const trigger = screen.getByRole("button", { name: "Pick folder" });
    expect(trigger).toHaveTextContent("… / Child / Grandchild");
    expect(trigger).toHaveAttribute("title", "Root / Child / Grandchild");
    const descriptionId = trigger.getAttribute("aria-describedby");
    expect(descriptionId).toBeTruthy();
    expect(document.getElementById(descriptionId ?? "")).toHaveTextContent(
      "Root / Child / Grandchild",
    );
  });

  it("does not duplicate the selected node when the API breadcrumb includes itself", () => {
    const selfInclusiveChild = makeNode({
      id: "self-inclusive-child",
      title: "Child",
      parent_id: "root",
      breadcrumb: [
        { id: "root", section: "context", title: "Root", slug: null, node_kind: "folder" },
        {
          id: "self-inclusive-child",
          section: "context",
          title: "Child",
          slug: null,
          node_kind: "folder",
        },
      ],
    });
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT, selfInclusiveChild]}
        onChange={() => {}}
        selectedId="self-inclusive-child"
      />,
    );

    expect(
      screen.getByRole("button", { name: "Pick folder" }),
    ).toHaveTextContent("Root / Child");
  });

  it("opens the tree on trigger click and toggles aria-expanded", async () => {
    const user = userEvent.setup();
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT, CHILD]}
        onChange={() => {}}
        selectedId=""
      />,
    );
    const trigger = screen.getByRole("button", { name: "Pick folder" });
    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("tree", { name: "Pick folder tree" }),
    ).toBeInTheDocument();
  });

  it("expands and collapses from the named chevron control without selecting", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT, CHILD]}
        onChange={onChange}
        selectedId=""
      />,
    );
    const trigger = screen.getByRole("button", { name: "Pick folder" });
    await user.click(trigger);
    await screen.findByRole("tree", { name: "Pick folder tree" });

    // With no selected node, only root rows are visible until the user explores.
    expect(screen.queryByRole("treeitem", { name: /Child/ })).not.toBeInTheDocument();

    const expand = screen.getByRole("button", { name: "Expand Root" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(expand).toHaveAttribute("data-tree-toggle", "root");
    expect(expand).toHaveAttribute("tabindex", "-1");

    // Reproduce the native pointerdown -> click sequence that reaches a
    // portaled menu before its row click handler.
    fireEvent.pointerDown(expand);
    fireEvent.click(expand);

    expect(screen.getByRole("treeitem", { name: /Child/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse Root" }))
      .toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(onChange).not.toHaveBeenCalled();

    const collapse = screen.getByRole("button", { name: "Collapse Root" });
    fireEvent.pointerDown(collapse);
    fireEvent.click(collapse);

    expect(screen.queryByRole("treeitem", { name: /Child/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand Root" }))
      .toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("reports the portaled menu element while it is mounted", async () => {
    const user = userEvent.setup();
    const onMenuElementChange = vi.fn();
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT]}
        onChange={() => {}}
        onMenuElementChange={onMenuElementChange}
        selectedId=""
      />,
    );
    const trigger = screen.getByRole("button", { name: "Pick folder" });

    await user.click(trigger);
    const tree = await screen.findByRole("tree", { name: "Pick folder tree" });
    const menu = tree.closest(".tree-picker-menu");
    expect(menu).toBeInstanceOf(HTMLDivElement);
    expect(onMenuElementChange).toHaveBeenLastCalledWith(menu);

    await user.click(trigger);
    expect(onMenuElementChange).toHaveBeenLastCalledWith(null);
  });

  it("handles tree navigation once after clicking a native chevron", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT, CHILD]}
        onChange={onChange}
        selectedId=""
      />,
    );
    await user.click(screen.getByRole("button", { name: "Pick folder" }));
    const toggle = await screen.findByRole("button", { name: "Expand Root" });
    await user.click(toggle);
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    await user.keyboard("{ArrowLeft}");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    const tree = screen.getByRole("tree", { name: "Pick folder tree" });
    expect(tree).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(onChange).not.toHaveBeenCalled();

    await user.keyboard("{ArrowDown}{Enter}");
    expect(onChange).toHaveBeenCalledExactlyOnceWith("child");
  });

  it("focuses only after placement is visible, without stealing focus on reposition", async () => {
    type Position = Awaited<ReturnType<typeof floating.computePosition>>;
    const pending: Array<(position: Position) => void> = [];
    const positionMock = vi.mocked(floating.computePosition).mockImplementation(
      () => new Promise<Position>((resolve) => pending.push(resolve)),
    );
    async function finishPlacement() {
      await act(async () => {
        pending.splice(0).forEach((resolve) => resolve({
          x: 12, y: 40, placement: "bottom-start", strategy: "fixed", middlewareData: {},
        }));
      });
    }
    try {
      const user = userEvent.setup();
      render(
        <TreePickerDropdown
          ariaLabel="Pick folder"
          nodes={[ROOT, CHILD]}
          onChange={() => {}}
          selectedId=""
        />,
      );
      const trigger = screen.getByRole("button", { name: "Pick folder" });
      await user.click(trigger);
      const search = screen.getByLabelText("Search target parents");
      expect(search.closest(".tree-picker-menu")).toHaveStyle({ visibility: "hidden" });
      expect(search).not.toHaveFocus();
      expect(pending.length).toBeGreaterThan(0);

      await finishPlacement();
      expect(search.closest(".tree-picker-menu")).toHaveStyle({ visibility: "visible" });
      expect(search).toHaveFocus();
      await user.keyboard("{ArrowDown}");
      const tree = screen.getByRole("tree", { name: "Pick folder tree" });
      expect(tree).toHaveFocus();
      fireEvent(window, new Event("resize"));
      await waitFor(() => expect(pending.length).toBeGreaterThan(0));
      await finishPlacement();
      expect(tree).toHaveFocus();

      await user.keyboard("{Escape}");
      await user.click(trigger);
      expect(screen.getByLabelText("Search target parents")).not.toHaveFocus();
      await finishPlacement();
      expect(screen.getByRole("searchbox", { name: "Search target parents" })).toHaveFocus();
    } finally {
      positionMock.mockRestore();
    }
  });

  it("auto-expands ancestors of the selected node based on its breadcrumb", async () => {
    const user = userEvent.setup();
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT, CHILD, GRANDCHILD]}
        onChange={() => {}}
        selectedId="grandchild"
      />,
    );
    await user.click(screen.getByRole("button", { name: "Pick folder" }));

    // All three rows should be visible because root and child are auto-expanded.
    expect(screen.getByRole("treeitem", { name: /Root/ })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /Child/ })).toBeInTheDocument();
    const grandchild = screen.getByRole("treeitem", { name: /Grandchild/ });
    expect(grandchild).toHaveAttribute("aria-selected", "true");
  });

  it("does not auto-expand unrelated branches when a node is selected", async () => {
    const user = userEvent.setup();
    const siblingRoot = makeNode({ id: "sibling-root", title: "Sibling Root" });
    const siblingChild = makeNode({
      id: "sibling-child",
      title: "Sibling Child",
      parent_id: "sibling-root",
      breadcrumb: [
        {
          id: "sibling-root",
          section: "context",
          title: "Sibling Root",
          slug: null,
          node_kind: "folder",
        },
      ],
    });
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT, CHILD, GRANDCHILD, siblingRoot, siblingChild]}
        onChange={() => {}}
        selectedId="grandchild"
      />,
    );
    await user.click(screen.getByRole("button", { name: "Pick folder" }));

    expect(screen.getByRole("treeitem", { name: /Grandchild/ })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /Sibling Root/ })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Sibling Child/ })).not.toBeInTheDocument();
  });

  it("preserves manual expansion across node refetches for the same selection", async () => {
    const user = userEvent.setup();
    const siblingRoot = makeNode({ id: "sibling-root", title: "Sibling Root" });
    const siblingChild = makeNode({
      id: "sibling-child",
      title: "Sibling Child",
      parent_id: "sibling-root",
      breadcrumb: [
        {
          id: "sibling-root",
          section: "context",
          title: "Sibling Root",
          slug: null,
          node_kind: "folder",
        },
      ],
    });
    const nodes = [ROOT, CHILD, GRANDCHILD, siblingRoot, siblingChild];
    const { rerender } = render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={nodes}
        onChange={() => {}}
        selectedId="grandchild"
      />,
    );
    await user.click(screen.getByRole("button", { name: "Pick folder" }));
    const sibling = screen.getByRole("treeitem", { name: /Sibling Root/ });
    await user.click(
      sibling.querySelector('[data-tree-toggle="sibling-root"]') as Element,
    );

    expect(screen.getByRole("treeitem", { name: /Sibling Child/ })).toBeInTheDocument();

    rerender(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[...nodes]}
        onChange={() => {}}
        selectedId="grandchild"
      />,
    );

    expect(screen.getByRole("treeitem", { name: /Sibling Child/ })).toBeInTheDocument();
  });

  it("invokes onChange with the picked node id and closes the menu", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT, CHILD]}
        onChange={onChange}
        selectedId=""
      />,
    );
    const trigger = screen.getByRole("button", { name: "Pick folder" });
    await user.click(trigger);
    const root = screen.getByRole("treeitem", { name: /Root/ });
    await user.click(root.querySelector('[data-tree-toggle="root"]') as Element);
    await user.click(screen.getByRole("treeitem", { name: /Child/ }));

    expect(onChange).toHaveBeenCalledWith("child");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByRole("tree", { name: "Pick folder tree" }),
    ).not.toBeInTheDocument();
  });

  it("supports keyboard expansion and selection", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT, CHILD]}
        onChange={onChange}
        selectedId=""
      />,
    );
    const trigger = screen.getByRole("button", { name: "Pick folder" });
    await user.click(trigger);

    expect(screen.getByRole("searchbox", { name: "Search target parents" })).toHaveFocus();
    const tree = screen.getByRole("tree", { name: "Pick folder tree" });
    expect(tree.getAttribute("aria-activedescendant")).toContain(
      "tree-picker-option-root",
    );

    await user.keyboard("{ArrowDown}{ArrowRight}{ArrowDown}{Enter}");

    expect(onChange).toHaveBeenCalledWith("child");
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("closes the menu with Escape from keyboard navigation", async () => {
    const user = userEvent.setup();
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        nodes={[ROOT]}
        onChange={() => {}}
        selectedId=""
      />,
    );
    const trigger = screen.getByRole("button", { name: "Pick folder" });
    await user.click(trigger);

    await user.keyboard("{Escape}");

    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("closes only itself on the first Escape when nested in a dialog", async () => {
    const user = userEvent.setup();
    const onDialogClose = vi.fn();
    render(
      <Dialog labelledBy="move-title" onClose={onDialogClose}>
        <h2 id="move-title">Move item</h2>
        <TreePickerDropdown
          ariaLabel="Target parent"
          nodes={[ROOT]}
          onChange={() => {}}
          selectedId=""
        />
      </Dialog>,
    );
    const trigger = screen.getByRole("button", { name: "Target parent" });
    await user.click(trigger);

    await user.keyboard("{Escape}");

    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(onDialogClose).not.toHaveBeenCalled();

    await user.keyboard("{Escape}");
    expect(onDialogClose).toHaveBeenCalledTimes(1);
  });

  it("closes the menu on outside pointerdown", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <button type="button">Outside</button>
        <TreePickerDropdown
          ariaLabel="Pick folder"
          nodes={[ROOT]}
          onChange={() => {}}
          selectedId=""
        />
      </div>,
    );
    const trigger = screen.getByRole("button", { name: "Pick folder" });
    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    // Simulate a pointerdown outside the picker container.
    fireEvent.pointerDown(screen.getByRole("button", { name: "Outside" }));
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("disables the trigger when disabled is true", () => {
    render(
      <TreePickerDropdown
        ariaLabel="Pick folder"
        disabled
        nodes={[ROOT]}
        onChange={() => {}}
        selectedId=""
      />,
    );
    expect(screen.getByRole("button", { name: "Pick folder" })).toBeDisabled();
  });
});

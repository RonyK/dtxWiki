import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TreeNodePayload } from "../../api/workspace";
import { i18n } from "../../i18n";
import { DeleteTreeNodeDialog, MoveTreeNodeDialog } from "../TreeNodeActionDialogs";

describe("MoveTreeNodeDialog", () => {
  it("blocks submitting a move whose destination is the node's current parent", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const currentParent = treeNode({
      id: "folder-current",
      title: "Current Folder",
      node_kind: "folder",
      can_have_children: true,
      children_count: 1,
      breadcrumb: [
        treeBreadcrumb("Context", "context"),
        treeBreadcrumb("Programs", "programs"),
        treeBreadcrumb("Projects", "projects"),
      ],
    });
    const otherParent = treeNode({
      id: "folder-other",
      title: "Other Folder",
      node_kind: "folder",
      can_have_children: true,
    });
    const node = treeNode({
      id: "page-node",
      title: "Moving Page",
      parent_id: currentParent.id,
      target_kind: "context_page",
      target_id: "page-1",
    });

    render(
      <MoveTreeNodeDialog
        node={node}
        targets={[currentParent, otherParent]}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(screen.getByRole("textbox", { name: /Reason/ }), "Reorganize");

    const targetParent = screen.getByRole("button", { name: "Target parent" });
    expect(targetParent).toHaveTextContent("… / Projects / Current Folder");
    expect(targetParent).toHaveAttribute(
      "title",
      "Context / Programs / Projects / Current Folder",
    );

    // The dialog preselects the node's current parent; submitting that would
    // silently reassign the sort key on the backend, so it must stay disabled.
    expect(screen.getByRole("button", { name: "Move" })).toBeDisabled();
    expect(
      screen.getByText("Already under this parent. Pick a different parent to move."),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Target parent" }));
    await userEvent.click(screen.getByRole("treeitem", { name: /Other Folder/ }));

    expect(
      screen.queryByText("Already under this parent. Pick a different parent to move."),
    ).toBeNull();
    const moveButton = screen.getByRole("button", { name: "Move" });
    expect(moveButton).toBeEnabled();

    await userEvent.click(moveButton);

    expect(onSubmit).toHaveBeenCalledWith("folder-other", "Reorganize");
  });

  it("keeps the move enabled when the current parent is not a listed target", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const target = treeNode({
      id: "folder-target",
      title: "Target Folder",
      node_kind: "folder",
      can_have_children: true,
    });
    const node = treeNode({
      id: "page-node",
      title: "Moving Page",
      parent_id: "hidden-system-root",
      target_kind: "context_page",
      target_id: "page-1",
    });

    render(
      <MoveTreeNodeDialog
        node={node}
        targets={[target]}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    const moveButton = screen.getByRole("button", { name: "Move" });
    expect(moveButton).toBeEnabled();

    await userEvent.click(moveButton);

    expect(onSubmit).toHaveBeenCalledWith("folder-target", "");
    expect(screen.getByRole("textbox", { name: "Reason (optional)" })).toBeInTheDocument();
  });

  it("keeps the Move dialog open while expanding a target branch", async () => {
    const user = userEvent.setup();
    const targetParent = treeNode({
      id: "target-parent",
      title: "Destination folder",
      node_kind: "folder",
      can_have_children: true,
      children_count: 1,
    });
    const targetChild = treeNode({
      id: "target-child",
      title: "Destination child",
      node_kind: "folder",
      parent_id: targetParent.id,
      breadcrumb: [treeBreadcrumb(targetParent.title, targetParent.id)],
    });
    const node = treeNode({
      id: "moving-node",
      title: "Moving page",
      target_id: "page-1",
      target_kind: "context_page",
    });
    const onClose = vi.fn();

    render(
      <MoveTreeNodeDialog
        node={node}
        targets={[targetParent, targetChild]}
        onClose={onClose}
        onSubmit={vi.fn()}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Move Moving page" });
    await user.click(screen.getByRole("button", { name: "Target parent" }));
    const tree = screen.getByRole("tree", { name: "Target parent tree" });
    const parentRow = within(tree).getByRole("treeitem", { name: /Destination folder/ });
    const expand = within(parentRow).getByRole("button", {
      name: "Expand Destination folder",
    });

    await user.click(expand);

    expect(dialog).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(within(tree).getByRole("treeitem", { name: /Destination child/ })).toBeInTheDocument();
    expect(expand).toHaveAttribute("aria-expanded", "true");
  });
});

describe("DeleteTreeNodeDialog", () => {
  it("localizes the destructive confirm flow, including the child-branch warning banner", async () => {
    await i18n.changeLanguage("ko");
    try {
      const node = treeNode({
        id: "folder-with-children",
        title: "Programs",
        node_kind: "folder",
        can_have_children: true,
        children_count: 2,
      });

      render(
        <DeleteTreeNodeDialog node={node} onClose={() => undefined} onSubmit={vi.fn()} />,
      );

      // I18N-02: this component previously had no useTranslation at all, so
      // every string here — including the destructive confirm button —
      // fell through to the hardcoded English literal.
      const confirmButton = screen.getByRole("button", { name: "삭제" });
      expect(confirmButton).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();

      // The child-branch warning banner must actually render (a missing
      // key would silently produce an empty banner, not a visible failure).
      const banner = screen.getByText("아래 하위 분기 전체가 함께 보관 처리됩니다.");
      expect(banner).toBeInTheDocument();
      expect(screen.queryByText("Child nodes in this branch are removed too.")).toBeNull();
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("focuses a remaining row when deleting the node removes its action opener", async () => {
    function Harness() {
      const [dialogOpen, setDialogOpen] = useState(false);
      const [nodePresent, setNodePresent] = useState(true);
      const node = treeNode({ id: "deleted-node", title: "Deleted node" });
      return (
        <div className="tree-rail">
          <div aria-label="Nodes" role="tree">
            {nodePresent ? (
              <div
                aria-selected="false"
                data-node-id={node.id}
                role="treeitem"
                tabIndex={0}
              >
                <button
                  className="tree-row-menu-button"
                  onClick={() => setDialogOpen(true)}
                  type="button"
                >
                  Open delete dialog
                </button>
              </div>
            ) : (
              <div
                aria-selected="false"
                data-node-id="remaining-node"
                role="treeitem"
                tabIndex={0}
              >
                Remaining node
              </div>
            )}
          </div>
          {dialogOpen ? (
            <DeleteTreeNodeDialog
              node={node}
              onClose={() => setDialogOpen(false)}
              onSubmit={async () => {
                setNodePresent(false);
                setDialogOpen(false);
              }}
            />
          ) : null}
        </div>
      );
    }

    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open delete dialog" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("treeitem", { name: "Remaining node" })).toHaveFocus(),
    );
  });

  it("does not restore focus into a replacement route with the same node id", async () => {
    function Harness() {
      const [dialogOpen, setDialogOpen] = useState(false);
      const [route, setRoute] = useState<"tree" | "replacement">("tree");
      const node = treeNode({ id: "shared-node-id", title: "Route node" });
      if (route === "replacement") {
        return (
          <div className="tree-rail" key="replacement-route">
            <div aria-label="Replacement nodes" role="tree">
              <div
                aria-selected="false"
                data-node-id={node.id}
                role="treeitem"
                tabIndex={0}
              >
                <button className="tree-row-menu-button" type="button">
                  Replacement route action
                </button>
              </div>
            </div>
          </div>
        );
      }
      return (
        <div className="tree-rail" key="original-route">
          <div aria-label="Original nodes" role="tree">
            <div
              aria-selected="false"
              data-node-id={node.id}
              role="treeitem"
              tabIndex={0}
            >
              <button
                className="tree-row-menu-button"
                onClick={() => setDialogOpen(true)}
                type="button"
              >
                Open route delete dialog
              </button>
            </div>
          </div>
          {dialogOpen ? (
            <DeleteTreeNodeDialog
              node={node}
              onClose={() => setDialogOpen(false)}
              onSubmit={async () => {
                setRoute("replacement");
                setDialogOpen(false);
              }}
            />
          ) : null}
        </div>
      );
    }

    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open route delete dialog" }));

    await user.click(screen.getByRole("button", { name: "Delete" }));

    const replacementAction = await screen.findByRole("button", {
      name: "Replacement route action",
    });
    expect(replacementAction).not.toHaveFocus();
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

function treeBreadcrumb(title: string, id: string) {
  return {
    id,
    section: "context" as const,
    title,
    slug: id,
    node_kind: "folder" as const,
  };
}

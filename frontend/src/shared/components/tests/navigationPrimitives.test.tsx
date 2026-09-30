import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TreeNodePayload } from "../../api/workspace";
import { Breadcrumbs } from "../Breadcrumbs";
import { StatusPill } from "../StatusPill";
import { TreeRail } from "../TreeRail";
import {
  resolveExpandedNodeIds,
  updateTreeSearchParams,
  writeExpandedNodeIdsToStorage,
} from "../treeRouteState";

describe("navigation primitives", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("renders breadcrumbs with a current page", () => {
    render(
      <MemoryRouter>
        <Breadcrumbs
          items={[
            { label: "Context", href: "/context" },
            { label: "Decision log" },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Context" })).toHaveAttribute(
      "href",
      "/context",
    );
    expect(screen.getByText("Decision log")).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("maps lifecycle status to a status pill", () => {
    render(<StatusPill status="latest_draft" />);

    expect(screen.getByText("latest draft")).toHaveClass("status-pill-warn");
  });

  it("renders tree nodes with selected state", () => {
    render(
      <TreeRail
        title="Context"
        activeTargetId="page-1"
        nodes={[
          {
            id: "node-1",
            section: "context",
            title: "Inbox",
            slug: "inbox",
            node_kind: "folder",
            parent_id: null,
            sort_key: "0001",
            target_kind: null,
            target_id: null,
            is_system_node: true,
            can_have_children: true,
            primary_placement: true,
            visibility_status: "visible",
            children_count: 1,
            breadcrumb: [],
          },
          {
            id: "node-2",
            section: "context",
            title: "Decision log",
            slug: null,
            node_kind: "page_ref",
            parent_id: "node-1",
            sort_key: "0002",
            target_kind: "context_page",
            target_id: "page-1",
            is_system_node: false,
            can_have_children: false,
            primary_placement: true,
            visibility_status: "visible",
            children_count: 0,
            breadcrumb: [
              {
                id: "node-1",
                section: "context",
                title: "Inbox",
                slug: "inbox",
                node_kind: "folder",
              },
            ],
          },
        ]}
      />,
    );

    expect(screen.getByRole("treeitem", { name: "Decision log" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("selects target tree nodes", async () => {
    const onSelectTarget = vi.fn();
    render(
      <TreeRail
        title="Context"
        activeTargetId={null}
        onSelectTarget={onSelectTarget}
        nodes={[
          {
            id: "node-1",
            section: "context",
            title: "Decision log",
            slug: null,
            node_kind: "page_ref",
            parent_id: null,
            sort_key: "0001",
            target_kind: "context_page",
            target_id: "page-1",
            is_system_node: false,
            can_have_children: false,
            primary_placement: true,
            visibility_status: "visible",
            children_count: 0,
            breadcrumb: [],
          },
        ]}
      />,
    );

    await userEvent.click(screen.getByRole("treeitem", { name: "Decision log" }));

    expect(onSelectTarget).toHaveBeenCalledWith(
      expect.objectContaining({ target_id: "page-1" }),
    );
  });

  it("collapses expanded tree branches", async () => {
    function TreeHarness() {
      const [expandedNodeIds, setExpandedNodeIds] = useState(["node-1"]);
      return (
        <TreeRail
          title="Context"
          activeTargetId={null}
          expandedNodeIds={expandedNodeIds}
          onExpandedNodeIdsChange={setExpandedNodeIds}
          nodes={[
            {
              id: "node-1",
              section: "context",
              title: "Inbox",
              slug: "inbox",
              node_kind: "folder",
              parent_id: null,
              sort_key: "0001",
              target_kind: null,
              target_id: null,
              is_system_node: true,
              can_have_children: true,
              primary_placement: true,
              visibility_status: "visible",
              children_count: 1,
              breadcrumb: [],
            },
            {
              id: "node-2",
              section: "context",
              title: "Decision log",
              slug: null,
              node_kind: "page_ref",
              parent_id: "node-1",
              sort_key: "0002",
              target_kind: "context_page",
              target_id: "page-1",
              is_system_node: false,
              can_have_children: false,
              primary_placement: true,
              visibility_status: "visible",
              children_count: 0,
              breadcrumb: [
                {
                  id: "node-1",
                  section: "context",
                  title: "Inbox",
                  slug: "inbox",
                  node_kind: "folder",
                },
              ],
            },
          ]}
        />
      );
    }

    render(<TreeHarness />);

    expect(screen.getByRole("treeitem", { name: "Inbox" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByRole("treeitem", { name: "Decision log" }))
      .toBeInTheDocument();

    await userEvent.click(screen.getByRole("treeitem", { name: "Inbox" }));

    expect(screen.getByRole("treeitem", { name: "Inbox" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("treeitem", { name: "Decision log" })).toBeNull();
  });

  it("resolves tree query state while preserving active ancestors by default", () => {
    const nodes = [
      {
        id: "node-1",
        section: "context",
        title: "Inbox",
        slug: "inbox",
        node_kind: "folder",
        parent_id: null,
        sort_key: "0001",
        target_kind: null,
        target_id: null,
        is_system_node: true,
        can_have_children: true,
        primary_placement: true,
        visibility_status: "visible",
        children_count: 1,
        breadcrumb: [],
      },
      {
        id: "node-2",
        section: "context",
        title: "Decision log",
        slug: null,
        node_kind: "page_ref",
        parent_id: "node-1",
        sort_key: "0002",
        target_kind: "context_page",
        target_id: "page-1",
        is_system_node: false,
        can_have_children: false,
        primary_placement: true,
        visibility_status: "visible",
        children_count: 0,
        breadcrumb: [],
      },
    ] satisfies TreeNodePayload[];
    const expanded = resolveExpandedNodeIds(nodes, null, "page-1");
    const collapsed = resolveExpandedNodeIds(nodes, "", "page-1");
    const params = updateTreeSearchParams(new URLSearchParams("view=context"), {
      targetId: "page-1",
      expandedNodeIds: collapsed,
    });

    expect(expanded).toEqual(["node-1"]);
    expect(collapsed).toEqual(["node-1"]);
    expect(params.toString()).toBe("view=context&target=page-1&expanded=node-1");
  });

  it("unions the selected ancestor into an otherwise collapsed default", () => {
    const nodes = [
      treeNode({
        id: "node-1",
        title: "Inbox",
        node_kind: "folder",
        can_have_children: true,
        children_count: 1,
      }),
      treeNode({
        id: "node-2",
        title: "Decision log",
        parent_id: "node-1",
        target_kind: "context_page",
        target_id: "page-1",
      }),
    ];

    expect(
      resolveExpandedNodeIds(nodes, null, "page-1", {
        defaultMode: "none",
        storageKey: "context",
      }),
    ).toEqual(["node-1"]);
  });

  it.each([
    ["empty URL", "", undefined, ["node-1", "node-2"]],
    ["partial URL", "node-1", undefined, ["node-1", "node-2"]],
    ["empty stored", null, [], ["node-1", "node-2"]],
  ])("unions required ancestors for %s and keeps unrelated branches closed", (_label, encoded, stored, expected) => {
    const nodes = [
      treeNode({ id: "node-1", node_kind: "folder", can_have_children: true, children_count: 1 }),
      treeNode({ id: "node-2", parent_id: "node-1", node_kind: "folder", can_have_children: true, children_count: 1 }),
      treeNode({ id: "node-3", parent_id: "node-2", target_kind: "context_page", target_id: "page-1" }),
      treeNode({ id: "unrelated", node_kind: "folder", can_have_children: true, children_count: 1 }),
    ];
    if (stored) {
      writeExpandedNodeIdsToStorage("context", stored);
    }
    expect(resolveExpandedNodeIds(nodes, encoded, "node-3", { defaultMode: "none", storageKey: "context" })).toEqual(expected);
  });

  it("restores client-only tree expansion state from localStorage", () => {
    const nodes = [
      treeNode({
        id: "node-1",
        title: "Inbox",
        node_kind: "folder",
        can_have_children: true,
        children_count: 1,
      }),
      treeNode({
        id: "node-2",
        title: "Decision log",
        parent_id: "node-1",
        target_kind: "context_page",
        target_id: "page-1",
      }),
    ];

    writeExpandedNodeIdsToStorage("context", ["node-1", "missing", "node-1"]);

    expect(
      resolveExpandedNodeIds(nodes, null, "page-1", {
        defaultMode: "none",
        storageKey: "context",
      }),
    ).toEqual(["node-1"]);
  });

  it("keeps stored tree expansion state separate per workspace", () => {
    const nodes = [
      treeNode({
        id: "node-1",
        title: "Inbox",
        node_kind: "folder",
        can_have_children: true,
        children_count: 1,
      }),
      treeNode({
        id: "node-2",
        title: "Decision log",
        parent_id: "node-1",
        target_kind: "context_page",
        target_id: "page-1",
      }),
    ];

    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-a");
    writeExpandedNodeIdsToStorage("context", ["node-1"]);

    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-b");
    writeExpandedNodeIdsToStorage("context", ["workspace-b-node"]);

    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-a");
    expect(
      resolveExpandedNodeIds(nodes, null, "page-1", {
        defaultMode: "none",
        storageKey: "context",
      }),
    ).toEqual(["node-1"]);
  });

  it("ignores stored expansion ids that all belong to another tree", () => {
    const nodes = [
      treeNode({
        id: "node-1",
        title: "Inbox",
        node_kind: "folder",
        can_have_children: true,
        children_count: 1,
      }),
      treeNode({
        id: "node-2",
        title: "Decision log",
        parent_id: "node-1",
        target_kind: "context_page",
        target_id: "page-1",
      }),
    ];

    writeExpandedNodeIdsToStorage("context", ["other-workspace-node"]);

    expect(
      resolveExpandedNodeIds(nodes, null, "page-1", {
        defaultMode: "active-path",
        storageKey: "context",
      }),
    ).toEqual(["node-1"]);
  });

  it("falls back to legacy unscoped expansion entries after selecting a workspace", () => {
    const nodes = [
      treeNode({
        id: "node-1",
        title: "Inbox",
        node_kind: "folder",
        can_have_children: true,
        children_count: 1,
      }),
      treeNode({
        id: "node-2",
        title: "Decision log",
        parent_id: "node-1",
        target_kind: "context_page",
        target_id: "page-1",
      }),
    ];

    window.localStorage.setItem(
      "dtxwiki:tree-expanded:context",
      JSON.stringify(["node-1"]),
    );
    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-a");

    expect(
      resolveExpandedNodeIds(nodes, null, "page-1", {
        defaultMode: "none",
        storageKey: "context",
      }),
    ).toEqual(["node-1"]);
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

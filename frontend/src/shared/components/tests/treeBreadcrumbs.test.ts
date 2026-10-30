import { describe, expect, it } from "vitest";

import type { TreeNodePayload } from "../../api/workspace";
import { targetBreadcrumbs } from "../treeBreadcrumbs";

describe("targetBreadcrumbs", () => {
  it("omits a renamed canonical system root by identity, not localized title", () => {
    const root = treeNode("root", "Renamed storage root", {
      slug: "context",
      node_kind: "folder",
      is_system_node: true,
      can_have_children: true,
      target_id: null,
      target_kind: null,
    });
    const folder = treeNode("folder", "Context", {
      slug: "folder",
      node_kind: "folder",
      parent_id: root.id,
      breadcrumb: [ancestor(root.id, root.title)],
    });
    const page = treeNode("page-node", "Child", {
      parent_id: folder.id,
      target_id: "page-id",
      target_kind: "context_page",
      breadcrumb: [
        ancestor(root.id, root.title),
        ancestor(folder.id, folder.title),
      ],
    });
    expect(
      targetBreadcrumbs({ workspaceId: "11111111-1111-4111-8111-111111111111",
        basePath: "/context",
        rootLabel: "Localized Context",
        nodes: [root, folder, page],
        targetNodeId: page.id,
      }).map((item) => item.label),
    ).toEqual(["Localized Context", "Context", "Child"]);
  });
  it("links root and ancestor path segments to their tree targets", () => {
    const items = targetBreadcrumbs({ workspaceId: "11111111-1111-4111-8111-111111111111",
      basePath: "/context",
      rootLabel: "Context",
      nodes: [
        treeNode("root-node", "Context", {
          slug: "context",
          node_kind: "folder",
          target_id: null,
          target_kind: null,
          is_system_node: true,
          can_have_children: true,
        }),
        treeNode("page-node", "Runbook", {
          target_id: "page-1",
          breadcrumb: [
            ancestor("root-node", "Context"),
            ancestor("folder-node", "Operations"),
          ],
        }),
      ],
      targetId: "page-1",
    });

    expect(items).toEqual([
      { label: "Context", href: "/workspaces/11111111-1111-4111-8111-111111111111/context" },
      { label: "Operations", href: "/workspaces/11111111-1111-4111-8111-111111111111/context/folders/folder-node" },
      { label: "Runbook" },
    ]);
  });

  it("does not repeat the selected node when the API breadcrumb includes itself", () => {
    const page = treeNode("page-node", "Runbook", {
      target_id: "page-1",
      breadcrumb: [
        ancestor("folder-node", "Operations"),
        ancestor("page-node", "Runbook"),
      ],
    });

    expect(
      targetBreadcrumbs({ workspaceId: "11111111-1111-4111-8111-111111111111",
        basePath: "/context",
        rootLabel: "Context",
        nodes: [page],
        targetNodeId: page.id,
      }),
    ).toEqual([
      { label: "Context", href: "/workspaces/11111111-1111-4111-8111-111111111111/context" },
      { label: "Operations", href: "/workspaces/11111111-1111-4111-8111-111111111111/context/folders/folder-node" },
      { label: "Runbook" },
    ]);
  });

  it("can build breadcrumbs for selected folder nodes", () => {
    const items = targetBreadcrumbs({ workspaceId: "11111111-1111-4111-8111-111111111111",
      basePath: "/agent",
      rootLabel: "Agent",
      nodes: [
        treeNode("root-node", "Agent", {
          section: "agent",
          slug: "agent",
          node_kind: "folder",
          target_id: null,
          target_kind: null,
          is_system_node: true,
          can_have_children: true,
        }),
        treeNode("folder-node", "Persona", {
          section: "agent",
          node_kind: "folder",
          target_kind: null,
          target_id: null,
          breadcrumb: [ancestor("root-node", "Agent")],
        }),
      ],
      targetNodeId: "folder-node",
    });

    expect(items).toEqual([
      { label: "Agent", href: "/workspaces/11111111-1111-4111-8111-111111111111/agent" },
      { label: "Persona" },
    ]);
  });

  // P10.1: an explicit workspace id switches an ANCESTOR that is itself a
  // Context page to the canonical permalink — a folder ancestor has no page
  // id to canonicalize and always keeps the query-param tree-target link.
  it("links a Context-page ancestor to its canonical permalink when a workspace id is supplied", () => {
    const ancestorPageId = "44444444-4444-4444-8444-444444444444";
    const pageAncestor = treeNode("ancestor-page-node", "Runbook Index", {
      target_id: ancestorPageId,
      target_kind: "context_page",
    });
    const folderAncestor = treeNode("folder-node", "Operations", {
      node_kind: "folder",
      target_id: null,
      target_kind: null,
    });
    const page = treeNode("page-node", "Runbook", {
      target_id: "page-1",
      breadcrumb: [
        ancestor(folderAncestor.id, folderAncestor.title),
        ancestor(pageAncestor.id, pageAncestor.title),
      ],
    });

    expect(
      targetBreadcrumbs({
        basePath: "/context",
        workspaceId: "11111111-1111-4111-8111-111111111111",
        rootLabel: "Context",
        nodes: [folderAncestor, pageAncestor, page],
        targetNodeId: page.id,
      }),
    ).toEqual([
      { label: "Context", href: "/workspaces/11111111-1111-4111-8111-111111111111/context" },
      { label: "Operations", href: "/workspaces/11111111-1111-4111-8111-111111111111/context/folders/folder-node" },
      {
        label: "Runbook Index",
        href: `/workspaces/11111111-1111-4111-8111-111111111111/context/pages/${ancestorPageId}`,
      },
      { label: "Runbook" },
    ]);
  });

  it("omits a legacy Content root from Korean Context breadcrumbs", () => {
    const root = treeNode("content-root", "Content", {
      slug: "content",
      node_kind: "folder",
      is_system_node: true,
      can_have_children: true,
      target_id: null,
      target_kind: null,
    });
    const page = treeNode("page-node-ko", "한국어 문서", {
      target_id: "page-ko",
      breadcrumb: [ancestor(root.id, root.title)],
    });

    expect(
      targetBreadcrumbs({ workspaceId: "11111111-1111-4111-8111-111111111111",
        basePath: "/context",
        rootLabel: "컨텍스트",
        nodes: [root, page],
        targetNodeId: page.id,
      }).map((item) => item.label),
    ).toEqual(["컨텍스트", "한국어 문서"]);
  });
});

function treeNode(
  id: string,
  title: string,
  overrides: Partial<TreeNodePayload> = {},
): TreeNodePayload {
  return {
    id,
    section: "context",
    title,
    slug: null,
    node_kind: "page_ref",
    parent_id: null,
    sort_key: "0001",
    target_kind: "context_page",
    target_id: id,
    is_system_node: false,
    can_have_children: false,
    primary_placement: true,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: [],
    ...overrides,
  };
}

function ancestor(id: string, title: string) {
  return {
    id,
    section: "context",
    title,
    slug: title.toLowerCase(),
    node_kind: "folder",
  };
}

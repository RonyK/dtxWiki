import { treeNodeHref, workspacePath, workspaceTargetHref } from "../lib/workspaceRoutes";
import type { TreeNodePayload } from "../api/workspace";
import type { BreadcrumbItem } from "./Breadcrumbs";
import { isSemanticSectionRoot } from "./treeSectionRoot";

export function targetBreadcrumbs({
  basePath,
  workspaceId,
  rootLabel,
  nodes,
  targetId,
  targetNodeId,
  fallbackLabel,
  childLabel,
}: {
  basePath?: string;
  workspaceId: string;
  rootLabel: string;
  nodes: TreeNodePayload[];
  targetId?: string | null;
  targetNodeId?: string | null;
  fallbackLabel?: string | null;
  childLabel?: string;
}): BreadcrumbItem[] {
  // The first crumb links to the section dashboard. Folder crumbs use their
  // own tree node IDs, and page crumbs use their document IDs.
  const rootHref = basePath && workspaceId
    ? (basePath.startsWith("/workspaces/") ? basePath : workspacePath(workspaceId, basePath))
    : undefined;
  const items: BreadcrumbItem[] = [{ label: rootLabel, href: rootHref }];
  const targetNode = targetNodeId
    ? nodes.find((node) => node.id === targetNodeId)
    : targetId
      ? nodes.find((node) => node.target_id === targetId)
      : null;
  for (const ancestor of targetNode?.breadcrumb ?? []) {
    // The API can include the selected node as the final entry. Add it once
    // below so its role as the current item or an ancestor is explicit.
    if (ancestor.id === targetNode?.id) {
      continue;
    }
    const ancestorNode = nodes.find((node) => node.id === ancestor.id);
    if (ancestorNode && isSemanticSectionRoot(ancestorNode, rootLabel)) {
      continue;
    }
    items.push({
      label: ancestor.title,
      href: ancestorNode ? treeNodeHref(workspaceId, ancestorNode) ?? undefined
        : ancestor.node_kind === "folder" && ["context", "library", "agent"].includes(ancestor.section)
          ? workspaceTargetHref(workspaceId, ancestor.section as "context" | "library" | "agent", ancestor.id, "folder")
          : undefined,
    });
  }
  // On a create screen the selected node is the parent, so keep it clickable.
  // The new page is current even when its title matches an ancestor's title.
  if (childLabel !== undefined) {
    if (targetNode && !isSemanticSectionRoot(targetNode, rootLabel)) {
      items.push({ label: targetNode.title, href: treeNodeHref(workspaceId, targetNode) ?? undefined });
    }
    items.push({ label: childLabel });
    return items;
  }
  const currentLabel = targetNode?.title ?? fallbackLabel;
  if (currentLabel) {
    items.push({ label: currentLabel });
  }
  return items;
}

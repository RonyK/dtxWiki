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
}: {
  basePath?: string;
  workspaceId: string;
  rootLabel: string;
  nodes: TreeNodePayload[];
  targetId?: string | null;
  targetNodeId?: string | null;
  fallbackLabel?: string | null;
}): BreadcrumbItem[] {
  const items: BreadcrumbItem[] = [{ label: rootLabel, href: basePath && workspaceId ? (basePath.startsWith("/workspaces/") ? basePath : workspacePath(workspaceId, basePath)) : undefined }];
  const targetNode = targetNodeId
    ? nodes.find((node) => node.id === targetNodeId)
    : targetId
      ? nodes.find((node) => node.target_id === targetId)
      : null;
  for (const ancestor of targetNode?.breadcrumb ?? []) {
    // The live tree API returns a path that can include the selected node as
    // its final entry. Breadcrumbs adds the selected node separately so it is
    // rendered as the non-link current page; omit that path entry to avoid a
    // duplicated trailing label.
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
  const currentLabel = targetNode?.title ?? fallbackLabel;
  if (currentLabel) {
    items.push({ label: currentLabel });
  }
  return items;
}

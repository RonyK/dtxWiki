import { formatCanonicalContextUrl } from "../lib/contextLinks";
import type { TreeNodePayload } from "../api/workspace";
import type { BreadcrumbItem } from "./Breadcrumbs";
import { TREE_TARGET_PARAM } from "./treeRouteState";
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
  // P10.1: opt-in only — when a caller supplies it, an ancestor that is
  // itself a Context page links to the canonical permalink instead of the
  // query-param tree-target form. A folder ancestor has no page id to
  // canonicalize and always keeps the query-param link.
  workspaceId?: string | null;
  rootLabel: string;
  nodes: TreeNodePayload[];
  targetId?: string | null;
  targetNodeId?: string | null;
  fallbackLabel?: string | null;
}): BreadcrumbItem[] {
  const items: BreadcrumbItem[] = [{ label: rootLabel, href: basePath }];
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
      href: treeNodeHref(basePath, workspaceId, ancestorNode, ancestor.id),
    });
  }
  const currentLabel = targetNode?.title ?? fallbackLabel;
  if (currentLabel) {
    items.push({ label: currentLabel });
  }
  return items;
}

function treeNodeHref(
  basePath: string | undefined,
  workspaceId: string | null | undefined,
  node: TreeNodePayload | undefined,
  nodeId: string,
) {
  if (workspaceId && node?.target_kind === "context_page" && node.target_id) {
    return formatCanonicalContextUrl(workspaceId, node.target_id);
  }
  if (!basePath) {
    return undefined;
  }
  const params = new URLSearchParams({ [TREE_TARGET_PARAM]: nodeId });
  return `${basePath}?${params.toString()}`;
}

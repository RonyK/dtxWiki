import { useMemo } from "react";

import type { TreeNodePayload } from "../../shared/api/workspace";
import type { ContextLinkTarget } from "../../shared/lib/contextLinks";
import {
  contextPageHref,
  workspaceTargetHref,
} from "../../shared/lib/workspaceRoutes";
import { MarkdownView } from "./MarkdownView";

const INDEX_SECTIONS = new Set(["context", "library", "agent"]);
const VISIBLE = "visible";

/**
 * Render backend-generated workspace index Markdown in the authenticated app.
 * Translate index-specific locators using this workspace's loaded visible
 * tree. Links embedded in authored summaries follow the ordinary page link
 * policy; the Markdown/API wire format stays untouched for agent and MCP.
 */
export function RoutingIndexPreview({
  body,
  nodes,
  workspaceId,
}: {
  body: string;
  nodes: TreeNodePayload[];
  workspaceId: string;
}) {
  const linkTargets = useMemo(
    () => routingIndexLinkTargets(workspaceId, nodes),
    [nodes, workspaceId],
  );

  return (
    <MarkdownView
      allowWorkspaceNavigationLinks
      body={body}
      linkTargets={linkTargets}
      workspaceId={workspaceId}
    />
  );
}

export function routingIndexLinkTargets(
  workspaceId: string,
  nodes: TreeNodePayload[],
): ContextLinkTarget[] {
  if (!workspaceId) return [];

  const visibleNodes = nodes.filter(
    (node) => node.visibility_status === VISIBLE && INDEX_SECTIONS.has(node.section),
  );
  const nodesById = new Map(visibleNodes.map((node) => [node.id, node]));
  const targets = new Map<string, ContextLinkTarget>();
  const ambiguous = new Set<string>();

  const addTarget = (target: ContextLinkTarget) => {
    if (ambiguous.has(target.locator)) return;
    const previous = targets.get(target.locator);
    if (previous && previous.href !== target.href) {
      targets.delete(target.locator);
      ambiguous.add(target.locator);
      return;
    }
    targets.set(target.locator, target);
  };

  for (const node of visibleNodes) {
    if (node.node_kind === "folder") {
      const path = visibleFolderPath(node, nodesById);
      if (path) {
        addTarget({
          locator: `node:${path}`,
          href: workspaceTargetHref(
            workspaceId,
            node.section as "context" | "library" | "agent",
            node.id,
            "folder",
          ),
          target_page_id: null,
          state: "ok",
        });
      }
    }

    if (node.target_kind === "context_page" && node.target_id) {
      const href = contextPageHref(workspaceId, node.target_id);
      addTarget({
        locator: `/p/${encodeURIComponent(workspaceId)}/${encodeURIComponent(node.target_id)}`,
        href,
        target_page_id: node.target_id,
        state: "ok",
      });
    } else if (node.target_kind === "library_item" && node.target_id) {
      addTarget({
        locator: `/library/${encodeURIComponent(node.target_id)}.md`,
        href: workspaceTargetHref(workspaceId, "library", node.target_id, "item"),
        target_page_id: null,
        state: "ok",
      });
    }
  }

  return [...targets.values()];
}

function visibleFolderPath(
  folder: TreeNodePayload,
  nodesById: Map<string, TreeNodePayload>,
): string | null {
  if (folder.node_kind !== "folder") return null;
  const section = folder.section;
  const parts: string[] = [];
  const seen = new Set<string>();
  let current: TreeNodePayload | undefined = folder;

  while (current) {
    if (
      seen.has(current.id) ||
      current.section !== section ||
      current.visibility_status !== VISIBLE
    ) {
      return null;
    }
    seen.add(current.id);
    parts.unshift(current.slug || current.id);
    if (!current.parent_id) break;
    current = nodesById.get(current.parent_id);
    if (!current) return null;
  }

  if (parts[0] !== section) return null;
  return `/${parts.join("/")}`;
}

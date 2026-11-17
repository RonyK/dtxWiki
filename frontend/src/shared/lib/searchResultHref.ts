import { contextPageHref as canonicalContextHref, VERSION_QUERY_PARAM, workspacePath, workspaceTargetHref } from "./workspaceRoutes";
import { isWorkspaceUuid } from "./workspaceIdentity";
export type SearchResultLinkInput = {
  id: string;
  type: string;
  stable_url?: string | null;
  version_id?: string | null;
  source_ref?: Record<string, string> | null;
};

export type SearchResultDestination = "context" | "library" | "agent" | "review" | "chats" | "ask";
export type SearchResultLink = {
  href: string;
  destination: SearchResultDestination;
};

export function searchResultHref<T extends SearchResultLinkInput>(
  result: T,
  workspaceId: string,
): string | null {
  return searchResultLink(result, workspaceId)?.href ?? null;
}

// Resolve destination and href together: canonical Context URLs do not start
// with /context, and feedback can point to a different domain than its type.
export function searchResultLink<T extends SearchResultLinkInput>(
  result: T,
  workspaceId: string,
): SearchResultLink | null {
  if (!isWorkspaceUuid(workspaceId)) return null;
  if (result.stable_url) {
    const prefix = `/workspaces/${workspaceId}/library/items/`;
    if (["library_item", "library_artifact", "library_item_version", "source"].includes(result.type) &&
        result.stable_url.startsWith(prefix)) {
      return { href: result.stable_url, destination: "library" };
    }
    if ((result.type === "context_page" || result.type === "context_claim") &&
        result.stable_url.startsWith(`/p/${workspaceId}/`)) {
      return { href: contextVersionHref(result.stable_url, result), destination: "context" };
    }
  }
  if (result.type === "feedback") {
    return sourceRefHref(result.source_ref ?? {}, workspaceId);
  }
  if (result.type === "change_set") {
    return reviewItemHref(result.source_ref ?? {}, workspaceId);
  }
  if (result.type === "library_artifact") {
    return { href: workspaceTargetHref(workspaceId, "library", result.id, "artifact"), destination: "library" };
  }
  const targetId = resultDocumentId(result);
  if (
    (result.type === "context_page" || result.type === "context_claim") &&
    targetId
  ) {
    const link = contextPageHref(targetId, workspaceId);
    return link ? { ...link, href: contextVersionHref(link.href, result) } : null;
  }
  if (result.type === "agent_page" && targetId) {
    return { href: workspaceTargetHref(workspaceId, "agent", targetId), destination: "agent" };
  }
  // A conversation opens on its own screen: an MCP chat in Chats, an Ask
  // session in Ask. Those routes enforce their own access.
  if (result.type === "mcp_chat_session" && targetId) {
    return { href: workspacePath(workspaceId, "chats/" + encodeURIComponent(targetId)), destination: "chats" };
  }
  if (result.type === "ask_session" && targetId) {
    return { href: workspacePath(workspaceId, "ask/" + encodeURIComponent(targetId)), destination: "ask" };
  }
  if (result.type === "workspace_tree_node" && targetId) {
    const ref = result.source_ref ?? {};
    const section = ref.section;
    if (ref.node_kind !== "folder" || (section !== "context" && section !== "library" && section !== "agent")) return null;
    return { href: workspaceTargetHref(workspaceId, section, targetId, "folder"), destination: section };
  }
  if (
    (result.type === "library_item" ||
      result.type === "library_artifact" ||
      result.type === "library_item_version" ||
      result.type === "source") &&
    targetId
  ) {
    return { href: workspaceTargetHref(workspaceId, "library", targetId), destination: "library" };
  }
  return null;
}

function sourceRefHref(
  sourceRef: Record<string, string>,
  workspaceId: string,
): SearchResultLink | null {
  if (sourceRef.kind === "context_page" && sourceRef.id) {
    return contextPageHref(sourceRef.id, workspaceId);
  }
  if (sourceRef.kind === "agent_page" && sourceRef.id) {
    return { href: workspaceTargetHref(workspaceId, "agent", sourceRef.id), destination: "agent" };
  }
  if (sourceRef.kind === "library_item" && sourceRef.id) {
    return { href: workspaceTargetHref(workspaceId, "library", sourceRef.id), destination: "library" };
  }
  return null;
}

function contextPageHref(pageId: string, workspaceId: string): SearchResultLink | null {
  if (!isWorkspaceUuid(workspaceId) || !isWorkspaceUuid(pageId)) return null;
  return { href: canonicalContextHref(workspaceId, pageId), destination: "context" };
}

function contextVersionHref(href: string, result: SearchResultLinkInput): string {
  const versionId = result.version_id ?? result.source_ref?.version_id;
  if (!versionId || !isWorkspaceUuid(versionId)) return href;
  const url = new URL(href, "https://dtxwiki.invalid");
  url.searchParams.set(VERSION_QUERY_PARAM, versionId);
  return url.pathname + url.search + url.hash;
}

function reviewItemHref(sourceRef: Record<string, string>, workspaceId: string): SearchResultLink | null {
  if (sourceRef.kind === "review_item" && sourceRef.id) {
    return { href: workspacePath(workspaceId, "review/" + encodeURIComponent(sourceRef.id)), destination: "review" };
  }
  return null;
}

function resultDocumentId(result: SearchResultLinkInput): string | null {
  const sourceRef = result.source_ref ?? {};
  if (sourceRef.kind === "content_chunk" && sourceRef.document_id) {
    return sourceRef.document_id;
  }
  if (
    result.type === "library_artifact" ||
    result.type === "library_item_version"
  ) {
    if (sourceRef.kind === "library_item" && sourceRef.id) {
      return sourceRef.id;
    }
    return null;
  }
  if (result.type === "context_claim") {
    return sourceRef.kind === "context_page" && sourceRef.id
      ? sourceRef.id
      : null;
  }
  if (sourceRef.kind === result.type && sourceRef.id) {
    return sourceRef.id;
  }
  return result.id;
}

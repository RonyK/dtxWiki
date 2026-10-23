export type SearchResultLinkInput = {
  id: string;
  type: string;
  concept_path?: string | null;
  source_ref?: Record<string, string> | null;
};

export type SearchResultDestination = "context" | "library" | "agent" | "review";
export type SearchResultLink = {
  href: string;
  destination: SearchResultDestination;
};

export function searchResultHref<T extends SearchResultLinkInput>(
  result: T,
  workspaceId?: string | null,
): string | null {
  return searchResultLink(result, workspaceId)?.href ?? null;
}

// Resolve destination and href together: canonical Context URLs do not start
// with /context, and feedback can point to a different domain than its type.
export function searchResultLink<T extends SearchResultLinkInput>(
  result: T,
  workspaceId?: string | null,
): SearchResultLink | null {
  if (result.type === "feedback") {
    return sourceRefHref(result.source_ref ?? {}, workspaceId);
  }
  if (result.type === "change_set") {
    return reviewItemHref(result.source_ref ?? {});
  }
  const targetId = resultDocumentId(result);
  if (
    (result.type === "context_page" || result.type === "context_claim") &&
    targetId
  ) {
    return contextPageHref(targetId, workspaceId);
  }
  if (result.type === "agent_page" && targetId) {
    return { href: `/agent?target=${encodeURIComponent(targetId)}`, destination: "agent" };
  }
  if (result.type === "workspace_tree_node" && targetId) {
    return { href: `/context?target=${encodeURIComponent(targetId)}`, destination: "context" };
  }
  if (
    (result.type === "library_item" ||
      result.type === "library_artifact" ||
      result.type === "library_item_version" ||
      result.type === "source") &&
    targetId
  ) {
    return { href: `/library?target=${encodeURIComponent(targetId)}`, destination: "library" };
  }
  return null;
}

function sourceRefHref(
  sourceRef: Record<string, string>,
  workspaceId?: string | null,
): SearchResultLink | null {
  if (sourceRef.kind === "context_page" && sourceRef.id) {
    return contextPageHref(sourceRef.id, workspaceId);
  }
  if (sourceRef.kind === "agent_page" && sourceRef.id) {
    return { href: `/agent?target=${encodeURIComponent(sourceRef.id)}`, destination: "agent" };
  }
  if (sourceRef.kind === "library_item" && sourceRef.id) {
    return { href: `/library?target=${encodeURIComponent(sourceRef.id)}`, destination: "library" };
  }
  return null;
}

// P10.1: canonical Context permalink, once a workspace id is in scope at the
// call site — a bare tree/folder target has no page id to canonicalize and
// keeps using the query-param form (see treeBreadcrumbs.ts).
function contextPageHref(pageId: string, workspaceId?: string | null): SearchResultLink {
  if (workspaceId) {
    return { href: `/workspaces/${encodeURIComponent(workspaceId)}/context/pages/${encodeURIComponent(pageId)}`, destination: "context" };
  }
  return { href: `/context?target=${encodeURIComponent(pageId)}`, destination: "context" };
}

function reviewItemHref(sourceRef: Record<string, string>): SearchResultLink | null {
  if (sourceRef.kind === "review_item" && sourceRef.id) {
    return { href: `/review?review=${encodeURIComponent(sourceRef.id)}`, destination: "review" };
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
    return libraryItemIdFromConceptPath(result.concept_path);
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

function libraryItemIdFromConceptPath(path: string | null | undefined) {
  const match = path?.match(/^\/?library\/([^/]+)\.md$/);
  return match?.[1] ?? null;
}

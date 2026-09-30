export type SearchResultLinkInput = {
  id: string;
  type: string;
  concept_path?: string | null;
  source_ref?: Record<string, string> | null;
};

export function searchResultHref<T extends SearchResultLinkInput>(
  result: T,
  workspaceId?: string | null,
): string | null {
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
    return `/agent?target=${encodeURIComponent(targetId)}`;
  }
  if (result.type === "workspace_tree_node" && targetId) {
    return `/context?target=${encodeURIComponent(targetId)}`;
  }
  if (
    (result.type === "library_item" ||
      result.type === "library_artifact" ||
      result.type === "library_item_version" ||
      result.type === "source") &&
    targetId
  ) {
    return `/library?target=${encodeURIComponent(targetId)}`;
  }
  return null;
}

function sourceRefHref(
  sourceRef: Record<string, string>,
  workspaceId?: string | null,
): string | null {
  if (sourceRef.kind === "context_page" && sourceRef.id) {
    return contextPageHref(sourceRef.id, workspaceId);
  }
  if (sourceRef.kind === "agent_page" && sourceRef.id) {
    return `/agent?target=${encodeURIComponent(sourceRef.id)}`;
  }
  if (sourceRef.kind === "library_item" && sourceRef.id) {
    return `/library?target=${encodeURIComponent(sourceRef.id)}`;
  }
  return null;
}

// P10.1: canonical Context permalink, once a workspace id is in scope at the
// call site — a bare tree/folder target has no page id to canonicalize and
// keeps using the query-param form (see treeBreadcrumbs.ts).
function contextPageHref(pageId: string, workspaceId?: string | null) {
  if (workspaceId) {
    return `/workspaces/${encodeURIComponent(workspaceId)}/context/pages/${encodeURIComponent(pageId)}`;
  }
  return `/context?target=${encodeURIComponent(pageId)}`;
}

function reviewItemHref(sourceRef: Record<string, string>): string | null {
  if (sourceRef.kind === "review_item" && sourceRef.id) {
    return `/review?review=${encodeURIComponent(sourceRef.id)}`;
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

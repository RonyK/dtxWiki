import { apiRequest } from "./client";
import type { components } from "./types.gen";

export type ContextMediaAttachment =
  components["schemas"]["ContextMediaAttachmentPayload"];

/**
 * `mine` is the default the server also defaults to, and the only scope a
 * non-owner may ask for -- `workspace` answers 403 below owner. The caller
 * decides which to send from the role it already knows, rather than probing.
 */
export type ContextMediaAttachmentScope = "mine" | "workspace";

export type ContextMediaAttachmentQuery = {
  scope?: ContextMediaAttachmentScope;
  usage?: "all" | "used" | "unused";
  orphanOnly?: boolean;
  pageId?: string;
  query?: string;
  limit?: number;
  cursor?: string;
};

export type ContextMediaAttachmentPage = {
  items: ContextMediaAttachment[];
  /** Null once the last page has been served. */
  nextCursor: string | null;
  /** Echoed back so an empty list can say whose emptiness it is describing. */
  scope: ContextMediaAttachmentScope;
};

type ContextMediaAttachmentListResponse = {
  data: {
    items: ContextMediaAttachment[];
    next_cursor: string | null;
    scope: ContextMediaAttachmentScope;
  };
};

export async function listWorkspaceContextMediaAttachments(
  workspaceId: string,
  query: ContextMediaAttachmentQuery = {},
): Promise<ContextMediaAttachmentPage> {
  const params = new URLSearchParams();
  if (query.scope) params.set("scope", query.scope);
  if (query.usage && query.usage !== "all") params.set("usage", query.usage);
  if (query.orphanOnly) params.set("orphan_only", "true");
  if (query.pageId) params.set("page_id", query.pageId);
  // Trimmed: a box holding only spaces should read as "no filter", not as a
  // search for a space that matches every filename with one in it.
  const trimmed = query.query?.trim();
  if (trimmed) params.set("q", trimmed);
  if (query.limit) params.set("limit", String(query.limit));
  if (query.cursor) params.set("cursor", query.cursor);
  const suffix = params.toString();
  const response = await apiRequest<ContextMediaAttachmentListResponse>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context/media/attachments${
      suffix ? `?${suffix}` : ""
    }`,
  );
  return {
    items: response.data.items,
    nextCursor: response.data.next_cursor,
    scope: response.data.scope,
  };
}

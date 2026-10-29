import { apiRequest } from "./client";
import type { components } from "./types.gen";

export type InboxItem = components["schemas"]["InboxItemPayload"];
export type InboxPage = components["schemas"]["InboxPagePayload"];
export type InboxMark = components["schemas"]["InboxMarkPayload"];
export type InboxState = "all" | "unread" | "archived";
// One inbox the signed-in user can open. Every inbox speaks the same list and
// marks API under its own base path, so a new inbox adds a scope, not a branch.
export type InboxScope = { key: string; basePath: string };

export const ACCOUNT_INBOX_SCOPE: InboxScope = {
  key: "account",
  basePath: "/api/v1/users/me/inbox",
};

export function workspaceInboxScope(workspaceId: string): InboxScope {
  return {
    key: `workspace:${workspaceId}`,
    basePath: `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/inbox`,
  };
}

export async function listInbox(
  scope: InboxScope,
  options: { state: InboxState; cursor?: string | null; limit?: number },
) {
  const params = new URLSearchParams({ state: options.state });
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.limit) params.set("limit", String(options.limit));
  const response = await apiRequest<{ data: InboxPage }>(`${scope.basePath}?${params}`);
  return response.data;
}

export async function markInboxItem(
  scope: InboxScope,
  item: Pick<InboxItem, "kind" | "item_key" | "revision">,
  marks: { read?: boolean; archived?: boolean },
) {
  const response = await apiRequest<{ data: InboxMark }>(`${scope.basePath}/marks`, {
    method: "POST",
    body: JSON.stringify({
      kind: item.kind,
      item_key: item.item_key,
      revision: item.revision,
      ...marks,
    }),
  });
  return response.data;
}

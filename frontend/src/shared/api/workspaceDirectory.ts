import { apiRequest } from "./client";
import { invalidateWorkspaceListCache } from "./workspace";

export type WorkspaceDirectoryRequestStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "cancelled"
  | "closed";

export type WorkspaceDirectoryItem = {
  workspace_id: string;
  title: string;
  description: string | null;
  visibility: "team" | "public";
  join_policy: "invite_only" | "request";
  current_request_status: WorkspaceDirectoryRequestStatus | null;
};

export type WorkspaceDirectoryData = {
  items: WorkspaceDirectoryItem[];
  next_cursor: string | null;
};

export type WorkspaceJoinRequestItem = {
  request_id: string;
  workspace_id: string;
  requester_user_id: string;
  requester_email: string;
  requester_display_name: string;
  status: WorkspaceDirectoryRequestStatus;
  attempt_no: number;
  requested_at: string;
  updated_at: string;
  decided_at: string | null;
  decided_by_user_id: string | null;
  decision_note: string | null;
  closed_reason: string | null;
};

export type WorkspaceJoinMutationData = {
  request: WorkspaceJoinRequestItem;
  new_attempt: boolean;
  membership_created: boolean;
};

export async function listWorkspaceDirectory({
  cursor,
  limit = 20,
  pendingOnly,
  query,
  signal,
}: {
  cursor?: string | null;
  limit?: number;
  pendingOnly?: boolean;
  query?: string;
  signal?: AbortSignal;
} = {}) {
  const params = new URLSearchParams();
  const normalizedQuery = query?.trim();
  if (normalizedQuery) {
    params.set("q", normalizedQuery);
  }
  if (cursor) {
    params.set("cursor", cursor);
  }
  if (pendingOnly) {
    params.set("pending_only", "true");
  }
  params.set("limit", String(limit));
  const response = await apiRequest<{ data: WorkspaceDirectoryData }>(
    `/api/v1/workspace-directory?${params.toString()}`,
    { signal },
  );
  return response.data;
}

export async function listPendingWorkspaceJoins(signal?: AbortSignal) {
  const items: WorkspaceDirectoryItem[] = [];
  let cursor: string | null = null;
  do {
    const page = await listWorkspaceDirectory({
      cursor,
      limit: 50,
      pendingOnly: true,
      signal,
    });
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor && !signal?.aborted);
  return items;
}

export async function requestWorkspaceJoin(workspaceId: string) {
  const response = await apiRequest<{ data: WorkspaceJoinMutationData }>(
    `/api/v1/workspaces/${workspaceId}/join-requests`,
    { method: "POST" },
  );
  if (response.data.membership_created) {
    invalidateWorkspaceListCache();
  }
  return response.data;
}

export async function cancelWorkspaceJoin(workspaceId: string) {
  const response = await apiRequest<{ data: WorkspaceJoinMutationData }>(
    `/api/v1/workspaces/${workspaceId}/join-requests/me`,
    { method: "DELETE" },
  );
  return response.data;
}

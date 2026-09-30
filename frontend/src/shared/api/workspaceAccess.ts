import { apiRequest } from "./client";
import { invalidateWorkspaceListCache } from "./workspace";

export type WorkspaceMemberRole = "viewer" | "editor" | "owner";
export type WorkspaceMemberInviteRole = Exclude<WorkspaceMemberRole, "owner">;

export type WorkspaceMember = {
  user_id: string;
  email: string;
  display_name: string;
  status: string;
  role: WorkspaceMemberRole;
  joined_at: string;
  is_personal_anchor_owner: boolean;
  is_current_user: boolean;
};

export type WorkspaceMemberPage = {
  items: WorkspaceMember[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  next_cursor: string | null;
};

export type WorkspaceMemberMutation = {
  member: WorkspaceMember;
  created: boolean;
  first_share: boolean;
  first_share_warning_code: string | null;
  member_count: number;
};

export type WorkspaceMemberRemoval = {
  removed_user_id: string;
  previous_role: WorkspaceMemberRole;
  default_workspace_id: string | null;
  fallback_workspace_id: string | null;
  member_count: number;
};

export type WorkspaceVisibility = "private" | "team" | "public";
export type WorkspaceJoinPolicy = "invite_only" | "request";

export type WorkspaceAccessSettings = {
  workspace_id: string;
  updated_at: string;
  visibility: WorkspaceVisibility;
  join_policy: WorkspaceJoinPolicy;
  pending_join_request_count: number;
  public_page_count: number;
  closed_join_request_count: number;
  cleared_public_page_count: number;
};

export type WorkspaceJoinRequestStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "cancelled"
  | "closed";

export type WorkspaceJoinRequest = {
  request_id: string;
  workspace_id: string;
  requester_user_id: string;
  requester_email: string;
  requester_display_name: string;
  status: WorkspaceJoinRequestStatus;
  attempt_no: number;
  requested_at: string;
  updated_at: string;
  decided_at: string | null;
  decided_by_user_id: string | null;
  decision_note: string | null;
  closed_reason: string | null;
};

export type WorkspaceJoinRequestPage = {
  items: WorkspaceJoinRequest[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

export type WorkspaceJoinMutation = {
  request: WorkspaceJoinRequest;
  new_attempt: boolean;
  membership_created: boolean;
};

export async function listWorkspaceMembers(
  workspaceId: string,
  options: {
    query?: string;
    role?: WorkspaceMemberRole;
    cursor?: string;
    limit?: number;
    signal?: AbortSignal;
  } = {},
) {
  const query = new URLSearchParams();
  query.set("limit", String(options.limit ?? 25));
  if (options.query) {
    query.set("query", options.query);
  }
  if (options.role) {
    query.set("role", options.role);
  }
  if (options.cursor) {
    query.set("cursor", options.cursor);
  }
  const response = await apiRequest<{ data: WorkspaceMemberPage }>(
    `/api/v1/workspaces/${workspaceId}/members?${query.toString()}`,
    { signal: options.signal },
  );
  return response.data;
}

export async function addWorkspaceMember(
  workspaceId: string,
  input: {
    email: string;
    role: WorkspaceMemberInviteRole;
    confirm_first_share?: boolean;
  },
) {
  const response = await apiRequest<{ data: WorkspaceMemberMutation }>(
    `/api/v1/workspaces/${workspaceId}/members`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
  invalidateWorkspaceListCache();
  return response.data;
}

export async function updateWorkspaceMemberRole(
  workspaceId: string,
  userId: string,
  role: WorkspaceMemberRole,
) {
  const response = await apiRequest<{ data: WorkspaceMemberMutation }>(
    `/api/v1/workspaces/${workspaceId}/members/${userId}`,
    {
      method: "PATCH",
      body: JSON.stringify({ role }),
    },
  );
  invalidateWorkspaceListCache();
  return response.data;
}

export async function removeWorkspaceMember(
  workspaceId: string,
  userId: string,
) {
  const response = await apiRequest<{ data: WorkspaceMemberRemoval }>(
    `/api/v1/workspaces/${workspaceId}/members/${userId}`,
    { method: "DELETE" },
  );
  invalidateWorkspaceListCache();
  return response.data;
}

export async function getWorkspaceAccess(workspaceId: string) {
  const response = await apiRequest<{ data: WorkspaceAccessSettings }>(
    `/api/v1/workspaces/${workspaceId}/access`,
  );
  return response.data;
}

export async function updateWorkspaceAccess(
  workspaceId: string,
  input: {
    visibility: WorkspaceVisibility;
    join_policy: WorkspaceJoinPolicy;
    confirm_access_change: boolean;
    expected_updated_at: string;
    expected_pending_join_request_count?: number;
    expected_public_page_count?: number;
  },
) {
  const response = await apiRequest<{ data: WorkspaceAccessSettings }>(
    `/api/v1/workspaces/${workspaceId}/access`,
    {
      method: "PATCH",
      body: JSON.stringify(input),
    },
  );
  invalidateWorkspaceListCache();
  return response.data;
}

export async function listWorkspaceJoinRequests(
  workspaceId: string,
  options: { offset?: number; limit?: number } = {},
) {
  const query = new URLSearchParams({
    status: "pending",
    offset: String(options.offset ?? 0),
    limit: String(options.limit ?? 50),
  });
  const response = await apiRequest<{ data: WorkspaceJoinRequestPage }>(
    `/api/v1/workspaces/${workspaceId}/join-requests?${query.toString()}`,
  );
  return response.data;
}

export async function decideWorkspaceJoinRequest(
  workspaceId: string,
  requestId: string,
  input: {
    decision: "approve" | "reject";
    expected_attempt_no: number;
    decision_note?: string | null;
  },
) {
  const response = await apiRequest<{ data: WorkspaceJoinMutation }>(
    `/api/v1/workspaces/${workspaceId}/join-requests/${requestId}`,
    {
      method: "PATCH",
      body: JSON.stringify(input),
    },
  );
  invalidateWorkspaceListCache();
  return response.data;
}

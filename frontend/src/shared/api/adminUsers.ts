import { apiRequest } from "./client";
import type { components } from "./types.gen";

export type AdminUserWorkspace = {
  workspace_id: string;
  slug: string;
  title: string;
  role: "viewer" | "editor" | "owner";
};

export type AdminUser = {
  user_id: string;
  email: string | null;
  display_name: string;
  status: string;
  email_verified_at: string | null;
  workspaces: AdminUserWorkspace[];
  identities?: AdminUserIdentity[];
};

export type AdminUserIdentity = {
  identity_id: string;
  provider_slug: string;
  issuer: string;
  subject: string;
};

export type AdminIdentityRebindRequest = AdminUserIdentity & {
  expected_issuer: string;
  expected_subject: string;
  reason: string;
};

export async function rebindAdminIdentity(userId: string, request: AdminIdentityRebindRequest) {
  return apiRequest<{ data: AdminUserIdentity & { user_id: string } }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/identities/rebind`,
    { method: "POST", body: JSON.stringify(request) },
  ).then((response) => response.data);
}

export type AdminUserSearchResult = {
  items: AdminUser[];
  total: number;
  limit: number;
  offset: number;
};

export type AdminPasswordResetDelivery = "email" | "link";

export type AdminPasswordResetRequest = {
  delivery: AdminPasswordResetDelivery;
  revoke_api_tokens: boolean;
};

export type AdminPasswordResetResult = {
  user_id: string;
  delivery: AdminPasswordResetDelivery;
  expires_at: string;
  reset_url: string | null;
  revoke_api_tokens: boolean;
};

export async function searchAdminUsers(
  query: string,
  options: { limit?: number; offset?: number } = {},
) {
  const params = new URLSearchParams({
    query,
    limit: String(options.limit ?? 25),
    offset: String(options.offset ?? 0),
  });
  const response = await apiRequest<{ data: AdminUserSearchResult }>(
    `/api/v1/admin/users?${params.toString()}`,
  );
  return response.data;
}

export async function issueAdminPasswordReset(
  userId: string,
  request: AdminPasswordResetRequest,
) {
  const response = await apiRequest<{ data: AdminPasswordResetResult }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/password-reset`,
    {
      method: "POST",
      body: JSON.stringify(request),
    },
  );
  return response.data;
}

export type AdminEmailConflict = components["schemas"]["AdminEmailConflictData"];
export type AdminAuditEvent = components["schemas"]["AdminAuditEventData"];

export async function listAdminEmailConflicts() {
  return apiRequest<{ data: components["schemas"]["AdminEmailConflictListData"] }>(
    "/api/v1/admin/email-conflicts?status=pending&limit=50",
  ).then((response) => response.data);
}

export async function decideAdminEmailConflict(
  requestId: string,
  decision: "approve" | "reject",
  reason: string,
) {
  return apiRequest<{ data: components["schemas"]["AdminEmailConflictDecisionData"] }>(
    `/api/v1/admin/email-conflicts/${encodeURIComponent(requestId)}/decision`,
    { method: "POST", body: JSON.stringify({ decision, reason }) },
  ).then((response) => response.data);
}

export async function listAdminAuditEvents(filters: {
  actionPrefix?: string;
  targetUserId?: string;
  cursor?: string | null;
}) {
  const query = new URLSearchParams({ limit: "50" });
  if (filters.actionPrefix) query.set("action_prefix", filters.actionPrefix);
  if (filters.targetUserId) query.set("target_user_id", filters.targetUserId);
  if (filters.cursor) query.set("cursor", filters.cursor);
  return apiRequest<{ data: components["schemas"]["AdminAuditEventListData"] }>(
    `/api/v1/admin/audit-events?${query.toString()}`,
  ).then((response) => response.data);
}

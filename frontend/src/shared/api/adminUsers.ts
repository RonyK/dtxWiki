import { apiRequest } from "./client";

export type AdminUserWorkspace = {
  workspace_id: string;
  slug: string;
  title: string;
  role: "viewer" | "editor" | "owner";
};

export type AdminUser = {
  user_id: string;
  email: string;
  display_name: string;
  status: string;
  email_verified_at: string | null;
  deletion_requested_at: string | null;
  purge_after: string | null;
  deletion_failure_count?: number | null;
  deletion_last_error?: string | null;
  workspaces: AdminUserWorkspace[];
};

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

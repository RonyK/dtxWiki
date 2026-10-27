import { apiRequest } from "./client";

export type UserDeletionPolicy = { retention_days: number };

export type UserDeletionWorkspace = {
  workspace_id: string;
  title: string;
  // Other members whose accounts are active.
  other_member_count: number;
};

export type UserDeletionPreview = UserDeletionPolicy & {
  // Held with the account: closed now, deleted with it after retention.
  target_workspaces: UserDeletionWorkspace[];
  // Self-withdrawal only: delete these, or give them another admin, first.
  blocking_workspaces: UserDeletionWorkspace[];
};

export type UserDeletionApprovedWorkspace = Pick<
  UserDeletionWorkspace,
  "workspace_id" | "other_member_count"
>;

/** The exact targets a confirmation approves; the server refuses any drift. */
export function approvedDeletionWorkspaces(
  preview: UserDeletionPreview,
): UserDeletionApprovedWorkspace[] {
  return preview.target_workspaces.map(({ workspace_id, other_member_count }) => ({
    workspace_id,
    other_member_count,
  }));
}

/** Where the owner deletes a workspace, opened beside the withdrawal form. */
export function workspaceDeletionSettingsPath(workspaceId: string) {
  return `/workspaces/${encodeURIComponent(workspaceId)}/settings?section=workspace#workspace-danger-title`;
}

export type UserDeletion = {
  user_id: string;
  status: "deletion_pending";
  requested_at: string;
  purge_after: string;
  retention_days: number;
};

export type UserDeletionRestore = {
  user_id: string;
  status: "active" | "disabled";
};

export async function getUserDeletionPolicy() {
  const response = await apiRequest<{ data: UserDeletionPolicy }>(
    "/api/v1/admin/user-deletion-policy",
  );
  return response.data;
}

export async function getMyDeletionPreview() {
  const response = await apiRequest<{ data: UserDeletionPreview }>(
    "/api/v1/users/me/deletion-preview",
  );
  return response.data;
}

export async function getUserDeletionPreview(userId: string) {
  const response = await apiRequest<{ data: UserDeletionPreview }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/deletion-preview`,
  );
  return response.data;
}

export async function updateUserDeletionPolicy(retentionDays: number) {
  const response = await apiRequest<{ data: UserDeletionPolicy }>(
    "/api/v1/admin/user-deletion-policy",
    { method: "PATCH", body: JSON.stringify({ retention_days: retentionDays }) },
  );
  return response.data;
}

export async function withdrawAccount(input: {
  confirmation_email: string;
  current_password: string;
  expected_retention_days: number;
  approved_workspaces: UserDeletionApprovedWorkspace[];
}) {
  const response = await apiRequest<{ data: UserDeletion }>("/api/v1/users/me", {
    method: "DELETE",
    body: JSON.stringify(input),
  });
  return response.data;
}

export async function deleteUserAccount(
  userId: string,
  confirmationEmail: string,
  expectedRetentionDays: number,
  approvedWorkspaces: UserDeletionApprovedWorkspace[],
) {
  const response = await apiRequest<{ data: UserDeletion }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}`,
    {
      method: "DELETE",
      body: JSON.stringify({
        confirmation_email: confirmationEmail,
        expected_retention_days: expectedRetentionDays,
        approved_workspaces: approvedWorkspaces,
      }),
    },
  );
  return response.data;
}

export async function restoreUserAccount(userId: string) {
  const response = await apiRequest<{ data: UserDeletionRestore }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/deletion-restore`,
    { method: "POST" },
  );
  return response.data;
}

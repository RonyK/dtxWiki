import { apiRequest } from "./client";

export type UserDeletionPolicy = { retention_days: number };

export type UserDeletionPreview = UserDeletionPolicy & {
  personal_workspace: {
    workspace_id: string;
    title: string;
    other_member_count: number;
  } | null;
  transferred_workspaces: { workspace_id: string; title: string }[];
  recipient_admin_count: number;
};

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
}) {
  const response = await apiRequest<{ data: UserDeletion }>("/api/v1/users/me", {
    method: "DELETE",
    body: JSON.stringify(input),
  });
  return response.data;
}

export async function deleteUserAccount(userId: string, confirmationEmail: string, expectedRetentionDays: number) {
  const response = await apiRequest<{ data: UserDeletion }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}`,
    { method: "DELETE", body: JSON.stringify({ confirmation_email: confirmationEmail, expected_retention_days: expectedRetentionDays }) },
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

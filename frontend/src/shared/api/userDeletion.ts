import { apiRequest } from "./client";

export type UserDeletionPolicy = { retention_days: number };

export type UserDeletion = {
  user_id: string;
  status: "deletion_pending";
  requested_at: string;
  purge_after: string;
  retention_days: number;
};

export async function getUserDeletionPolicy(admin = false) {
  const response = await apiRequest<{ data: UserDeletionPolicy }>(
    admin ? "/api/v1/admin/user-deletion-policy" : "/api/v1/users/me/deletion-policy",
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

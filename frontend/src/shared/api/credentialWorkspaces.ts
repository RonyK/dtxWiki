import { apiRequest } from "./client";

export type CredentialWorkspacePage = {
  data: {
    id: string;
    title: string;
    slug: string;
    role: string;
    is_default: boolean;
  }[];
  total_count: number;
  next_cursor: string | null;
};

export function listCredentialWorkspaces(query: string, cursor: string | null) {
  const params = new URLSearchParams({ q: query, limit: "8" });
  if (cursor) params.set("cursor", cursor);
  return apiRequest<CredentialWorkspacePage>(
    `/api/v1/auth/credential-workspaces?${params}`,
  );
}

import { apiRequest } from "./client";
import type { components } from "./types.gen";

export const MCP_ACCESS_PAGE_SIZE = 20;

export type McpClientListStatus = "active" | "disabled" | "all";
export type AccessKeyListStatus =
  | "active"
  | "inactive"
  | "expired"
  | "revoked"
  | "all";

export type CursorListPage<TItem, TStatusCounts> = {
  data: TItem[];
  next_cursor?: string | null;
  total_count: number;
  filtered_count: number;
  status_counts: TStatusCounts;
};

// Keep the transport contract generated from OpenAPI. The generic shell above
// describes only what cursor navigation consumes; resource fields, counts, and
// delete payloads remain compile-time checked against the backend schemas.
export type SettingsMcpClientPage =
  components["schemas"]["SettingsMcpClientListResponse"];
// D-01v2 (P10.3 T4): the workspace-scoped `GET/DELETE
// /workspaces/{id}/settings/access-keys*` pair is gone. Access keys are an
// account-scoped resource now, so the Settings list reads `GET /auth/tokens`
// (see `listAccessKeys` in ./auth) and each row carries its own allowed
// `workspaces` set instead of one immutable `workspace_id`.
type SettingsMcpClientDeleteResponse =
  components["schemas"]["SettingsMcpClientDeleteResponse"];

export type PageFilters<TStatus extends string> = {
  status: TStatus;
  q?: string;
  limit?: number;
  cursor?: string | null;
};

export async function listSettingsMcpClients(
  workspaceId: string,
  filters: PageFilters<McpClientListStatus>,
  signal?: AbortSignal,
) {
  return apiRequest<SettingsMcpClientPage>(
    `/api/v1/workspaces/${workspaceId}/settings/mcp-clients${pageQuery(filters)}`,
    { signal },
  );
}

export async function deleteSettingsMcpClient(
  workspaceId: string,
  clientId: string,
) {
  const response = await apiRequest<SettingsMcpClientDeleteResponse>(
    `/api/v1/workspaces/${workspaceId}/settings/mcp-clients/${clientId}`,
    {
    method: "DELETE",
    },
  );
  return response.data;
}

export function pageQuery<TStatus extends string>(
  filters: PageFilters<TStatus>,
) {
  const query = new URLSearchParams();
  query.set("status", filters.status);
  query.set("limit", String(filters.limit ?? MCP_ACCESS_PAGE_SIZE));
  const search = filters.q?.trim();
  if (search) {
    query.set("q", search);
  }
  if (filters.cursor) {
    query.set("cursor", filters.cursor);
  }
  return `?${query.toString()}`;
}

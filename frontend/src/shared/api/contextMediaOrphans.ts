import { apiRequest } from "./client";
import type { components } from "./types.gen";

// Epic-2 D-7 (Phase 5c) shipped the two read endpoints below with no UI --
// this client (and the Settings section it backs) is the missing consumer.
// Both endpoints return the full current set with no paging and no age
// filter (visibility is deliberately not retention-gated; see
// `ContextMediaOrphanPayload`'s docstring in the generated schema).
export type ContextMediaOrphan = components["schemas"]["ContextMediaOrphanPayload"];

type ContextMediaOrphanListResponse = {
  data: { items: ContextMediaOrphan[] };
};

export async function listWorkspaceContextMediaOrphans(workspaceId: string) {
  const response = await apiRequest<ContextMediaOrphanListResponse>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context/media/orphans`,
  );
  return response.data.items;
}

// The admin surface is still scoped to one workspace at a time (`workspace_id`
// is a required query param) -- there is deliberately no cross-workspace
// aggregation here, since that would be a control-plane query the Phase 3
// gate forbids. Server admins pick which workspace via the same workspace
// switcher as everything else in Settings.
export async function listAdminContextMediaOrphans(workspaceId: string) {
  const response = await apiRequest<ContextMediaOrphanListResponse>(
    `/api/v1/admin/context-media/orphans?workspace_id=${encodeURIComponent(workspaceId)}`,
  );
  return response.data.items;
}

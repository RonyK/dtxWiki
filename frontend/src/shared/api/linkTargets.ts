import { apiRequest } from "./client";
import type { components } from "./types.gen";

export type LinkTargetPreviewRequest = components["schemas"]["LinkPreviewRequest"] & {
  source_page_id: string | null;
  source_version_id: string | null;
};
export type LinkTargetPreview = components["schemas"]["LinkPreviewPayload"];

/** Read-only projection of the exact unsaved buffer; it never saves the body. */
export async function previewLinkTargets(
  workspaceId: string,
  request: LinkTargetPreviewRequest,
  signal?: AbortSignal,
): Promise<LinkTargetPreview> {
  const response = await apiRequest<{ data: LinkTargetPreview }>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/link-targets/preview`,
    { method: "POST", body: JSON.stringify(request), signal },
  );
  return response.data;
}

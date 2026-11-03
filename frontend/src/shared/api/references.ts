import { apiRequest } from "./client";
import type { components } from "./types.gen";

export type DocumentReferenceKind = "context_page" | "library_item" | "agent_page";
export type DocumentReferenceTarget = {
  target_kind: DocumentReferenceKind;
  target_id: string;
  note?: string | null;
};
export type ReferenceRemoval = DocumentReferenceTarget | { reference_id: string };
export type ReferenceView = components["schemas"]["ReferenceView"];
export type ReferenceCommandResult = components["schemas"]["CiteResult"];

export async function changePageReferences(
  workspaceId: string,
  pageId: string,
  request: {
    version_id: string;
    expected_editing_revision: number;
    idempotency_key: string;
    add: DocumentReferenceTarget[];
    remove: ReferenceRemoval[];
  },
) {
  const response = await apiRequest<{ data: ReferenceCommandResult }>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context/pages/${encodeURIComponent(pageId)}/references`,
    { method: "POST", body: JSON.stringify(request) },
  );
  return response.data;
}

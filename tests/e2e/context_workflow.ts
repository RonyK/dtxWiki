import type { components } from "../../frontend/src/shared/api/types.gen";
import { backendBaseUrl } from "./auth";
import { expect, type APIRequestContext } from "./fixtures";

/** Bind fixture writes to the same merge candidate as browser confirmation. */
export async function mergeBinding(
  request: APIRequestContext,
  workspaceId: string,
  pageId: string,
  versionId: string,
) {
  const response = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${pageId}/merge-preview`,
    { data: { version_id: versionId } },
  );
  expect(response.ok(), await response.text()).toBeTruthy();
  const preview = (await response.json()).data as {
    editing_revision: number;
    published_version_id: string | null;
    result: { result_hash: string } | null;
  };
  expect(preview.result, "Context fixture must have a clean merge candidate").not.toBeNull();
  return {
    expected_editing_revision: preview.editing_revision,
    expected_published_version_id: preview.published_version_id,
    result_hash: preview.result!.result_hash,
  };
}

/** Approval publishes the previewed result; it needs no subsequent publish. */
export async function approveContextDraft(
  request: APIRequestContext,
  workspaceId: string,
  pageId: string,
  versionId: string,
  reviewItemId: string,
  decisionFields: Pick<components["schemas"]["ReviewDecisionRequest"], "decision_reason"> = {},
) {
  const response = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/reviews/${reviewItemId}/decision`,
    {
      data: {
        decision_type: "approve",
        ...decisionFields,
        ...await mergeBinding(request, workspaceId, pageId, versionId),
      },
    },
  );
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()).data as components["schemas"]["ReviewItemDetail"];
}

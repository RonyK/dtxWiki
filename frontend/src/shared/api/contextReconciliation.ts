import { apiRequest } from "./client";
import type { components } from "./types.gen";

export type ContextReconciliationBlockState =
  | "same"
  | "head_only"
  | "branch_only"
  | "conflict";

export type ContextReconciliationBlock = {
  block_id: string;
  kind: string;
  granularity: "block" | "table_row";
  parent_block_id: string | null;
  row_index: number | null;
  state: ContextReconciliationBlockState;
  label: string;
};

export type ContextReconciliationSide = {
  hash: string | null;
  text: string | null;
  actor_kind: string | null;
  actor_display_name: string | null;
  at: string | null;
  version_id: string | null;
};

export type ContextReconciliationOperationEffect = {
  op: "insert_after";
  content: string;
};

export type ContextReconciliationConflict = {
  block_id: string;
  resolution_target_id: string | null;
  kind: string;
  granularity: "block" | "table_row";
  parent_block_id: string | null;
  row_index: number | null;
  line_span: [number, number] | null;
  base: ContextReconciliationSide;
  head: ContextReconciliationSide;
  yours: ContextReconciliationSide;
  head_effects: ContextReconciliationOperationEffect[];
  yours_effects: ContextReconciliationOperationEffect[];
  identity_retired: boolean;
  restore_yours_available: boolean;
};

export type ContextReconciliationCleanChange = {
  block_id: string;
  resolution_target_id: string | null;
  kind: string;
  state: "head_only" | "branch_only";
  head: ContextReconciliationSide | null;
  yours: ContextReconciliationSide | null;
};

export type ContextReconciliationVersion = {
  version_id: string;
  version_no: number;
  content_hash: string;
  created_at: string;
};

/**
 * Server-owned reconciliation projection. In particular, `blocks` is required:
 * clients must never infer block identity or conflicts from a Markdown diff.
 */
export type ContextReconciliationDetail = {
  page: { id: string; slug: string; title: string };
  branch: ContextReconciliationVersion & {
    actor_kind: string;
    actor_display_name: string | null;
    base_version_id: string;
  };
  base: ContextReconciliationVersion;
  original_head: ContextReconciliationVersion;
  head: ContextReconciliationVersion;
  head_advance_count: number;
  blocks: ContextReconciliationBlock[];
  conflicts: ContextReconciliationConflict[];
  clean_changes: ContextReconciliationCleanChange[];
};

export type ContextReconciliationResolutionChoice =
  | "keep_head"
  | "restore_yours"
  | "custom";

export type ContextReconciliationResolution = {
  block_id: string;
  choice: ContextReconciliationResolutionChoice;
  expected_hash: string | null;
  content: string | null;
};

export type ContextReconciliationRequest = {
  expected_head_version_id: string;
  reconciles: [string];
  resolutions: ContextReconciliationResolution[];
  excluded_clean_change_ids: string[];
  reason: string;
  idempotency_key: string;
  dry_run: boolean;
};

export type ContextReconciliationPreview = {
  head_version_id: string;
  merged_body_markdown: string;
  predicted_block_map: Array<Record<string, unknown>>;
  unresolved_count: 0;
};

export type ContextReconciliationCommit = {
  draft_version_id: string;
  head_version_id: string;
  branch_status: "superseded";
  review_item_id: string | null;
};

export type ContextReconciliationResult =
  components["schemas"]["ContextReconciliationResultResponse"]["data"];

export async function getContextReconciliation(
  workspaceId: string,
  pageId: string,
  branchVersionId: string,
  options: { signal?: AbortSignal } = {},
) {
  const response = await apiRequest<{ data: ContextReconciliationDetail }>(
    `/api/v1/workspaces/${workspaceId}/context/pages/${pageId}/versions/${branchVersionId}/reconciliation`,
    { signal: options.signal },
  );
  return response.data;
}

export async function reconcileContextBranch(
  workspaceId: string,
  pageId: string,
  branchVersionId: string,
  request: ContextReconciliationRequest,
) {
  const response = await apiRequest<{ data: ContextReconciliationResult }>(
    `/api/v1/workspaces/${workspaceId}/context/pages/${pageId}/versions/${branchVersionId}/reconciliation`,
    {
      method: "POST",
      body: JSON.stringify(request),
    },
  );
  return response.data;
}

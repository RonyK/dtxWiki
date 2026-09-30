import { apiRequest } from "./client";
import type { components } from "./types.gen";
import type { ContextHistoryStatus } from "../lib/contextHistory";

export type VersionInfo = {
  version_id: string;
  version_no: number;
  version_status: string;
  history_status: ContextHistoryStatus;
  graph_status: string | null;
  is_visible_head: boolean;
  is_published_head: boolean;
  is_draft_head: boolean;
  is_alternative: boolean;
  is_discarded: boolean;
  parent_version_id: string | null;
  content_hash: string | null;
  created_at: string | null;
};

export type ContextPageSummary = {
  id: string;
  slug: string;
  title: string;
  page_type: string;
  lifecycle_status: string;
  aliases: string[];
  visible_version_id: string | null;
  current_version_id: string | null;
  current_published_version_id: string | null;
  latest_draft_version_id: string | null;
  is_public: boolean;
  updated_at: string;
};

export type PendingReviewInfo = {
  review_item_id: string;
  status: string;
  risk_level: string;
};

export type ContextPageDetail = ContextPageSummary & {
  workspace_id: string;
  body_markdown: string | null;
  version_info: VersionInfo | null;
  tree_path: Array<{ id: string; title: string; section: string }>;
  parent_node_id: string | null;
  children_count: number;
  backlink_count: number;
  wikilink_targets: Array<{
    target_slug: string;
    target_page_id: string | null;
    current_slug: string | null;
  }>;
  warnings: string[];
  // F1: an open review awaiting a decision for this page's draft, surfaced for
  // inline approval on the Context page. pending_draft_warnings lints the draft
  // body so reviewers see its format issues before approving (Sweep-2).
  pending_draft_version_id: string | null;
  pending_draft_warnings: string[];
  pending_review: PendingReviewInfo | null;
  reconciliation_pending_count: number;
  reconciliation_pending_count_truncated: boolean;
  next_reconciliation_version_id: string | null;
  reconciliation_available: boolean;
  published_version_info?: {
    version_id: string;
    version_no: number;
    published_at: string;
  } | null;
};

export type VersionEntry = components["schemas"]["VersionEntry"] & {
  /** Fail-closed capability advertised only for reconcilable branch versions. */
  reconciliation_available?: boolean;
};

export type ChangeSetEntry = {
  change_set_id: string;
  actor_type: string;
  actor_id: string | null;
  reason: string | null;
  status: string;
  risk_level: string;
  created_at: string;
};

export type ReviewDecisionEntry = {
  decision_id: string;
  decision_type: string;
  actor_type: string;
  decision_reason: string | null;
  created_at: string;
};

export type ContextChangeHistory = {
  versions: VersionEntry[];
  changesets: ChangeSetEntry[];
  review_decisions: ReviewDecisionEntry[];
  diffs: Array<Record<string, unknown>> | null;
  next_cursor: string | null;
};

export type ContextChangeHistoryOptions = {
  limit?: number;
  cursor?: string | null;
};

export type ContextPageListOptions = {
  page_type?: string;
  status?: string;
  has_draft?: boolean;
  limit?: number;
  cursor?: string | null;
};

export type ContextPageListResult = {
  data: ContextPageSummary[];
  next_cursor: string | null;
  total: number | null;
  has_more: boolean;
};

export async function listContextPages(
  workspaceId: string,
  options: ContextPageListOptions = {},
): Promise<ContextPageListResult> {
  const query = new URLSearchParams();
  if (options.page_type) query.set("page_type", options.page_type);
  if (options.status) query.set("status", options.status);
  if (options.has_draft !== undefined)
    query.set("has_draft", String(options.has_draft));
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const response = await apiRequest<{
    data: ContextPageSummary[];
    next_cursor: string | null;
    total: number | null;
    has_more: boolean;
  }>(`/api/v1/workspaces/${workspaceId}/context/pages${suffix}`);
  return {
    data: response.data,
    next_cursor: response.next_cursor ?? null,
    total: response.total ?? null,
    has_more: response.has_more ?? false,
  };
}

export async function listContextChanges(
  workspaceId: string,
  pageId: string,
  options: ContextChangeHistoryOptions = {},
) {
  const query = new URLSearchParams({
    target_type: "context_page",
    target_id: pageId,
  });
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  const response = await apiRequest<{ data: ContextChangeHistory }>(
    `/api/v1/workspaces/${workspaceId}/changesets?${query.toString()}`,
  );
  return {
    ...response.data,
    next_cursor: response.data.next_cursor ?? null,
  };
}

// P9.1: the intermediate `/context-pages/{page_id}` route (with its own
// nested `version_info` detail shape) was retired -- reads now go through
// the same canonical `/context/pages/{page_id}` route pages.ts uses, which
// returns the flat ContextPageDetail schema (a PagePayload superset). This
// adapter reshapes that flat response into the nested shape ContextScreen
// already renders, so callers below did not need to be rewritten.
type BackendContextPageDetail = components["schemas"]["ContextPageDetail"];

function toContextPageDetail(raw: BackendContextPageDetail): ContextPageDetail {
  return {
    id: raw.id,
    slug: raw.slug,
    title: raw.title,
    page_type: raw.page_type,
    // Dropped from the consolidated detail schema (still present on list-item
    // ContextPageSummary rows) -- version_status is the closest surviving
    // equivalent. version_info below is always populated, so this value is
    // not actually rendered today.
    lifecycle_status: raw.version_status ?? "draft",
    aliases: raw.aliases ?? [],
    visible_version_id: raw.visible_version_id ?? null,
    current_version_id: raw.current_version_id ?? null,
    current_published_version_id: raw.current_published_version_id ?? null,
    latest_draft_version_id: raw.latest_draft_version_id ?? null,
    is_public: raw.is_public,
    updated_at: raw.updated_at,
    workspace_id: raw.workspace_id,
    body_markdown: raw.body_md,
    version_info: {
      version_id: raw.version_id,
      // Dropped from the consolidated detail schema; the version list
      // (listContextChanges) still carries version_no per entry.
      version_no: 0,
      version_status: raw.version_status ?? "",
      history_status: raw.history_status ?? "current_editing",
      graph_status: raw.graph_status ?? null,
      is_visible_head: raw.is_visible_head,
      is_published_head: raw.is_published_head,
      is_draft_head: raw.is_draft_head,
      is_alternative: raw.is_alternative,
      is_discarded: raw.is_discarded,
      parent_version_id: raw.parent_version_id ?? null,
      content_hash: raw.content_hash,
      created_at: raw.created_at,
    },
    tree_path: (raw.tree_path ?? []).map((entry) => ({
      id: entry.id,
      title: entry.title,
      section: entry.section,
    })),
    parent_node_id: raw.parent_node_id ?? null,
    children_count: raw.children_count,
    backlink_count: raw.backlink_count,
    wikilink_targets: (raw.wikilink_targets ?? []).map((target) => ({
      target_slug: target.target_slug,
      target_page_id: target.target_page_id ?? null,
      current_slug: target.current_slug ?? null,
    })),
    warnings: raw.warnings ?? [],
    pending_draft_version_id: raw.pending_draft_version_id ?? null,
    pending_draft_warnings: raw.pending_draft_warnings ?? [],
    pending_review: raw.pending_review ?? null,
    reconciliation_pending_count: raw.reconciliation_pending_count,
    reconciliation_pending_count_truncated:
      raw.reconciliation_pending_count_truncated,
    next_reconciliation_version_id: raw.next_reconciliation_version_id ?? null,
    reconciliation_available: raw.reconciliation_available,
    published_version_info: raw.published_version_info ?? null,
  };
}

export async function readContextPage(workspaceId: string, pageId: string) {
  const response = await apiRequest<{ data: BackendContextPageDetail }>(
    `/api/v1/workspaces/${workspaceId}/context/pages/${pageId}`,
  );
  return toContextPageDetail(response.data);
}

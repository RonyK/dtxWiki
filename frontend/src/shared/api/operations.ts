import { ApiError, apiRequest, getApiBaseUrl } from "./client";
import type { PagePayload } from "./pages";
import type { components } from "./types.gen";

export type ActivityLane = "pending" | "advisory" | "history";
export type ActivityPeriod = "today" | "yesterday" | "week" | "month" | "all";
export type ActivitySubTab = "live" | "history" | "traces" | "proposals";

// Derived from the generated OpenAPI schema (N4): every constructor in the
// activity service emits actor/target/body/review_item_id/affordances, so the
// schema marks them required. The `target` field IS serialized (an earlier note
// that the backend omitted it was stale — every ActivityItemRead constructor
// sets an ActivityTarget).
export type ActivityItem = components["schemas"]["ActivityItemRead"];
export type ActivityListData = components["schemas"]["ActivityListData"];
export type ActivityFeedEvent = components["schemas"]["ActivityFeedEvent"];
export type ActivityFeedEntry = components["schemas"]["ActivityFeedEntry"];
export type ActivityFeedData = components["schemas"]["ActivityFeedData"];
export type ActivityReadGroupData = components["schemas"]["ActivityReadGroupData"];

export type ActivityFeedFilters = {
  period?: ActivityPeriod;
  kind?: "all" | "workspace" | "system" | "mcp" | "review_decision";
  subtype?: string;
  lane?: ActivityLane;
  group_reads?: boolean;
  limit?: number;
  cursor?: string | null;
};

export type ActivityFilters = {
  period?: ActivityPeriod;
  lane?: ActivityLane;
  sub_tab?: ActivitySubTab;
  // Server-side workspace/system split, applied BEFORE the recency budget so a
  // workspace failed job is not crowded out by newer system audit rows. "mcp"
  // is expressed via sub_tab=traces, so the server kind is only the binary
  // workspace|system split (omit for the unfiltered "all" view).
  kind?: "workspace" | "system" | "review_decision" | "all";
  limit?: number;
  // Opaque cursor for the dedicated lane pages' "Load more" (next_cursor from a
  // prior page). Only the lane pages paginate; the overview omits it.
  cursor?: string | null;
};

export type ReviewDecisionType = "approve" | "reject" | "request_changes";
export type ReviewAudienceKind = "chat_owner" | "requester" | "workspace_editors";
export type ReviewScope = "to_me" | "from_me" | "all";
export type ReviewStatusFilter =
  | "pending"
  | "changes_requested"
  | "approved"
  | "rejected"
  | "resolved";
export type ContextIngressSaveMode =
  | "create_page"
  | "create_child_page"
  | "append_section"
  | "update_draft"
  | "feedback_only";

export type ReviewEvidenceOperation =
  components["schemas"]["ReviewEvidenceOperation"];

// Derived from the generated OpenAPI schema (N4): every field is always
// serialized, so the schema marks them required.
export type ReviewDecision = components["schemas"]["ReviewDecisionPayload"];
export type ReviewEvidence = components["schemas"]["ReviewEvidencePayload"];

// Derived from the generated schema: the backend types both the audience and
// reconciliation affordances, so this type no longer needs a manual overlay.
export type ReviewItem = components["schemas"]["ReviewItemDetail"];
export type ReviewDecisionUndo = components["schemas"]["ReviewDecisionUndo"];
export type ReviewContentTarget = components["schemas"]["ReviewContentTarget"];
export type ReviewItemListData =
  components["schemas"]["ReviewItemListPayload"];

export type ReviewRiskBreakdown =
  components["schemas"]["ReviewStatusRiskBreakdown"];
export type ReviewCounts = components["schemas"]["ReviewCountsPayload"];

export type ReviewBulkDecisionFilters = {
  status?: ReviewStatusFilter | string;
  risk_level?: string;
  scope?: ReviewScope;
  target_page_id?: string;
};

export type ReviewSelection = components["schemas"]["ReviewSelectionPayload"];

export async function createReviewSelection(
  workspaceId: string,
  input: { review_item_ids?: string[]; filters?: ReviewBulkDecisionFilters },
) {
  const response = await apiRequest<{ data: ReviewSelection }>(
    `/api/v1/workspaces/${workspaceId}/reviews/selection`,
    { method: "POST", body: JSON.stringify(input) },
  );
  return response.data;
}

export type ReviewBulkDecisionResult =
  components["schemas"]["ReviewBulkDecisionPayload"];

export type ReviewReconciliationBulkDiscardResult =
  components["schemas"]["ContextReconciliationBulkDiscardResult"];

// Derived from the generated OpenAPI schema (N4): every field is always
// serialized, so the schema marks them required.
export type PolicyCard = components["schemas"]["PolicyCard"] & {
  revision?: string;
};
// Derived from the generated schema like the rest of this file: the backend
// types both the connection state and the client row, so neither needs a
// hand-maintained copy here.
export type SettingsMcpClient =
  components["schemas"]["SettingsMcpClientData"];
export type McpConnectionState = SettingsMcpClient["connection_state"];
type GeneratedSettingsPolicies = components["schemas"]["SettingsPolicies"];
export type SettingsPolicies = Omit<
  GeneratedSettingsPolicies,
  | "workspace"
  | "library"
  | "citation"
  | "context_write"
  | "agent_injection"
  | "review"
  | "lint"
> & {
  workspace: PolicyCard;
  library: PolicyCard;
  citation: PolicyCard;
  context_write: PolicyCard;
  agent_injection: PolicyCard;
  review: PolicyCard;
  lint: PolicyCard;
};

export type McpClientStatus = "active" | "disabled";

export type SettingsPolicyGroup =
  | "workspace"
  | "library"
  | "citation"
  | "context_write"
  | "agent_injection"
  | "review"
  | "lint";

export type SearchScope =
  | "all"
  | "context_page"
  | "context_claim"
  | "library_item"
  | "library_artifact"
  | "feedback"
  | "change"
  | "agent_page";

export type SearchFilters = components["schemas"]["SearchFilters"];

// Derived from the generated OpenAPI schema (types.gen.ts) instead of being
// hand-maintained: the backend always serializes every field, so the schema now
// marks them all required (N4) and these aliases stay in lock-step with the
// contract. total_count is the full pre-truncation match count; next_cursor
// pages through the rest (A5).
export type RankedSearchResult = components["schemas"]["RankedResult"];
export type SearchPayload = components["schemas"]["SearchPayload"];

// Derived from the generated OpenAPI schema (N4): the backend always serializes
// every field these aliases reference, so the schema marks them required.
export type AskSessionRef = components["schemas"]["AskSessionRef"];
export type AskTurnDetail = components["schemas"]["AskTurnDetail"];
export type AskSessionDetail = components["schemas"]["AskSessionDetail"];
export type EnvelopeWarning = components["schemas"]["EnvelopeWarning"];
export type GraphNeighborEntry = components["schemas"]["GraphNeighborEntry"];
export type GraphNeighborsPayload = components["schemas"]["GraphNeighborsPayload"];
export type ContextPageLocation = components["schemas"]["ContextPageLocation"];

export type McpChatArtifactKind =
  | "context_page"
  | "library_item"
  | "agent_page"
  | "context_media_asset";
export type McpChatArtifactVerb =
  | "created"
  | "edited"
  | "cited"
  | "referenced"
  | "search_match";
export type McpChatPeriod = "today" | "week" | "month" | "all";
export type McpChatSort = "last_activity_desc" | "write_count_desc";

// Derived from the generated OpenAPI schema (N4): every field is always
// serialized, so the schema marks them required and these stay in lock-step.
export type McpChatClientRef = components["schemas"]["McpChatClientRef"];
export type McpChatArtifactLink =
  components["schemas"]["McpChatArtifactLinkPayload"];
export type McpChatSessionRef = components["schemas"]["McpChatSessionRef"];
export type McpChatTimelineArtifact =
  components["schemas"]["McpChatTimelineArtifact"];
export type McpChatTimelineItem = components["schemas"]["McpChatTimelineItem"];
export type McpChatEditSummary = components["schemas"]["McpChatEditSummary"];
export type McpChatSessionDiff = components["schemas"]["McpChatSessionDiff"];
export type McpChatPreviewPurgeResult =
  components["schemas"]["McpChatPreviewPurgeResult"];
export type McpChatDetail = components["schemas"]["McpChatDetail"];
export type McpChatListData = components["schemas"]["McpChatListPayload"];
export type McpChatProvenanceEntry =
  components["schemas"]["McpChatProvenanceEntry"];

// Derived from the generated OpenAPI schema (N4): every field is always
// serialized, so the schema marks them required.
export type ContextIngressProposal =
  components["schemas"]["ContextIngressProposalDetail"];

export type ContextIngressAcceptResult =
  components["schemas"]["ContextIngressAcceptResult"];

export type LintFinding = components["schemas"]["LintFindingRead"];
export type LintRuleInfo = components["schemas"]["LintRuleInfo"];

export type LintRun = components["schemas"]["LintRunRead"];

// Derived from the generated OpenAPI schema (N4): every field JobRead sets is
// always serialized, so the schema marks them required.
export type JobStatus = components["schemas"]["JobRead"]["status"];
export type Job = components["schemas"]["JobRead"];
export type JobListData = components["schemas"]["JobListData"];

export type JobFilters = {
  status?: JobStatus;
  job_type?: string;
  problems_only?: boolean;
  limit?: number;
  cursor?: string | null;
};

// Workspace-scoped job visibility (Epic-2 Phase 1b). Not the operator/admin
// screen -- that is adminJobs.ts, a separate cross-workspace surface.
export async function listWorkspaceJobs(
  workspaceId: string,
  filters: JobFilters = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{ data: JobListData }>(
    `/api/v1/workspaces/${workspaceId}/jobs${query}`,
  );
  return response.data;
}

export async function listActivity(
  workspaceId: string,
  filters: ActivityFilters = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{ data: ActivityListData }>(
    `/api/v1/workspaces/${workspaceId}/activity${query}`,
  );
  return response.data;
}

export async function listActivityFeed(
  workspaceId: string,
  filters: ActivityFeedFilters = {},
  signal?: AbortSignal,
): Promise<ActivityFeedData> {
  const response = await apiRequest<{ data: ActivityFeedData }>(
    `/api/v1/workspaces/${workspaceId}/activity/feed${encodeQuery(filters)}`,
    { signal },
  );
  return response.data;
}

export async function getActivityReadGroup(
  workspaceId: string,
  token: string,
  signal?: AbortSignal,
): Promise<ActivityReadGroupData> {
  const response = await apiRequest<{ data: ActivityReadGroupData }>(
    `/api/v1/workspaces/${workspaceId}/activity/read-group${encodeQuery({ token })}`,
    { signal },
  );
  return response.data;
}

// One Context page's own history: publishing and review events, which name the
// page, plus renames and moves, which name its tree node. The workspace feed
// mixes both in with everything else.
export async function listContextPageActivity(
  workspaceId: string,
  pageId: string,
  filters: ActivityFilters = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{ data: ActivityListData }>(
    `/api/v1/workspaces/${workspaceId}/context/pages/${pageId}/activity${query}`,
  );
  return response.data;
}

export async function listReviews(
  workspaceId: string,
  filters: {
    status?: ReviewStatusFilter | string;
    risk_level?: string;
    scope?: ReviewScope;
    limit?: number;
    cursor?: string | null;
    include_deferred_reconciliations?: boolean;
  } = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{ data: ReviewItemListData }>(
    `/api/v1/workspaces/${workspaceId}/reviews${query}`,
  );
  return response.data;
}

export async function getReviewCounts(
  workspaceId: string,
  filters: { risk_level?: string } = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{ data: ReviewCounts }>(
    `/api/v1/workspaces/${workspaceId}/reviews/counts${query}`,
  );
  return response.data;
}

// RD-1: a real review decision row (actor + type + reason), newest first.
export type ReviewDecisionRecentItem =
  components["schemas"]["ReviewDecisionRecentItem"];

export async function listRecentReviewDecisions(
  workspaceId: string,
  filters: { limit?: number } = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{ data: ReviewDecisionRecentItem[] }>(
    `/api/v1/workspaces/${workspaceId}/reviews/decisions/recent${query}`,
  );
  return response.data;
}

export async function getReview(
  workspaceId: string,
  reviewItemId: string,
  signal?: AbortSignal,
) {
  const response = await apiRequest<{ data: ReviewItem }>(
    `/api/v1/workspaces/${workspaceId}/reviews/${reviewItemId}`,
    { signal },
  );
  return response.data;
}

export async function decideReview(
  workspaceId: string,
  reviewItemId: string,
  decision_type: ReviewDecisionType,
  decision_reason?: string | null,
  options: {
    audience_kind?: ReviewAudienceKind;
    decision_id?: string;
    expected_impact_fingerprint?: string | null;
  } = {},
) {
  const response = await apiRequest<{ data: ReviewItem }>(
    `/api/v1/workspaces/${workspaceId}/reviews/${reviewItemId}/decision`,
    {
      method: "POST",
      body: JSON.stringify({
        decision_type,
        decision_reason: decision_reason || null,
        ...(options.decision_id ? { decision_id: options.decision_id } : {}),
        // Reject only: binds the decision to the impact the dialog showed.
        ...(options.expected_impact_fingerprint
          ? { expected_impact_fingerprint: options.expected_impact_fingerprint }
          : {}),
        // Only include audience_kind when routing a request_changes decision;
        // omitting it preserves the server default for approve/reject.
        ...(options.audience_kind
          ? { audience_kind: options.audience_kind }
          : {}),
      }),
    },
  );
  return response.data;
}

export async function undoReviewDecision(
  workspaceId: string,
  reviewItemId: string,
  decisionId: string,
) {
  const response = await apiRequest<{ data: ReviewItem }>(
    `/api/v1/workspaces/${workspaceId}/reviews/${reviewItemId}/decisions/${decisionId}/undo`,
    { method: "POST" },
  );
  return response.data;
}

export async function decideReviewsBulk(
  workspaceId: string,
  request: {
    review_item_ids?: string[] | null;
    filters?: ReviewBulkDecisionFilters | null;
    decision_type: "approve";
    dry_run?: boolean;
    selection_token?: string;
    batch_limit?: number;
  },
) {
  const response = await apiRequest<{ data: ReviewBulkDecisionResult }>(
    `/api/v1/workspaces/${workspaceId}/reviews/decide-bulk`,
    {
      method: "POST",
      body: JSON.stringify(request),
    },
  );
  return response.data;
}

export async function discardReviewReconciliationsBulk(
  workspaceId: string,
  request: { review_item_ids: string[]; reason: string },
) {
  const response = await apiRequest<{
    data: ReviewReconciliationBulkDiscardResult;
  }>(
    `/api/v1/workspaces/${workspaceId}/reviews/reconciliations/discard-bulk`,
    {
      method: "POST",
      body: JSON.stringify(request),
    },
  );
  return response.data;
}

export async function claimReview(workspaceId: string, reviewItemId: string) {
  const response = await apiRequest<{ data: ReviewItem }>(
    `/api/v1/workspaces/${workspaceId}/reviews/${reviewItemId}/claim`,
    { method: "POST" },
  );
  return response.data;
}

export async function releaseReview(workspaceId: string, reviewItemId: string) {
  const response = await apiRequest<{ data: ReviewItem }>(
    `/api/v1/workspaces/${workspaceId}/reviews/${reviewItemId}/release`,
    { method: "POST" },
  );
  return response.data;
}

export async function getSettingsPolicies(
  workspaceId: string,
  page: { limit?: number; cursor?: string | null } = {},
) {
  const query = new URLSearchParams();
  if (page.limit !== undefined) {
    query.set("limit", String(page.limit));
  }
  if (page.cursor) {
    query.set("cursor", page.cursor);
  }
  const suffix = query.size > 0 ? `?${query}` : "";
  const response = await apiRequest<{ data: SettingsPolicies }>(
    `/api/v1/workspaces/${workspaceId}/settings/policies${suffix}`,
  );
  const legacyRevision = "0".repeat(64);
  for (const group of [
    "workspace",
    "library",
    "citation",
    "context_write",
    "agent_injection",
    "review",
    "lint",
  ] as const) {
    const card = response.data[group];
    if (card) card.revision ||= legacyRevision;
  }
  return response.data;
}

export async function updateSettingsPolicy(
  workspaceId: string,
  request: { group: SettingsPolicyGroup; key: string; value: unknown },
) {
  const response = await apiRequest<{ data: SettingsPolicies }>(
    `/api/v1/workspaces/${workspaceId}/settings/policies`,
    {
      method: "PATCH",
      body: JSON.stringify(request),
    },
  );
  return response.data;
}

export async function updateSettingsPolicyBatch(
  workspaceId: string,
  request: {
    group: SettingsPolicyGroup;
    expected_revision: string;
    changes: Record<string, unknown>;
  },
) {
  try {
    const response = await apiRequest<{ data: SettingsPolicies }>(
      `/api/v1/workspaces/${workspaceId}/settings/policies/batch`,
      {
        method: "PATCH",
        body: JSON.stringify(request),
      },
    );
    return response.data;
  } catch (error) {
    // One-release compatibility for pre-batch servers. Current servers always
    // take the atomic path; only an explicit missing route may use legacy
    // single-key writes.
    if (
      !(error instanceof ApiError) ||
      error.status !== 404 ||
      request.group === "library"
    ) {
      throw error;
    }
    let latest: SettingsPolicies | null = null;
    for (const [key, value] of Object.entries(request.changes)) {
      latest = await updateSettingsPolicy(workspaceId, {
        group: request.group,
        key,
        value,
      });
    }
    if (!latest) throw error;
    return latest;
  }
}

export async function updateSettingsMcpClientStatus(
  workspaceId: string,
  clientId: string,
  status: McpClientStatus,
) {
  const response = await apiRequest<{ data: SettingsPolicies }>(
    `/api/v1/workspaces/${workspaceId}/settings/mcp-clients/${clientId}`,
    {
      method: "PATCH",
      body: JSON.stringify({ status }),
    },
  );
  return response.data;
}

export type DeploymentSuggestedCommand =
  components["schemas"]["SuggestedCommand"];

// Mirrors backend.schemas.health.HealthComponents (the readiness probe's
// per-component status).
export type DeploymentHealthComponents =
  components["schemas"]["HealthComponents"];

export type DeploymentStatus = components["schemas"]["DeploymentStatus"];
export type DeploymentUpdateStatus =
  components["schemas"]["DeploymentUpdateStatus"];

// Read-only instance deployment state for the system (server-admin) settings
// tab. Workspace-independent: it describes the whole dtxWiki server. The app
// cannot mutate /etc, so this returns status + copyable `dtxwiki config set`
// commands rather than an editable form.
export async function getDeploymentStatus(signal?: AbortSignal) {
  const response = await apiRequest<{ data: DeploymentStatus }>(
    "/api/v1/admin/deployment",
    { signal },
  );
  return response.data;
}

export async function checkDeploymentUpdate(force: boolean, signal?: AbortSignal) {
  const response = await apiRequest<{ data: DeploymentUpdateStatus }>(
    "/api/v1/admin/deployment/update-check",
    { method: "POST", body: JSON.stringify({ force }), signal },
  );
  return response.data;
}

export async function searchWorkspace(
  workspaceId: string,
  request: {
    query?: string;
    scope?: SearchScope;
    filters?: SearchFilters;
    retrieval_mode?: string;
    limit?: number;
    cursor?: string | null;
  } = {},
  init: RequestInit = {},
) {
  const response = await apiRequest<{ data: SearchPayload }>(
    `/api/v1/workspaces/${workspaceId}/search`,
    {
      ...init,
      method: "POST",
      body: JSON.stringify({
        query: request.query ?? "",
        scope: request.scope ?? "all",
        filters: request.filters ?? {},
        retrieval_mode: request.retrieval_mode ?? "keyword",
        limit: request.limit ?? 20,
        ...(request.cursor ? { cursor: request.cursor } : {}),
      }),
    },
  );
  return response.data;
}

export async function listAskSessions(
  workspaceId: string,
  filters: { limit?: number; cursor?: string | null } = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{
    data: {
      items: AskSessionRef[];
      date_buckets: Array<Record<string, unknown>>;
      next_cursor: string | null;
      total: number | null;
      has_more: boolean;
    };
  }>(`/api/v1/workspaces/${workspaceId}/ask/sessions${query}`);
  return response.data;
}

export async function readAskSession(workspaceId: string, sessionId: string) {
  const response = await apiRequest<{ data: AskSessionDetail }>(
    `/api/v1/workspaces/${workspaceId}/ask/sessions/${sessionId}`,
  );
  return response.data;
}

export async function createAskSession(
  workspaceId: string,
  request: { title?: string; default_topic_tags?: string[] } = {},
) {
  const response = await apiRequest<{ data: AskSessionDetail }>(
    `/api/v1/workspaces/${workspaceId}/ask/sessions`,
    {
      method: "POST",
      body: JSON.stringify({
        title: request.title ?? null,
        default_topic_tags: request.default_topic_tags ?? [],
      }),
    },
  );
  return response.data;
}

export async function createAskTurn(
  workspaceId: string,
  sessionId: string,
  user_message: string,
) {
  const response = await apiRequest<{
    data: AskTurnDetail;
    envelope?: { warnings?: EnvelopeWarning[] };
  }>(
    `/api/v1/workspaces/${workspaceId}/ask/sessions/${sessionId}/turns`,
    {
      method: "POST",
      body: JSON.stringify({ user_message }),
    },
  );
  return {
    data: response.data,
    warnings: response.envelope?.warnings ?? [],
  };
}

export async function getContextPageLocation(pageId: string) {
  const response = await apiRequest<{ data: ContextPageLocation }>(
    `/api/v1/context/pages/${encodeURIComponent(pageId)}/location`,
  );
  return response.data;
}

export async function getContextPageNeighbors(
  workspaceId: string,
  pageId: string,
  format: "json" | "mermaid" = "json",
) {
  const suffix = format === "mermaid" ? "?format=mermaid" : "";
  const response = await apiRequest<{ data: GraphNeighborsPayload }>(
    `/api/v1/workspaces/${workspaceId}/context/pages/${pageId}/neighbors${suffix}`,
  );
  return response.data;
}

export async function getSearchCapabilities(workspaceId: string) {
  const response = await apiRequest<{
    data: { supported_retrieval_modes: string[] };
  }>(`/api/v1/workspaces/${workspaceId}/search/capabilities`);
  return response.data;
}

export async function promoteAskTurn(
  workspaceId: string,
  turnId: string,
  request: {
    save_mode: ContextIngressSaveMode;
    parent_node_id?: string | null;
    page_type: string;
    title?: string | null;
    body_markdown?: string | null;
  },
) {
  const response = await apiRequest<{ data: ContextIngressProposal }>(
    `/api/v1/workspaces/${workspaceId}/ask/turns/${turnId}/promote`,
    {
      method: "POST",
      body: JSON.stringify({
        save_mode: request.save_mode,
        parent_node_id: request.parent_node_id ?? null,
        page_type: request.page_type,
        title: request.title ?? null,
        body_markdown: request.body_markdown ?? null,
        alternatives: [],
      }),
    },
  );
  return response.data;
}

export async function listMcpChats(
  workspaceId: string,
  filters: {
    q?: string;
    client?: string;
    period?: McpChatPeriod;
    writes_only?: boolean;
    archived?: boolean;
    sort?: McpChatSort;
    limit?: number;
    cursor?: string | null;
  } = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{ data: McpChatListData }>(
    `/api/v1/workspaces/${workspaceId}/mcp-chats${query}`,
  );
  return response.data;
}

export async function readMcpChat(workspaceId: string, chatSessionId: string) {
  const response = await apiRequest<{ data: McpChatDetail }>(
    `/api/v1/workspaces/${workspaceId}/mcp-chats/${chatSessionId}`,
  );
  return response.data;
}

export async function updateMcpChatTitle(
  workspaceId: string,
  chatSessionId: string,
  title: string,
) {
  const response = await apiRequest<{ data: McpChatDetail }>(
    `/api/v1/workspaces/${workspaceId}/mcp-chats/${chatSessionId}`,
    {
      method: "PATCH",
      body: JSON.stringify({ title }),
    },
  );
  return response.data;
}

// archived_at is null when the chat is restored via unarchive, so the shared
// DTO widens it to string | null and both calls reuse the same response model.
type McpChatArchiveResultDTO = {
  chat_session_id: string;
  archived_at: string | null;
  audit_event_id: string;
};

export async function archiveMcpChat(workspaceId: string, chatSessionId: string) {
  const response = await apiRequest<{ data: McpChatArchiveResultDTO }>(
    `/api/v1/workspaces/${workspaceId}/mcp-chats/${chatSessionId}`,
    { method: "DELETE" },
  );
  return response.data;
}

export async function unarchiveMcpChat(workspaceId: string, chatSessionId: string) {
  const response = await apiRequest<{ data: McpChatArchiveResultDTO }>(
    `/api/v1/workspaces/${workspaceId}/mcp-chats/${chatSessionId}/unarchive`,
    { method: "POST" },
  );
  return response.data;
}

// Workspace owner only; deletes the stored edit previews of every call in the
// chat and leaves sizes, op kinds and statuses in place.
export async function purgeMcpChatPreviews(
  workspaceId: string,
  chatSessionId: string,
) {
  const response = await apiRequest<{ data: McpChatPreviewPurgeResult }>(
    `/api/v1/workspaces/${workspaceId}/mcp-chats/${chatSessionId}/previews`,
    { method: "DELETE" },
  );
  return response.data;
}

export async function listArtifactProvenance(
  workspaceId: string,
  resourceKind: McpChatArtifactKind,
  resourceId: string,
) {
  const query = encodeQuery({ kind: resourceKind, id: resourceId });
  const response = await apiRequest<{ data: { items: McpChatProvenanceEntry[] } }>(
    `/api/v1/workspaces/${workspaceId}/mcp-chats/resource-provenance${query}`,
  );
  return response.data;
}

export async function acceptContextIngressProposal(
  workspaceId: string,
  proposalId: string,
  request: {
    final_save_mode: ContextIngressSaveMode;
    final_parent_node_id: string;
    final_title: string;
    final_page_type: string;
    target_page_id?: string | null;
    body_markdown?: string | null;
    review_required?: boolean | null;
  },
) {
  const response = await apiRequest<{ data: ContextIngressAcceptResult }>(
    `/api/v1/workspaces/${workspaceId}/context/ingress-proposals/${proposalId}/accept`,
    {
      method: "POST",
      body: JSON.stringify(request),
    },
  );
  return response.data;
}

export async function rejectContextIngressProposal(
  workspaceId: string,
  proposalId: string,
  reason: string,
) {
  const response = await apiRequest<{ data: ContextIngressProposal }>(
    `/api/v1/workspaces/${workspaceId}/context/ingress-proposals/${proposalId}/reject`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
  return response.data;
}

export async function listContextIngressProposals(
  workspaceId: string,
  filters: { status?: string; source_kind?: string; limit?: number } = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{
    data: { proposals: ContextIngressProposal[]; next_cursor: string | null };
  }>(`/api/v1/workspaces/${workspaceId}/context/ingress-proposals${query}`);
  return response.data;
}

export async function listLintFindings(
  workspaceId: string,
  filters: {
    severity?: string;
    rule?: string;
    status?: string;
    limit?: number;
    cursor?: string | null;
    layer?: string;
    finding_id?: string;
    page?: number;
    page_size?: number;
  } = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{
    data: {
      findings: LintFinding[];
      next_cursor: string | null;
      total: number | null;
      has_more: boolean;
      page: number | null;
      page_size: number | null;
      page_count: number | null;
      has_previous: boolean | null;
      has_next: boolean | null;
    };
  }>(`/api/v1/workspaces/${workspaceId}/lint/findings${query}`);
  return response.data;
}

export async function getLintRules(workspaceId: string) {
  const response = await apiRequest<{ data: { rules: LintRuleInfo[] } }>(
    `/api/v1/workspaces/${workspaceId}/lint/rules`,
  );
  return response.data;
}

// F2d: on-demand "Format now" — normalize Markdown server-side (stateless).
export async function formatMarkdown(workspaceId: string, markdown: string) {
  const response = await apiRequest<{ data: { markdown: string } }>(
    `/api/v1/workspaces/${workspaceId}/lint/format`,
    { method: "POST", body: JSON.stringify({ markdown }) },
  );
  return response.data.markdown;
}

export async function createLintRun(
  workspaceId: string,
  request: { lint_scope?: string; trigger_type?: string } = {},
) {
  const response = await apiRequest<{ data: LintRun }>(
    `/api/v1/workspaces/${workspaceId}/lint/runs`,
    {
      method: "POST",
      body: JSON.stringify({
        lint_scope: request.lint_scope ?? "workspace",
        trigger_type: request.trigger_type ?? "manual",
      }),
    },
  );
  return response.data;
}

export interface LintFindingBatchDecisionRequest {
  finding_ids: string[];
  decision: "acknowledge" | "dismiss";
  reason?: string;
}

export interface LintFindingBatchDecisionData {
  updated: LintFinding[];
  failed: { id: string; error_code: string; message: string }[];
}

export async function decideLintFindingsBatch(
  workspaceId: string,
  request: LintFindingBatchDecisionRequest
) {
  const response = await apiRequest<{ data: LintFindingBatchDecisionData }>(
    `/api/v1/workspaces/${workspaceId}/lint/findings/batch/decision`,
    {
      method: "POST",
      body: JSON.stringify(request),
    }
  );
  return response.data;
}

export async function decideLintFinding(
  workspaceId: string,
  findingId: string,
  decision: "acknowledge" | "dismiss" | "fix",
  reason: string | null = null,
) {
  const response = await apiRequest<{ data: LintFinding }>(
    `/api/v1/workspaces/${workspaceId}/lint/findings/${findingId}/decision`,
    {
      method: "POST",
      body: JSON.stringify({ decision, reason }),
    },
  );
  return response.data;
}

export async function getLintRun(workspaceId: string, runId: string) {
  const response = await apiRequest<{ data: LintRun }>(
    `/api/v1/workspaces/${workspaceId}/lint/runs/${runId}`,
  );
  return response.data;
}

export type CoverageGap = components["schemas"]["CoverageGapRead"];

export async function listCoverageGaps(
  workspaceId: string,
  filters: { status?: string; limit?: number } = {},
) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{
    data: { items: CoverageGap[]; has_more: boolean };
  }>(
    `/api/v1/workspaces/${workspaceId}/coverage-gaps${query}`,
  );
  return response.data;
}

// The unauthenticated public reader payload (T16). A dedicated shape that
// physically lacks every internal field (version ids, hashes, tags, actors) so
// no internal metadata can leak — this is NOT a filtered PagePayload. `id` is
// present only to drive the logged-in -> in-app redirect; it grants no access.
// `exposure_scope` is safe crawler-policy state for the shared SPA shell.
export type PublicPagePayload = components["schemas"]["PublicPagePayload"];

// Unauthenticated public page read (T16). Must NOT send a session cookie or
// bearer token — `apiRequest` always sets credentials:"include" (and a token
// when configured), so we use a bare fetch with credentials:"omit" to keep the
// request truly anonymous. 404 (uniform) for private/missing/draft-only pages;
// 422 for a malformed slug. Returns null on 404 so callers render a minimal
// not-found state without leaking why.
export async function readPublicPage(
  workspaceSlug: string,
  slug: string,
): Promise<PublicPagePayload | null> {
  const path = `/api/v1/public/w/${encodeURIComponent(workspaceSlug)}/pages/${encodeURIComponent(slug)}`;
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    method: "GET",
    credentials: "omit",
    headers: { Accept: "application/json" },
  });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, await parsePublicErrorBody(response));
  }
  const body = (await response.json()) as { data: PublicPagePayload };
  return body.data;
}

// Toggle a page's public (anonymously readable) flag (T16). editor+ only; the
// backend also enforces. 422 page_has_no_published_version when publishing a
// page with no published head; 403 when the caller is not editor+.
export async function setPagePublic(
  workspaceId: string,
  pageId: string,
  isPublic: boolean,
  options: {
    acknowledgePublicExposure?: boolean;
    expectedPublishedVersionId?: string | null;
  } = {},
) {
  const response = await apiRequest<{ data: PagePayload }>(
    `/api/v1/workspaces/${workspaceId}/context/pages/${pageId}/publish`,
    {
      method: "POST",
      body: JSON.stringify({
        is_public: isPublic,
        acknowledge_public_exposure:
          options.acknowledgePublicExposure ?? false,
        expected_published_version_id:
          options.expectedPublishedVersionId ?? null,
      }),
    },
  );
  return response.data;
}

async function parsePublicErrorBody(response: Response) {
  const text = await response.text();
  try {
    return JSON.parse(text) as {
      error_code: string;
      message: string;
      request_id: string;
      warnings: [];
    };
  } catch {
    return {
      error_code: "internal_error",
      message: text || `Request failed with ${response.status}`,
      request_id: "",
      warnings: [] as [],
    };
  }
}

function encodeQuery(
  params: Record<string, string | number | boolean | null | undefined>,
) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

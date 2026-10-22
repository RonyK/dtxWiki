import { apiRequest } from "./client";
import type { PageVersionDiff } from "./pages";

export type AgentCategoryEntry = {
  category: string;
  description: string;
  default_injection_policy: string;
};

export type AgentPageSummary = {
  id: string;
  category: string;
  title: string;
  slug: string;
  lifecycle_status: string;
  injection_policy: string;
  priority: number;
  sensitivity: string;
  current_active_version_id: string | null;
  latest_draft_version_id: string | null;
  updated_at: string;
};

export type AgentPageDetail = AgentPageSummary & {
  body_markdown: string | null;
  frontmatter: Record<string, unknown>;
  version_info: {
    version_id: string;
    version_no: number;
    version_status: string;
    graph_status: string | null;
    parent_version_id: string | null;
    content_hash: string | null;
    created_at: string | null;
  } | null;
  tree_path: Array<{ id: string; title: string; section: string }>;
  parent_node_id: string | null;
  children_count: number;
  warnings: string[];
};

export type AgentVersionEntry = {
  version_id: string;
  version_no: number;
  version_status: string;
  actor_type: string;
  actor_display_name: string | null;
  created_at: string | null;
  prior_version_id: string | null;
  actor_id: string | null;
  lines_added: number | null;
  lines_removed: number | null;
  is_current: boolean;
};

export type AgentChangeHistory = {
  versions: AgentVersionEntry[];
  changesets: Array<Record<string, unknown>>;
  review_decisions: Array<Record<string, unknown>>;
  diffs: Array<Record<string, unknown>> | null;
};

export type AgentPageBundleSummary = {
  bundle_id: string;
  status: "generated" | "used" | "expired";
  created_at: string;
  valid_until: string;
  used_at: string | null;
  expired_at: string | null;
  included: boolean;
};

export type AgentPageDraftPayload = {
  base_version_id?: string | null;
  body_markdown: string;
  category: string;
  frontmatter?: Record<string, unknown>;
  parent_node_id?: string | null;
  reason: string;
  sensitivity: "public" | "workspace" | "private" | "secret_candidate";
  slug: string;
  title: string;
};

export type AgentPageDraftResult = {
  agent_page_id: string;
  change_set_id: string;
  draft_version_id: string;
  next_action: "owner_activation_required" | "review_required";
  review_item_id: string | null;
};

export async function listAgentCategories(workspaceId: string) {
  const response = await apiRequest<{ data: AgentCategoryEntry[] }>(
    `/api/v1/workspaces/${workspaceId}/agent/categories`,
  );
  return response.data;
}

export type AgentPageListOptions = {
  category?: string;
  lifecycle_status?: string;
  limit?: number;
  cursor?: string | null;
};

export type AgentPageListResult = {
  data: AgentPageSummary[];
  next_cursor: string | null;
  total: number | null;
  has_more: boolean;
};

export async function listAgentPages(
  workspaceId: string,
  options: AgentPageListOptions = {},
): Promise<AgentPageListResult> {
  const query = new URLSearchParams();
  if (options.category) query.set("category", options.category);
  if (options.lifecycle_status)
    query.set("lifecycle_status", options.lifecycle_status);
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const response = await apiRequest<{
    data: AgentPageSummary[];
    next_cursor: string | null;
    total: number | null;
    has_more: boolean;
  }>(`/api/v1/workspaces/${workspaceId}/agent/pages${suffix}`);
  return {
    data: response.data,
    next_cursor: response.next_cursor ?? null,
    total: response.total ?? null,
    has_more: response.has_more ?? false,
  };
}

export async function createAgentPageDraft(
  workspaceId: string,
  payload: AgentPageDraftPayload,
) {
  const response = await apiRequest<{ data: AgentPageDraftResult }>(
    `/api/v1/workspaces/${workspaceId}/agent/pages`,
    {
      body: JSON.stringify(payload),
      method: "POST",
    },
  );
  return response.data;
}

export async function updateAgentPageDraft(
  workspaceId: string,
  agentPageId: string,
  payload: AgentPageDraftPayload,
) {
  const response = await apiRequest<{ data: AgentPageDraftResult }>(
    `/api/v1/workspaces/${workspaceId}/agent/pages/${agentPageId}/draft`,
    {
      body: JSON.stringify(payload),
      method: "POST",
    },
  );
  return response.data;
}

export async function readAgentPage(
  workspaceId: string,
  agentPageId: string,
  version = "active",
) {
  const query = new URLSearchParams({ version }).toString();
  const response = await apiRequest<{ data: AgentPageDetail }>(
    `/api/v1/workspaces/${workspaceId}/agent/pages/${agentPageId}?${query}`,
  );
  return response.data;
}

export async function listAgentChanges(workspaceId: string, agentPageId: string) {
  const query = new URLSearchParams({
    target_type: "agent_page",
    target_id: agentPageId,
  }).toString();
  const response = await apiRequest<{ data: AgentChangeHistory }>(
    `/api/v1/workspaces/${workspaceId}/changesets?${query}`,
  );
  return response.data;
}

export async function listAgentPageBundles(
  workspaceId: string,
  agentPageId: string,
) {
  const response = await apiRequest<{ data: AgentPageBundleSummary[] }>(
    `/api/v1/workspaces/${workspaceId}/agent/pages/${agentPageId}/bundles`,
  );
  return response.data;
}

export async function diffAgentPageVersions(
  workspaceId: string,
  agentPageId: string,
  fromVersionId: string,
  toVersionId: string,
  options: { signal?: AbortSignal } = {},
) {
  const query = new URLSearchParams({
    from_version_id: fromVersionId,
    to_version_id: toVersionId,
  });
  const response = await apiRequest<{ data: PageVersionDiff }>(
    `/api/v1/workspaces/${workspaceId}/agent/pages/${agentPageId}/diff?${query.toString()}`,
    { signal: options.signal },
  );
  return response.data;
}

export async function restoreAgentPageVersion(
  workspaceId: string,
  agentPageId: string,
  versionId: string,
) {
  const response = await apiRequest<{ data: AgentPageDraftResult }>(
    `/api/v1/workspaces/${workspaceId}/agent/pages/${agentPageId}/restore`,
    {
      method: "POST",
      body: JSON.stringify({ version_id: versionId }),
    },
  );
  return response.data;
}

export async function activateAgentPage(
  workspaceId: string,
  agentPageId: string,
  versionId: string,
  reason: string,
) {
  const response = await apiRequest<{ data: AgentPageDetail }>(
    `/api/v1/workspaces/${workspaceId}/agent/pages/${agentPageId}/activate`,
    {
      method: "POST",
      body: JSON.stringify({ reason, version_id: versionId }),
    },
  );
  return response.data;
}

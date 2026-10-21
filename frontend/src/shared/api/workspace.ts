import { apiRequest } from "./client";
import { translate } from "../i18n";

const SELECTED_WORKSPACE_ID_KEY = "dtxwiki.selectedWorkspaceId";
const SELECTED_WORKSPACE_SLUG_KEY = "dtxwiki.selectedWorkspaceSlug";
const WORKSPACE_LIST_REQUEST_TIMEOUT_MS = 30_000;

export type WorkspaceSummary = {
  id: string;
  slug: string;
  title: string;
  name: string;
  goal: string;
  default_language: string;
  visibility: string;
  join_policy?: string;
  role?: string | null;
  member_count?: number | null;
  // WS-09: distinguishes two shared Personal workspaces (same default
  // title, same initials) in the switcher when viewing someone else's.
  owner_display_name?: string | null;
};

export type WorkspaceListData = {
  workspaces: WorkspaceSummary[];
  current_workspace_id: string | null;
  default_workspace_id?: string | null;
  personal_workspace_id?: string | null;
};

export type WorkspaceListResponse = {
  data: WorkspaceListData;
};

export type WorkspaceCreateInput = {
  title: string;
  slug?: string;
  goal?: string;
  default_language?: string;
  visibility?: string;
};

export type WorkspaceDeleteResponse = {
  data: {
    deleted_workspace_id: string;
  };
};

type WorkspaceSelectionSnapshot = {
  data: WorkspaceListData;
  workspace: WorkspaceSummary | null;
  workspaceId: string | null;
};

let workspaceSelectionSnapshot: WorkspaceSelectionSnapshot | null = null;
let workspaceSessionGeneration = 0;
let workspaceListRequest: {
  generation: number;
  promise: Promise<WorkspaceListResponse>;
} | null = null;

export function invalidateWorkspaceListCache() {
  workspaceSessionGeneration += 1;
  workspaceListRequest = null;
  workspaceSelectionSnapshot = null;
}

export type TreeNodePayload = {
  id: string;
  section: string;
  title: string;
  slug: string | null;
  node_kind: string;
  parent_id: string | null;
  sort_key: string;
  nav_summary?: string | null;
  // render-time heuristic (never stored) — grey "auto" fallback on the card
  nav_summary_auto?: string | null;
  target_kind: string | null;
  target_id: string | null;
  target_meta?: Record<string, string>;
  is_system_node: boolean;
  can_have_children: boolean;
  primary_placement: boolean;
  visibility_status: string;
  children_count: number;
  has_children?: boolean;
  breadcrumb: Array<{
    id: string;
    section: string;
    title: string;
    slug: string | null;
    node_kind: string;
  }>;
};

export type TreeSectionPolicy = {
  allow_user_folder_create: boolean;
  allow_page_as_parent: boolean;
};

export type TreeListData = {
  nodes: TreeNodePayload[];
  policies: Record<string, TreeSectionPolicy>;
};

export type TreeFolderCreateInput = {
  parent_id?: string | null;
  section: "context" | "agent" | "library";
  slug?: string;
  title: string;
  node_kind?: "folder";
};

export type TreeNodeUpdateInput = {
  display_name: string;
  // folder-only routing summary; explicit null clears the stored value
  nav_summary?: string | null;
};

export type TreeNodeMoveInput = {
  new_parent_id: string;
  node_id: string;
  reason?: string | null;
  sort_key?: string | null;
};

export type TreeNodeReorderInput = {
  node_id: string;
  direction: "up" | "down";
};

async function requestWorkspaceList() {
  // This shared GET owns its cancellation: a caller must not abort another
  // caller's deduplicated request. Keep the deadline through JSON body reading,
  // so receiving headers alone cannot leave workspace hydration pending.
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    WORKSPACE_LIST_REQUEST_TIMEOUT_MS,
  );
  try {
    return await apiRequest<WorkspaceListResponse>("/api/v1/workspaces", {
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timeout);
  }
}

export async function listWorkspaces() {
  const generation = workspaceSessionGeneration;
  if (!workspaceListRequest || workspaceListRequest.generation !== generation) {
    const promise = requestWorkspaceList()
      .then((response) => {
        if (workspaceSessionGeneration === generation) {
          cacheWorkspaceSelection(response.data);
        }
        return response;
      })
      .finally(() => {
        if (workspaceListRequest?.promise === promise) {
          workspaceListRequest = null;
        }
      });
    workspaceListRequest = { generation, promise };
  }
  return workspaceListRequest.promise;
}

export async function getCurrentWorkspaceId() {
  const cached = currentWorkspaceSelection();
  if (cached.workspaceId) {
    return cached.workspaceId;
  }
  // The persisted selection is only a hint: it can outlive the session that
  // wrote it (account switch in the same browser, workspace deleted from
  // another device). Resolve it against the fetched workspace list, which
  // falls back and repairs localStorage when the stored id is stale.
  const generation = workspaceSessionGeneration;
  const response = await listWorkspaces();
  if (workspaceSessionGeneration !== generation) {
    // listWorkspaces fences its own cache write, but it still resolves to the
    // caller that started it. Never let that prior-session payload escape or
    // re-enter resolveSelectedWorkspaceId after an authentication boundary.
    return getCurrentWorkspaceId();
  }
  return resolveSelectedWorkspaceId(response.data);
}

export async function getCurrentWorkspaceSlug() {
  const cached = currentWorkspaceSelection();
  if (cached.workspace?.slug) {
    return cached.workspace.slug;
  }
  const generation = workspaceSessionGeneration;
  const response = await listWorkspaces();
  if (workspaceSessionGeneration !== generation) {
    return getCurrentWorkspaceSlug();
  }
  const workspaceId = resolveSelectedWorkspaceId(response.data);
  const workspace = response.data.workspaces.find(
    (candidate) => candidate.id === workspaceId,
  );
  if (!workspace) {
    throw new Error(
      translate(
        "shell:workspace.chooseRequired",
        "Choose a workspace before continuing.",
      ),
    );
  }
  return workspace.slug;
}

export async function createWorkspace(input: WorkspaceCreateInput) {
  const response = await apiRequest<{ data: WorkspaceSummary }>(
    "/api/v1/workspaces",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
  invalidateWorkspaceListCache();
  return response.data;
}

/**
 * Delete a workspace.
 *
 * `purgeContents` is the owner's second confirmation. Without it the backend
 * refuses any workspace that still holds content or operational rows and
 * answers 409 with the counts; the caller shows those, asks again, and re-sends
 * with it set. It is a separate argument rather than a default because the
 * destructive step has to be something a caller opts into explicitly.
 *
 * `purgeGrant` is the single-use token that first 409 also returns
 * (PURGE_AUTHZ_PLAN S-3/S-7): the workspace-delete route is session-only and
 * now additionally requires this proof that the same browser session saw the
 * blocker counts before confirming. Sent as the `X-Purge-Grant` header, never
 * a query parameter -- a query string reaches access logs and referrers.
 */
export async function deleteWorkspace(
  workspaceId: string,
  options: { purgeContents?: boolean; purgeGrant?: string } = {},
) {
  const query = options.purgeContents ? "?purge=true" : "";
  const headers: HeadersInit | undefined = options.purgeGrant
    ? { "X-Purge-Grant": options.purgeGrant }
    : undefined;
  const response = await apiRequest<WorkspaceDeleteResponse>(
    `/api/v1/workspaces/${workspaceId}${query}`,
    { method: "DELETE", headers },
  );
  // A successful delete changes both membership summaries and their landing
  // anchors. Fence any GET /workspaces that began before the mutation so the
  // caller's authoritative refresh cannot reuse or cache its stale payload.
  invalidateWorkspaceListCache();
  if (getSelectedWorkspaceId() === response.data.deleted_workspace_id) {
    setSelectedWorkspaceId(null);
  }
  return response.data;
}

export async function setDefaultWorkspace(workspaceId: string) {
  const response = await apiRequest<{
    data: { default_workspace_id: string };
  }>("/api/v1/users/me/default-workspace", {
    method: "PATCH",
    body: JSON.stringify({ workspace_id: workspaceId }),
  });
  invalidateWorkspaceListCache();
  return response.data;
}

export function getSelectedWorkspaceId() {
  if (typeof window === "undefined") {
    return null;
  }
  return window.localStorage.getItem(SELECTED_WORKSPACE_ID_KEY);
}

export function setSelectedWorkspaceId(workspaceId: string | null) {
  if (typeof window === "undefined") {
    return;
  }
  if (workspaceId) {
    window.localStorage.setItem(SELECTED_WORKSPACE_ID_KEY, workspaceId);
    window.localStorage.removeItem(SELECTED_WORKSPACE_SLUG_KEY);
    updateSnapshotSelection(workspaceId);
    return;
  }
  window.localStorage.removeItem(SELECTED_WORKSPACE_ID_KEY);
  window.localStorage.removeItem(SELECTED_WORKSPACE_SLUG_KEY);
  workspaceSelectionSnapshot = null;
}

export function setSelectedWorkspace(workspace: WorkspaceSummary | null) {
  if (typeof window === "undefined") {
    return;
  }
  if (!workspace) {
    setSelectedWorkspaceId(null);
    return;
  }
  window.localStorage.setItem(SELECTED_WORKSPACE_ID_KEY, workspace.id);
  window.localStorage.setItem(SELECTED_WORKSPACE_SLUG_KEY, workspace.slug);
  updateSnapshotSelection(workspace.id, workspace);
}

export function getSelectedWorkspaceSlug() {
  if (typeof window === "undefined") {
    return null;
  }
  return window.localStorage.getItem(SELECTED_WORKSPACE_SLUG_KEY);
}

export function resolveSelectedWorkspaceId(data: WorkspaceListData) {
  const selected = getSelectedWorkspaceId();
  const selectedWorkspace = selected
    ? data.workspaces.find((workspace) => workspace.id === selected)
    : null;
  if (selectedWorkspace) {
    setSelectedWorkspace(selectedWorkspace);
    return selected;
  }
  const currentWorkspace = data.workspaces.find(
    (workspace) => workspace.id === data.current_workspace_id,
  );
  if (currentWorkspace) {
    setSelectedWorkspace(currentWorkspace);
    return currentWorkspace.id;
  }
  // Some older servers returned the resolved current id without expanding the
  // membership summaries. That id is still an explicit server-side anchor, so
  // data screens may use it; do not persist or present a synthetic workspace
  // summary. A genuinely unanchored multi-membership response has a null
  // current_workspace_id and remains unresolved below.
  if (data.current_workspace_id && data.workspaces.length === 0) {
    setSelectedWorkspace(null);
    return data.current_workspace_id;
  }
  const defaultWorkspace = data.default_workspace_id
    ? data.workspaces.find(
        (workspace) => workspace.id === data.default_workspace_id,
      )
    : null;
  if (defaultWorkspace) {
    setSelectedWorkspace(defaultWorkspace);
    return defaultWorkspace.id;
  }
  const personalWorkspace = data.personal_workspace_id
    ? data.workspaces.find(
        (workspace) => workspace.id === data.personal_workspace_id,
      )
    : null;
  if (personalWorkspace) {
    setSelectedWorkspace(personalWorkspace);
    return personalWorkspace.id;
  }
  const fallback = data.workspaces.length === 1 ? data.workspaces[0] : null;
  setSelectedWorkspace(fallback);
  return fallback?.id ?? null;
}

export function cacheWorkspaceSelection(data: WorkspaceListData) {
  const workspaceId = resolveSelectedWorkspaceId(data);
  const workspace =
    data.workspaces.find((candidate) => candidate.id === workspaceId) ?? null;
  workspaceSelectionSnapshot = { data, workspace, workspaceId };
  return workspaceSelectionSnapshot;
}

export function currentWorkspaceSelection() {
  const selected = getSelectedWorkspaceId();
  if (
    workspaceSelectionSnapshot &&
    workspaceSelectionSnapshot.workspaceId === selected
  ) {
    return workspaceSelectionSnapshot;
  }
  return {
    data: { current_workspace_id: null, workspaces: [] },
    workspace: null,
    workspaceId: null,
  };
}

export function clearPersistedWorkspaceId() {
  // Fence both the selection snapshot and any request started under the prior
  // authenticated identity. Fetch cannot be cancelled reliably here, but a
  // late completion must neither repopulate this session's cache nor dedupe a
  // new account's authoritative workspace request.
  invalidateWorkspaceListCache();
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.removeItem(SELECTED_WORKSPACE_ID_KEY);
  window.localStorage.removeItem(SELECTED_WORKSPACE_SLUG_KEY);
}

function updateSnapshotSelection(
  workspaceId: string,
  knownWorkspace?: WorkspaceSummary,
) {
  if (
    !knownWorkspace &&
    !workspaceSelectionSnapshot?.data.workspaces.some(
      (candidate) => candidate.id === workspaceId,
    )
  ) {
    workspaceSelectionSnapshot = null;
    return;
  }
  if (!workspaceSelectionSnapshot) {
    workspaceSelectionSnapshot = {
      data: {
        current_workspace_id: workspaceId,
        workspaces: knownWorkspace ? [knownWorkspace] : [],
      },
      workspace: knownWorkspace ?? null,
      workspaceId,
    };
    return;
  }
  const workspace =
    knownWorkspace ??
    workspaceSelectionSnapshot.data.workspaces.find(
      (candidate) => candidate.id === workspaceId,
    ) ??
    null;
  workspaceSelectionSnapshot = {
    data: workspaceSelectionSnapshot.data,
    workspace,
    workspaceId,
  };
}

export async function listTree(workspaceId: string, section?: string) {
  return (await listTreeData(workspaceId, section)).nodes;
}

export async function listTreeData(
  workspaceId: string,
  section?: string,
): Promise<TreeListData> {
  const query = section
    ? `?${new URLSearchParams({ section }).toString()}`
    : "";
  const response = await apiRequest<{ data: TreeNodePayload[] | TreeListData }>(
    `/api/v1/workspaces/${workspaceId}/tree${query}`,
  );
  if (Array.isArray(response.data)) {
    return { nodes: response.data, policies: {} };
  }
  return response.data;
}

export async function createTreeFolder(
  workspaceId: string,
  input: TreeFolderCreateInput,
) {
  const response = await apiRequest<{ data: TreeNodePayload }>(
    `/api/v1/workspaces/${workspaceId}/tree/nodes`,
    {
      method: "POST",
      body: JSON.stringify({ ...input, node_kind: "folder" }),
    },
  );
  return response.data;
}

export async function updateTreeNode(
  workspaceId: string,
  nodeId: string,
  input: TreeNodeUpdateInput,
) {
  const response = await apiRequest<{ data: TreeNodePayload }>(
    `/api/v1/workspaces/${workspaceId}/tree/nodes/${nodeId}`,
    {
      method: "PATCH",
      body: JSON.stringify(input),
    },
  );
  return response.data;
}

export async function fetchTreeNodeIndexPreview(
  workspaceId: string,
  nodeId: string,
): Promise<string> {
  const response = await apiRequest<{ data: { markdown: string } }>(
    `/api/v1/workspaces/${workspaceId}/tree/nodes/${nodeId}/index-preview`,
  );
  return response.data.markdown;
}

export async function moveTreeNode(
  workspaceId: string,
  input: TreeNodeMoveInput,
) {
  const response = await apiRequest<{ data: TreeNodePayload }>(
    `/api/v1/workspaces/${workspaceId}/tree/move`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
  return response.data;
}

export async function reorderTreeNode(
  workspaceId: string,
  input: TreeNodeReorderInput,
) {
  const response = await apiRequest<{ data: TreeNodePayload }>(
    `/api/v1/workspaces/${workspaceId}/tree/reorder`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
  return response.data;
}

export async function reorderTreeNodeAt(
  workspaceId: string,
  nodeId: string,
  anchorNodeId: string,
  placement: "before" | "after",
) {
  const response = await apiRequest<{ data: TreeNodePayload }>(
    `/api/v1/workspaces/${workspaceId}/tree/reorder`,
    {
      method: "POST",
      body: JSON.stringify({
        node_id: nodeId,
        anchor_node_id: anchorNodeId,
        placement,
      }),
    },
  );
  return response.data;
}

export async function deleteTreeNode(workspaceId: string, nodeId: string) {
  const response = await apiRequest<{ data: TreeNodePayload }>(
    `/api/v1/workspaces/${workspaceId}/tree/nodes/${nodeId}`,
    { method: "DELETE" },
  );
  return response.data;
}

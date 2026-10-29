import type { TreeNodePayload } from "../api/workspace";

export const TREE_TARGET_PARAM = "target";
export const TREE_EXPANDED_PARAM = "expanded";
export const TREE_VERSION_PARAM = "version";
export const TREE_NEW_FOLDER_PARAM = "newFolder";

type ExpandedDefaultMode = "all" | "active-path" | "roots" | "none";
export type TreeExpansionStorageKey = "library" | "context" | "agent";

const TREE_EXPANSION_STORAGE_PREFIX = "dtxwiki:tree-expanded:";

export function resolveSelectedTargetId<T extends { id: string }>(
  items: T[],
  requestedTargetId: string | null,
): string | null {
  if (requestedTargetId && items.some((item) => item.id === requestedTargetId)) {
    return requestedTargetId;
  }
  return items[0]?.id ?? null;
}

export function resolveExpandedNodeIds(
  nodes: TreeNodePayload[],
  encodedExpanded: string | null,
  activeTargetId?: string | null,
  options: {
    defaultMode?: ExpandedDefaultMode;
    storageKey?: TreeExpansionStorageKey;
    workspaceId?: string;
  } = {},
): string[] {
  const nodeIds = new Set(nodes.map((node) => node.id));
  let baseline: string[] | null = null;
  if (encodedExpanded !== null) {
    baseline = Array.from(
      new Set(
        encodedExpanded
          .split(",")
          .map((value) => value.trim())
          .filter((value) => nodeIds.has(value)),
      ),
    );
  }
  if (baseline === null && options.storageKey && options.workspaceId) {
    const storedNodeIds = readExpandedNodeIdsFromStorage(
      options.storageKey,
      nodeIds,
      options.workspaceId,
    );
    if (storedNodeIds !== null) {
      baseline = storedNodeIds;
    }
  }

  const expanded = new Set(
    baseline ??
      defaultExpandedNodeIds(
        nodes,
        activeTargetId,
        options.defaultMode ?? "all",
      ),
  );
  // URL and persisted values are a baseline, not permission to hide the
  // selected node. Callers may pass either the actor-visible node id or its
  // target id; only that resolved node's ancestor chain is forced open.
  for (const ancestorId of ancestorIdsForTarget(nodes, activeTargetId)) {
    expanded.add(ancestorId);
  }
  return Array.from(expanded);
}

function defaultExpandedNodeIds(
  nodes: TreeNodePayload[],
  activeTargetId: string | null | undefined,
  mode: ExpandedDefaultMode,
) {
  if (mode === "none") {
    return new Set<string>();
  }
  if (mode === "all") {
    return new Set(
      nodes
        .filter((node) => node.children_count > 0)
        .map((node) => node.id),
    );
  }
  const expanded = new Set(
    nodes
      .filter((node) => node.parent_id === null && node.children_count > 0)
      .map((node) => node.id),
  );
  if (mode === "active-path") {
    for (const ancestorId of ancestorIdsForTarget(nodes, activeTargetId)) {
      expanded.add(ancestorId);
    }
  }
  return expanded;
}

export function writeExpandedNodeIdsToStorage(
  storageKey: TreeExpansionStorageKey,
  nodeIds: string[],
  workspaceId: string,
) {
  if (typeof window === "undefined" || !workspaceId) {
    return;
  }
  try {
    window.localStorage.setItem(
      treeExpansionStorageKey(storageKey, workspaceId),
      JSON.stringify(Array.from(new Set(nodeIds))),
    );
  } catch {
    // Browser storage can be disabled; the tree still works without persistence.
  }
}

export function updateTreeSearchParams(
  current: URLSearchParams,
  next: {
    expandedNodeIds?: string[];
    versionId?: string | null;
  },
): URLSearchParams {
  const params = new URLSearchParams(current);
  // Object identity belongs to the typed pathname, never this UI-state query.
  params.delete(TREE_TARGET_PARAM);
  if (next.expandedNodeIds !== undefined) {
    params.set(TREE_EXPANDED_PARAM, next.expandedNodeIds.join(","));
  }
  if (next.versionId !== undefined) {
    if (next.versionId) {
      params.set(TREE_VERSION_PARAM, next.versionId);
    } else {
      params.delete(TREE_VERSION_PARAM);
    }
  }
  return params;
}

function ancestorIdsForTarget(
  nodes: TreeNodePayload[],
  activeTargetId?: string | null,
) {
  if (!activeTargetId) {
    return [];
  }
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const activeNode =
    byId.get(activeTargetId) ??
    nodes.find((node) => node.target_id === activeTargetId);
  const ancestorIds: string[] = [];
  let parentId = activeNode?.parent_id ?? null;
  while (parentId) {
    ancestorIds.push(parentId);
    parentId = byId.get(parentId)?.parent_id ?? null;
  }
  return ancestorIds.reverse();
}

function readExpandedNodeIdsFromStorage(
  storageKey: TreeExpansionStorageKey,
  validNodeIds: Set<string>,
  workspaceId: string,
) {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    // Unscoped legacy entries cannot prove which workspace owns their IDs.
    const raw =
      window.localStorage.getItem(treeExpansionStorageKey(storageKey, workspaceId));
    if (raw === null) {
      return null;
    }
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return null;
    }
    const known = Array.from(
      new Set(
        parsed
          .map((value) => String(value))
          .filter((value) => validNodeIds.has(value)),
      ),
    );
    // A non-empty entry whose ids all belong to another tree (e.g. a stale
    // legacy entry from a different workspace) is no signal at all; let the
    // caller's default expansion apply instead of forcing a collapsed rail.
    if (known.length === 0 && parsed.length > 0) {
      return null;
    }
    return known;
  } catch {
    return null;
  }
}

function treeExpansionStorageKey(storageKey: TreeExpansionStorageKey, workspaceId: string) {
  return `${TREE_EXPANSION_STORAGE_PREFIX}${workspaceId}:${storageKey}`;
}

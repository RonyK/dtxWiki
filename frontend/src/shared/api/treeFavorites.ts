import { apiRequest } from "./client";
import type { TreeNodePayload } from "./workspace";

export type TreeFavoriteScope = { actorId: string; workspaceId: string };
export type FavoriteTargetKind = "context_page" | "library_item" | "agent_page";
type FavoriteValue = { isFavorite: boolean; pending: boolean; revision: number; readSequence: number };
type FavoriteRead = { actorId: string | null; generation: number; revision: number; sequence: number };

// A personal action belongs to the content, including every placement and rail.
// The session fence also prevents a late response from repopulating a new login.
let generation = 0;
let activeActorId: string | null = null;
let revision = 0;
let readSequence = 0;
let snapshot = new Map<string, FavoriteValue>();
const listeners = new Set<() => void>();

function publish(next: Map<string, FavoriteValue>) {
  snapshot = next;
  listeners.forEach((notify) => notify());
}

export function clearTreeFavoriteCache() {
  generation += 1;
  publish(new Map());
}

export function setTreeFavoriteActor(actorId: string | null) {
  if (actorId === activeActorId) return;
  activeActorId = actorId;
  clearTreeFavoriteCache();
}

export function subscribeTreeFavorites(notify: () => void) {
  listeners.add(notify);
  return () => { listeners.delete(notify); };
}

export function getTreeFavoriteSnapshot() {
  return snapshot;
}

export function beginTreeFavoriteRead(): FavoriteRead {
  return { actorId: activeActorId, generation, revision, sequence: ++readSequence };
}

export function reconcileTreeFavorites(workspaceId: string, nodes: TreeNodePayload[], read: FavoriteRead) {
  if (!read.actorId || read.actorId !== activeActorId || read.generation !== generation) return;
  const byTarget = new Map(nodes.filter(favoriteTargetKind).map((node) => [
    JSON.stringify([node.target_kind, node.target_id]), node.is_favorite === true,
  ]));
  let next: Map<string, FavoriteValue> | null = null;
  for (const [key, value] of snapshot) {
    const [cachedActor, cachedWorkspace, kind, id] = JSON.parse(key) as string[];
    const serverValue = byTarget.get(JSON.stringify([kind, id]));
    // A fetch started before mutation settlement may contain the prior value.
    // A slower, older read must not overwrite a newer authoritative response.
    if (cachedActor !== read.actorId || cachedWorkspace !== workspaceId || serverValue === undefined || value.pending ||
      value.revision > read.revision || value.readSequence > read.sequence) continue;
    next ??= new Map(snapshot);
    next.set(key, { ...value, isFavorite: serverValue, readSequence: read.sequence });
  }
  if (next) publish(next);
}

export function favoriteTargetKind(node: TreeNodePayload): FavoriteTargetKind | null {
  if (node.node_kind === "folder" || !node.target_id) return null;
  return node.target_kind === "context_page" ||
    node.target_kind === "library_item" || node.target_kind === "agent_page"
    ? node.target_kind : null;
}

export function treeFavoriteKey(scope: TreeFavoriteScope, node: TreeNodePayload) {
  return JSON.stringify([scope.actorId, scope.workspaceId, node.target_kind, node.target_id]);
}

export async function toggleTreeFavorite(scope: TreeFavoriteScope, node: TreeNodePayload) {
  if (scope.actorId !== activeActorId) return;
  const kind = favoriteTargetKind(node);
  if (!kind || !node.target_id) return;
  const key = treeFavoriteKey(scope, node);
  const previous = snapshot.get(key);
  if (previous?.pending) return;
  const session = generation;
  const isFavorite = !(previous?.isFavorite ?? node.is_favorite ?? false);
  publish(new Map(snapshot).set(key, { isFavorite, pending: true, revision: ++revision, readSequence: 0 }));
  try {
    const response = await apiRequest<{ data: {
      target_kind: FavoriteTargetKind; target_id: string; is_favorite: boolean;
    } }>(
      `/api/v1/workspaces/${encodeURIComponent(scope.workspaceId)}/favorites/${kind}/${encodeURIComponent(node.target_id)}`,
      { method: "PUT", body: JSON.stringify({ is_favorite: isFavorite }) },
    );
    if (generation !== session) return;
    publish(new Map(snapshot).set(key, {
      isFavorite: response.data.is_favorite, pending: false, revision: ++revision, readSequence: 0,
    }));
  } catch (error) {
    if (generation !== session) return;
    const next = new Map(snapshot);
    if (previous) next.set(key, previous);
    else next.delete(key);
    publish(next);
    throw error;
  }
}

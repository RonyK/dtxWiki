/** Map browser state is private to an actor; never clear unrelated app data. */
export const WORKSPACE_GRAPH_STORAGE_PREFIX = "dtxwiki:workspace-graph:";
export const WORKSPACE_GRAPH_SCOPED_PREFIX = `${WORKSPACE_GRAPH_STORAGE_PREFIX}v2:`;
let purgeEpoch = 0;

/** Explicit auth/scope purges invalidate callbacks even before React cleanup. */
export function workspaceGraphStorageEpoch(): number {
  return purgeEpoch;
}

export function workspaceGraphStorageScope(workspaceId: string, actorId: string): string {
  return `${WORKSPACE_GRAPH_SCOPED_PREFIX}${encodeURIComponent(actorId)}:${encodeURIComponent(workspaceId)}:`;
}

export function clearWorkspaceGraphStorage(prefix = WORKSPACE_GRAPH_STORAGE_PREFIX): void {
  // A pending read/save must be invalidated even when there are no keys yet,
  // or the browser refuses storage access. Legacy cleanup is not a purge.
  purgeEpoch += 1;
  removeMapStorage((key) => key.startsWith(prefix));
}

/** Legacy state has no reliable owner and cannot safely migrate to a user. */
export function clearLegacyWorkspaceGraphStorage(): void {
  removeMapStorage((key) => !key.startsWith(WORKSPACE_GRAPH_SCOPED_PREFIX));
}

function removeMapStorage(matches: (key: string) => boolean): void {
  if (typeof window === "undefined") return;
  try {
    const storage = window.sessionStorage;
    const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index));
    for (const key of keys) {
      if (key?.startsWith(WORKSPACE_GRAPH_STORAGE_PREFIX) && matches(key)) storage.removeItem(key);
    }
  } catch {
    // Restricted browser storage must not prevent signing out or rendering.
  }
}

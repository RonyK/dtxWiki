import { useEffect, useSyncExternalStore } from "react";

import {
  getWorkspaceGraphCapability,
  type WorkspaceGraphCapability,
} from "../../shared/api/workspaceGraph";
import { useOptionalAuth } from "../../shared/auth/AuthContext";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { clearWorkspaceGraphState } from "./workspaceGraphModel";

// One capability request per observed workspace authority, shared by navigation, the /graph
// route, and Context's "open in Knowledge Map" link. The server decides; this
// cache only avoids asking three times. Settings invalidates it on save.
type CapabilityAuthority = { actorId: string | null; role: string | null };
type CapabilityEntry = { authority: CapabilityAuthority } & (
  | { status: "loading"; capability: null }
  | { status: "ready"; capability: WorkspaceGraphCapability }
  | { status: "error"; capability: null });

const entries = new Map<string, CapabilityEntry>();
// Keep the observed authority when Settings invalidates a capability. Without
// this fence, a role refresh during revalidation could restore old Map state
// after the Gate has already unmounted its Screen.
const authorities = new Map<string, CapabilityAuthority>();
const listeners = new Set<() => void>();

function sameAuthority(authority: CapabilityAuthority | undefined, actorId: string | null, role: string | null) {
  return authority !== undefined && authority.actorId === actorId && authority.role === role;
}

function notify() {
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function load(workspaceId: string, actorId: string | null, role: string | null) {
  if (sameAuthority(entries.get(workspaceId)?.authority, actorId, role)) {
    return;
  }
  const previous = authorities.get(workspaceId);
  if (previous?.actorId && !sameAuthority(previous, actorId, role)) {
    clearWorkspaceGraphState(workspaceId, previous.actorId);
  }
  const authority = { actorId, role };
  authorities.set(workspaceId, authority);
  const pending: CapabilityEntry = { authority, status: "loading", capability: null };
  entries.set(workspaceId, pending);
  notify();
  getWorkspaceGraphCapability(workspaceId)
    .then((capability) => {
      // An invalidation while this request was in flight wins.
      if (entries.get(workspaceId) !== pending) return;
      entries.set(workspaceId, { authority, status: "ready", capability });
      notify();
    })
    .catch(() => {
      if (entries.get(workspaceId) !== pending) return;
      entries.set(workspaceId, { authority, status: "error", capability: null });
      notify();
    });
}

/** Drop a cached answer (or all of them) so the next reader asks again. */
export function invalidateWorkspaceGraphCapability(workspaceId?: string | null) {
  if (workspaceId) {
    entries.delete(workspaceId);
  } else {
    entries.clear();
  }
  notify();
}

export type WorkspaceGraphCapabilityState = {
  status: "idle" | CapabilityEntry["status"];
  capability: WorkspaceGraphCapability | null;
  /** Fail-closed: true only once the server has said the map is on. */
  enabled: boolean;
};

export function useWorkspaceGraphCapability(
  workspaceId: string | null | undefined,
): WorkspaceGraphCapabilityState {
  const actorId = useOptionalAuth()?.user?.user_id ?? null;
  const workspaceContext = useOptionalWorkspaceContext();
  const selectedWorkspace = workspaceContext?.selectedWorkspace;
  // The canonical route can change before the selected workspace catches up.
  // Resolve that route's known membership instead of inventing a null role
  // from the previous selection and purging the returning workspace's state.
  const workspace = workspaceContext?.workspaces?.find((item) => item.id === workspaceId)
    ?? (selectedWorkspace?.id === workspaceId ? selectedWorkspace : null);
  // A canonical URL identifies its workspace before membership hydration.
  // Wait for that first answer so null -> owner is not treated as a role
  // change that would purge the same actor's saved Map filters on reload.
  const workspaceInitialized = workspaceContext?.initialized !== false;
  const role = workspace?.id === workspaceId ? workspace?.role ?? null : null;
  const entry = useSyncExternalStore(
    subscribe,
    () => {
      const cached = workspaceInitialized && workspaceId ? entries.get(workspaceId) : undefined;
      // A role refresh must hide can_manage in this render, before passive
      // invalidation or a new capability request can run.
      return sameAuthority(cached?.authority, actorId, role) ? cached : undefined;
    },
    () => undefined,
  );

  useEffect(() => {
    if (workspaceInitialized && workspaceId && !entry) {
      load(workspaceId, actorId, role);
    }
  }, [actorId, entry, role, workspaceId, workspaceInitialized]);

  if (!workspaceId) {
    return { status: "idle", capability: null, enabled: false };
  }
  if (!entry) {
    return { status: "loading", capability: null, enabled: false };
  }
  return {
    status: entry.status,
    capability: entry.capability,
    enabled: entry.capability?.workspace_map.effective_enabled === true,
  };
}

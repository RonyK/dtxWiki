import { useEffect, useRef, useSyncExternalStore } from "react";

import { getSettingsPolicies } from "../api/operations";
import { useOptionalAuth } from "../auth/AuthContext";
import { useWorkspaceRoute } from "../lib/workspaceRoutes";
import { useOptionalWorkspaceContext } from "../workspace/WorkspaceContext";

export type LabsPreferences = Readonly<{
  ask: boolean;
  agent: boolean;
  knowledgeMap: boolean;
}>;

const disabled: LabsPreferences = { ask: false, agent: false, knowledgeMap: false };
type Entry = { status: "loading" | "ready" | "error"; preferences: LabsPreferences };
const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());
function onFocus() {
  invalidateWorkspaceLabsPreferences();
}
function onPageShow(event: PageTransitionEvent) {
  if (event.persisted) invalidateWorkspaceLabsPreferences();
}
const subscribe = (listener: () => void) => {
  if (listeners.size === 0) {
    window.addEventListener("focus", onFocus);
    window.addEventListener("pageshow", onPageShow);
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("pageshow", onPageShow);
    }
  };
};

function load(key: string, workspaceId: string) {
  if (entries.has(key)) return;
  const pending: Entry = { status: "loading", preferences: disabled };
  entries.set(key, pending);
  notify();
  void getSettingsPolicies(workspaceId).then((policies) => {
    if (entries.get(key) !== pending) return;
    const policy = policies.labs?.policy;
    entries.set(key, {
      status: "ready",
      preferences: {
        ask: policy?.ask_enabled === true,
        agent: policy?.agent_enabled === true,
        knowledgeMap: policy?.workspace_map_enabled === true,
      },
    });
    notify();
  }).catch(() => {
    if (entries.get(key) !== pending) return;
    entries.set(key, { status: "error", preferences: disabled });
    notify();
  });
}

/** Revalidate mounted readers after a successful workspace settings save. */
export function invalidateWorkspaceLabsPreferences(workspaceId?: string | null) {
  if (workspaceId) {
    for (const key of entries.keys()) {
      if (key.endsWith(`:${workspaceId}`)) entries.delete(key);
    }
  } else entries.clear();
  notify();
}

export function useLabsPreferences() {
  const auth = useOptionalAuth();
  const workspaceContext = useOptionalWorkspaceContext();
  const route = useWorkspaceRoute();
  const workspaceId = route?.workspaceId ?? workspaceContext?.selectedId;
  const selected = workspaceContext?.selectedWorkspace;
  const workspace = workspaceContext?.workspaces?.find((item) => item.id === workspaceId)
    ?? (selected?.id === workspaceId ? selected : null);
  const userId = auth?.status === "authenticated" ? auth.user?.user_id : null;
  // Labs are workspace administration tools. Neither browser storage nor a
  // cached answer from another workspace or membership can grant access.
  const checking = auth?.status === "checking"
    || Boolean(userId && workspaceId && workspaceContext?.initialized === false);
  const eligible = !checking && Boolean(userId && workspaceId && workspace?.role?.toLowerCase() === "owner");
  const key = userId && workspaceId ? `${userId}:${workspaceId}` : null;
  const previousUserId = useRef(userId);
  const entry = useSyncExternalStore(
    subscribe,
    () => eligible && key ? entries.get(key) : undefined,
    () => undefined,
  );
  useEffect(() => {
    const previous = previousUserId.current;
    previousUserId.current = userId;
    if (previous && previous !== userId) {
      for (const cachedKey of entries.keys()) {
        if (cachedKey.startsWith(`${previous}:`)) entries.delete(cachedKey);
      }
      notify();
    }
  }, [userId]);
  useEffect(() => {
    if (eligible && key && workspaceId && !entry) load(key, workspaceId);
    else if (!eligible && key && entries.has(key)) {
      entries.delete(key);
      notify();
    }
  }, [eligible, entry, key, workspaceId]);

  const status = checking ? "loading" : eligible ? entry?.status ?? "loading" : "idle";
  return {
    preferences: eligible ? entry?.preferences ?? disabled : disabled,
    ready: eligible && entry?.status === "ready",
    status,
  };
}

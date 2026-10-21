import { useCallback, useSyncExternalStore } from "react";

import { useOptionalAuth } from "../auth/AuthContext";

type LabsMenu = "ask" | "agent";
type LabsPreferences = Readonly<Record<LabsMenu, boolean>>;

const snapshots: readonly LabsPreferences[] = [
  { ask: false, agent: false },
  { ask: true, agent: false },
  { ask: false, agent: true },
  { ask: true, agent: true },
];

function parseSnapshot(value: string | null): number {
  try {
    const parsed: unknown = JSON.parse(value ?? "null");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return 0;
    const record = parsed as Record<string, unknown>;
    return (record.ask === true ? 1 : 0) | (record.agent === true ? 2 : 0);
  } catch {
    return 0;
  }
}

function createStore(key: string) {
  let lastSnapshot = 0;
  // Keep a successful local change usable when browser storage is denied or full.
  let fallbackSnapshot: number | undefined;
  const listeners = new Set<() => void>();
  const notify = () => listeners.forEach((listener) => listener());

  const getSnapshot = () => {
    if (fallbackSnapshot !== undefined) return fallbackSnapshot;
    try {
      lastSnapshot = parseSnapshot(window.localStorage.getItem(key));
    } catch {
      // Reading localStorage can throw in restricted browser profiles.
    }
    return lastSnapshot;
  };

  const onStorage = (event: StorageEvent) => {
    if (event.key !== null && event.key !== key) return;
    try {
      if (event.storageArea && event.storageArea !== window.localStorage) return;
    } catch {
      return;
    }
    fallbackSnapshot = undefined;
    lastSnapshot = parseSnapshot(event.newValue);
    notify();
  };

  return {
    getSnapshot,
    subscribe(listener: () => void) {
      if (listeners.size === 0) window.addEventListener("storage", onStorage);
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) window.removeEventListener("storage", onStorage);
      };
    },
    setMenuVisible(menu: LabsMenu, visible: boolean) {
      const current = getSnapshot();
      const flag = menu === "ask" ? 1 : 2;
      const next = visible ? current | flag : current & ~flag;
      if (next === current) return;
      lastSnapshot = next;
      try {
        window.localStorage.setItem(key, JSON.stringify(snapshots[next]));
        fallbackSnapshot = undefined;
      } catch {
        fallbackSnapshot = next;
      }
      notify();
    },
  };
}

const stores = new Map<string, ReturnType<typeof createStore>>();
const emptySubscribe = () => () => {};
const emptySnapshot = () => 0;

function getStore(userId: string) {
  const key = `dtxwiki:labs:${userId}`;
  let store = stores.get(key);
  if (!store) {
    store = createStore(key);
    stores.set(key, store);
  }
  return store;
}

export function useLabsPreferences() {
  const auth = useOptionalAuth();
  const userId = auth?.status === "authenticated" ? auth.user?.user_id : undefined;
  const store = userId ? getStore(userId) : null;
  // Selecting the account's store during render avoids showing or persisting the
  // previous account's preferences while an effect catches up after sign-in.
  const snapshot = useSyncExternalStore(
    store?.subscribe ?? emptySubscribe,
    store?.getSnapshot ?? emptySnapshot,
    emptySnapshot,
  );
  const setMenuVisible = useCallback((menu: LabsMenu, visible: boolean) => {
    store?.setMenuVisible(menu, visible);
  }, [store]);

  return { preferences: snapshots[snapshot], setMenuVisible, ready: store !== null };
}

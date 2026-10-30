import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { listInbox, type InboxScope } from "../api/inbox";
import type { WorkspaceSummary } from "../api/workspace";
import { inboxScopeOptions } from "./inboxScopes";

const INBOX_CHANGED_EVENT = "dtxwiki:inbox-changed";

// Tell the unread indicator to look again, after the user changed a mark.
export function notifyInboxChanged() {
  window.dispatchEvent(new Event(INBOX_CHANGED_EVENT));
}

async function scopeHasUnread(scope: InboxScope, isCurrent: () => boolean) {
  const visited = new Set<string>();
  let cursor: string | undefined;
  while (isCurrent()) {
    const page = await listInbox(scope, { state: "unread", limit: 1, cursor });
    if (!isCurrent()) return false;
    if (page.items.length > 0 || page.status_items.length > 0) return true;
    // Resolver-hidden rows can fill a scan without exhausting the stream.
    const next = page.next_cursor;
    if (!next) return false;
    if (visited.has(next)) throw new Error("Inbox pagination repeated a cursor");
    visited.add(next);
    cursor = next;
  }
  return false;
}

// Whether any inbox open in this context has an unread item, as found by
// asking each for one unread item, following continuation pages when needed.
// It checks on mount, workspace changes, window focus and mark changes;
// there is no polling.
export function useInboxUnread(workspace: WorkspaceSummary | null, enabled: boolean) {
  const { t } = useTranslation("inbox");
  const [unread, setUnread] = useState(false);
  const requestRef = useRef(0);
  const scopeKeys = enabled
    ? inboxScopeOptions({ workspace, t })
        .map((option) => option.scope.basePath)
        .join("|")
    : "";

  useEffect(() => {
    if (!enabled) {
      setUnread(false);
      return;
    }
    const scopes = inboxScopeOptions({ workspace, t }).map((option) => option.scope);
    const check = async () => {
      const request = ++requestRef.current;
      const isCurrent = () => request === requestRef.current;
      const results = await Promise.allSettled(
        scopes.map((scope) => scopeHasUnread(scope, isCurrent)),
      );
      if (!isCurrent()) return;
      setUnread(
        results.some((result) => result.status === "fulfilled" && result.value),
      );
    };
    void check();
    const recheck = () => void check();
    window.addEventListener("focus", recheck);
    window.addEventListener(INBOX_CHANGED_EVENT, recheck);
    return () => {
      requestRef.current += 1;
      window.removeEventListener("focus", recheck);
      window.removeEventListener(INBOX_CHANGED_EVENT, recheck);
    };
    // scopeKeys identifies the scopes; the option objects are rebuilt each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKeys, enabled]);

  return unread;
}

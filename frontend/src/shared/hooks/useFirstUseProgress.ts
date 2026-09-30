import { useEffect, useState } from "react";

import { listContextPages } from "../api/context";
import { listLibraryItems } from "../api/library";
import { listAskSessions, listRecentReviewDecisions } from "../api/operations";
import { getCurrentWorkspaceId } from "../api/workspace";
import type { FirstUseProgress } from "../components/FirstUseChecklist";
import { useOptionalWorkspaceContext } from "../workspace/WorkspaceContext";

export function useFirstUseProgress(): FirstUseProgress {
  const workspaceContext = useOptionalWorkspaceContext();
  const selectedWorkspaceId = workspaceContext?.selectedWorkspace?.id;
  const [progress, setProgress] = useState<FirstUseProgress>({});

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const workspaceId = selectedWorkspaceId ?? await getCurrentWorkspaceId();
        if (!workspaceId) return;
        const [library, context, ask, review] = await Promise.all([
          listLibraryItems(workspaceId, { limit: 1 }),
          listContextPages(workspaceId, { limit: 1 }),
          listAskSessions(workspaceId, { limit: 1 }),
          listRecentReviewDecisions(workspaceId, { limit: 1 }),
        ]);
        if (!cancelled) {
          setProgress({
            library: library.data.length > 0,
            context: context.data.length > 0,
            ask: ask.items.length > 0,
            review: review.length > 0,
          });
        }
      } catch {
        if (!cancelled) setProgress({});
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedWorkspaceId]);

  return progress;
}

import { useEffect, useState } from "react";

import { listContextPages } from "../api/context";
import { listLibraryItems } from "../api/library";
import { listRecentReviewDecisions } from "../api/operations";
import { useWorkspaceRoute } from "../lib/workspaceRoutes";
import type { FirstUseProgress } from "../components/FirstUseChecklist";

export function useFirstUseProgress(): FirstUseProgress {
  const selectedWorkspaceId = useWorkspaceRoute()?.workspaceId;
  const [progress, setProgress] = useState<FirstUseProgress>({});

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const workspaceId = selectedWorkspaceId;
        if (!workspaceId) return;
        const [library, context, review] = await Promise.all([
          listLibraryItems(workspaceId, { limit: 1 }),
          listContextPages(workspaceId, { limit: 1 }),
          listRecentReviewDecisions(workspaceId, { limit: 1 }),
        ]);
        if (!cancelled) {
          setProgress({
            library: library.data.length > 0,
            context: context.data.length > 0,
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

import { useEffect, useState } from "react";

import { listContextPages } from "../api/context";
import { listLibraryItems } from "../api/library";
import { listRecentReviewDecisions } from "../api/operations";
import { useWorkspaceRoute } from "../lib/workspaceRoutes";
import type { FirstUseProgress } from "../components/FirstUseChecklist";

export function useFirstUseProgress(): FirstUseProgress {
  const selectedWorkspaceId = useWorkspaceRoute()?.workspaceId;
  const [state, setState] = useState<{ workspaceId: string | null; progress: FirstUseProgress }>({
    workspaceId: null,
    progress: {},
  });

  useEffect(() => {
    let cancelled = false;
    setState({ workspaceId: selectedWorkspaceId ?? null, progress: {} });
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
          setState({
            workspaceId,
            progress: {
              library: library.data.length > 0,
              context: context.data.length > 0,
              review: review.length > 0,
            },
          });
        }
      } catch {
        if (!cancelled) setState({ workspaceId: selectedWorkspaceId ?? null, progress: {} });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedWorkspaceId]);

  return state.workspaceId === (selectedWorkspaceId ?? null) ? state.progress : {};
}

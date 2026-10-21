import { useCallback } from "react";

import { ApiError } from "../api/client";
import { getActivityReadGroup } from "../api/operations";
import type { ActivityFeedEvent, ActivityGroupLoader } from "../lib/activityFeed";

/**
 * Loads one read group's members and hands the server's visible events to
 * `reconcile`, which replaces the cached entry and counts in place. A changed
 * membership does not reload: that would re-snapshot the feed, collapse the
 * group the reader just opened and drop every loaded page. Only an invalid
 * token resets the list.
 */
export function useActivityGroupLoader(
  workspaceId: string | null | undefined,
  reconcile: (token: string, events: ActivityFeedEvent[]) => void,
  reload: () => void,
): ActivityGroupLoader {
  return useCallback(async (token: string) => {
    if (!workspaceId) throw new Error("Workspace not found");
    try {
      const group = await getActivityReadGroup(workspaceId, token);
      reconcile(token, group.events);
      return group.events;
    } catch (error) {
      if (error instanceof ApiError && error.body.error_code === "invalid_activity_token") reload();
      throw error;
    }
  }, [workspaceId, reconcile, reload]);
}

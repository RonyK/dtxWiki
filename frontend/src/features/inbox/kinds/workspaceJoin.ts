import { UserPlus } from "lucide-react";

import type { InboxRenderer } from "../inboxRenderers";
import { inboxVisibleUntil } from "../inboxVisibility";

// A join request; the title is the workspace, params carry its status and
// the requester's name.
export const workspaceJoinRenderer: InboxRenderer = {
  title: (item, t) => {
    const workspace = item.display?.title ?? t("kinds.workspace_join.untitled");
    const requester = String(item.display?.params?.requester ?? "");
    switch (item.display?.params?.status) {
      case "approved":
        return t("kinds.workspace_join.approved", { workspace });
      case "rejected":
        return t("kinds.workspace_join.rejected", { workspace });
      default:
        return t("kinds.workspace_join.pending", { requester, workspace });
    }
  },
  removal: (item, t) => {
    if (item.display?.params?.status === "pending") return t("kinds.workspace_join.pendingRemoval");
    const date = inboxVisibleUntil(item);
    return date ? t("kinds.workspace_join.resultRemoval", { date }) : null;
  },
  icon: UserPlus,
};

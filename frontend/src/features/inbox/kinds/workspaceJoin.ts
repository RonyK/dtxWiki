import { UserPlus } from "lucide-react";

import type { InboxRenderer } from "../inboxRenderers";

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
  icon: UserPlus,
};

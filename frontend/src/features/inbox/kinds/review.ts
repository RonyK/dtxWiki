import { ClipboardCheck, FileCheck2 } from "lucide-react";

import type { InboxRenderer } from "../inboxRenderers";

// A review the viewer can decide now; params.state is "pending" or
// "changes_requested".
export const reviewAttentionRenderer: InboxRenderer = {
  title: (item, t) => item.display?.title ?? t("kinds.review.untitled"),
  summary: (item, t) =>
    item.display?.params?.state === "changes_requested"
      ? t("kinds.review_attention.changesRequested")
      : t("kinds.review_attention.pending"),
  icon: ClipboardCheck,
};

// A person approved or rejected the viewer's change; params.decision.
export const reviewResultRenderer: InboxRenderer = {
  title: (item, t) => item.display?.title ?? t("kinds.review.untitled"),
  summary: (item, t) =>
    item.display?.params?.decision === "reject"
      ? t("kinds.review_result.rejected")
      : t("kinds.review_result.approved"),
  icon: FileCheck2,
};

import { KeyRound } from "lucide-react";

import type { InboxRenderer } from "../inboxRenderers";

// An Access Key about to expire or just expired; params.stage and
// params.expires_at.
export const accessKeyExpiryRenderer: InboxRenderer = {
  title: (item, t) => item.display?.title ?? t("kinds.access_key_expiry.untitled"),
  summary: (item, t) => {
    const raw = item.display?.params?.expires_at;
    const date = typeof raw === "string" ? new Date(raw) : null;
    if (!date) return null;
    return item.display?.params?.stage === "expired"
      ? t("kinds.access_key_expiry.expired", { date })
      : t("kinds.access_key_expiry.expiring", { date });
  },
  icon: KeyRound,
};

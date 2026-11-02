import { AlertTriangle, Check, ClipboardCheck, ClipboardList, Clock3, CornerUpLeft, Eye, Pencil, Plus, RotateCcw, Search, Send, Trash2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ActivityAction } from "../lib/activityFeed";

const ICONS = { edited: Pencil, read: Eye, search: Search, logged: ClipboardList, created: Plus, published: Check, restored: RotateCcw, deleted: Trash2, reviewed: ClipboardCheck, requested: Send, approved: Check, changes_requested: CornerUpLeft, rejected: X, failed: AlertTriangle, partial: AlertTriangle, accepted: Clock3 };
// Decision outcomes read exactly as the Review screen names them.
const LABEL_KEYS: Partial<Record<ActivityAction, string>> = {
  approved: "review:labels.decisionType.approve",
  changes_requested: "review:labels.decisionType.request_changes",
  rejected: "review:labels.decisionType.reject",
};

/** Domain codes choose tone and icon; translated copy never participates in classification. */
export function ActivityActionBadge({ action, count = 1 }: { action: ActivityAction; count?: number }) {
  const { t } = useTranslation("activity");
  const Icon = ICONS[action];
  return (
    <span className="activity-action-badge" data-action={action}>
      <Icon aria-hidden="true" size={12} />
      <span>{t(LABEL_KEYS[action] ?? `feed.action.${action}`)}{count > 1 ? ` · ${t("feed.repeatCount", { count })}` : ""}</span>
    </span>
  );
}

import { AlertTriangle, Check, ClipboardCheck, ClipboardList, Clock3, Eye, Pencil, Plus, RotateCcw, Search, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ActivityAction } from "../lib/activityFeed";

const ICONS = { edited: Pencil, read: Eye, search: Search, logged: ClipboardList, created: Plus, published: Check, restored: RotateCcw, deleted: Trash2, reviewed: ClipboardCheck, failed: AlertTriangle, partial: AlertTriangle, accepted: Clock3 };

/** Domain codes choose tone and icon; translated copy never participates in classification. */
export function ActivityActionBadge({ action, count = 1 }: { action: ActivityAction; count?: number }) {
  const { t } = useTranslation("activity");
  const Icon = ICONS[action];
  return (
    <span className="activity-action-badge" data-action={action}>
      <Icon aria-hidden="true" size={12} />
      <span>{t(`feed.action.${action}`)}{count > 1 ? ` · ${t("feed.repeatCount", { count })}` : ""}</span>
    </span>
  );
}

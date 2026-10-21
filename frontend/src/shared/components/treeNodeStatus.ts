import type { TFunction } from "i18next";

import type { TreeNodePayload } from "../api/workspace";
import { favoriteTargetKind } from "../api/treeFavorites";
import { formatRelativeTimestamp, normalizeTimestamp } from "../lib/datetime";

export const TREE_RECENT_CHANGE_MS = 72 * 60 * 60 * 1000;

export function contentChangedAt(node: TreeNodePayload) {
  return node.content_changed_at ? Date.parse(normalizeTimestamp(node.content_changed_at)) : NaN;
}

export function treeNodeStatus(node: TreeNodePayload, now: number) {
  if (!favoriteTargetKind(node)) return null;
  if (node.review_status === "pending" || node.review_status === "changes_requested") return "review";
  const changed = contentChangedAt(node);
  return Number.isFinite(changed) && now - changed < TREE_RECENT_CHANGE_MS ? "recent" : "normal";
}

export function treeNodeStatusDescription(
  node: TreeNodePayload, now: number, locale: string, t: TFunction<"tree">,
) {
  const status = treeNodeStatus(node, now);
  if (!status) return "";
  const reviewing = status === "review";
  const timestamp = reviewing ? node.review_requested_at : node.content_changed_at;
  const relative = timestamp ? formatRelativeTimestamp(timestamp, locale, { now, alwaysRelative: true }) : "—";
  const state = t(reviewing
    ? node.review_status === "changes_requested" ? "status.changesRequested" : "status.pending"
    : status === "recent" ? "status.recent" : "status.normal");
  const time = relative === "—"
    ? t(reviewing ? "status.noReviewTime" : "status.noContentTime")
    : reviewing ? t("status.reviewTime", { time: relative })
      : status === "normal" ? t("status.contentTime", { time: relative }) : relative;
  return `${state} · ${time}${node.is_favorite ? ` · ${t("favorite.mine")}` : ""}`;
}

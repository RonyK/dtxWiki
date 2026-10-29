import { workspacePath } from "./workspaceRoutes";
import type { ActivityFeedEntry, ActivityFeedEvent, ActivityLane, ActivityPeriod } from "../api/operations";
export type { ActivityFeedEntry, ActivityFeedEvent } from "../api/operations";
import { i18n } from "../i18n";
import { ACTIVITY_SUBTYPES, activityTargetHref, isMcpActivityItem, presentActivityItem } from "./activityLabels";

export type ActivityKind = "all" | "workspace" | "mcp" | "system";
export type ActivityQuery = {
  period: ActivityPeriod;
  kind: ActivityKind;
  subtype: string;
  group_reads: boolean;
};
export type ActivityGroupLoader = (token: string) => Promise<ActivityFeedEvent[]>;
export type ActivityAction = "edited" | "read" | "search" | "logged" | "created" | "published" | "restored" | "deleted" | "reviewed" | "failed" | "partial" | "accepted";

const PERIODS: readonly string[] = ["today", "yesterday", "week", "month", "all"];
const KINDS: readonly string[] = ["all", "workspace", "mcp", "system"];
export const DEFAULT_ACTIVITY_QUERY: ActivityQuery = { period: "all", kind: "all", subtype: "all", group_reads: true };

export function parseActivityQuery(params: URLSearchParams, defaults: Partial<ActivityQuery> = {}): ActivityQuery {
  const fallback = { ...DEFAULT_ACTIVITY_QUERY, ...defaults };
  const rawKind = params.get("kind");
  const kind = (rawKind && KINDS.includes(rawKind) ? rawKind : fallback.kind) as ActivityKind;
  const period = params.get("period");
  const rawSubtype = params.get("subtype") ?? fallback.subtype;
  const subtype = rawSubtype === "all" || (Object.hasOwn(ACTIVITY_SUBTYPES, kind) && ACTIVITY_SUBTYPES[kind].includes(rawSubtype))
    ? rawSubtype : "all";
  return {
    period: (period && PERIODS.includes(period) ? period : fallback.period) as ActivityPeriod,
    kind, subtype,
    group_reads: params.has("group_reads") ? params.get("group_reads") !== "0" : fallback.group_reads,
  };
}

/** Every list transition retains the complete scope; cursors never travel between scopes. */
export function activityQueryParams(query: ActivityQuery): URLSearchParams {
  return new URLSearchParams({ period: query.period, kind: query.kind, subtype: query.subtype, group_reads: query.group_reads ? "1" : "0" });
}
export function activityHref(query: ActivityQuery, lane: ActivityLane | undefined, workspaceId: string): string {
  return `${workspacePath(workspaceId, `activity${lane ? `/${lane}` : ""}`)}?${activityQueryParams(query)}`;
}
export function activityEntryCounts(entries: readonly ActivityFeedEntry[]) {
  return { returned_entry_count: entries.length, represented_event_count: entries.reduce((sum, entry) => sum + entry.event_count, 0) };
}
export function activityCountLabel(entryCount: number, eventCount: number): string {
  return i18n.t("activity:feed.count", { count: entryCount, events: eventCount, entryUnit: i18n.t("activity:feed.entryUnit", { count: entryCount }), eventUnit: i18n.t("activity:feed.originalEventUnit", { count: eventCount }) });
}

const ACTION_BY_OPERATION: Record<string, ActivityAction> = {
  read: "read", search: "search", edit: "edited", create: "created", publish: "published", restore: "restored",
  delete: "deleted", review: "reviewed",
};
export function isMcpExecution(event: ActivityFeedEvent): boolean {
  return event.target?.target_kind === "mcp_tool_call" || Boolean(event.tool_name);
}

const FAILURE_RESULTS = new Set(["failed", "conflict", "rejected_by_policy", "rejected", "denied", "error", "cancelled"]);
export function activityAction(event: ActivityFeedEvent): ActivityAction {
  const result = event.result_status;
  if (result && FAILURE_RESULTS.has(result)) return "failed";
  if (result === "partial") return "partial";
  if (result === "accepted" || result === "needs_review") return "accepted";
  // An unknown explicit outcome must never be rendered as successful editing/creation.
  if (result && result !== "succeeded") return "logged";
  if (isMcpExecution(event) && result !== "succeeded") return "logged";
  const operation = event.operation;
  if (operation) {
    return Object.hasOwn(ACTION_BY_OPERATION, operation) ? ACTION_BY_OPERATION[operation] : "logged";
  }
  // Compatibility for retained, pre-projection fixtures; only known audit action suffixes.
  const code = event.event_key ?? event.body ?? "";
  if (/^(?:[a-z][a-z0-9_]*\.)+(?:failed|rejected|denied)$/.test(code)) return "failed";
  const suffix = /^(?:[a-z][a-z0-9_]*\.)+(update|edit|create|register|publish|restore)$/.exec(code)?.[1];
  const legacy = { update: "edited", edit: "edited", create: "created", register: "created", publish: "published", restore: "restored" } as const;
  return suffix && Object.hasOwn(legacy, suffix) ? legacy[suffix as keyof typeof legacy] : "logged";
}

const MCP_OPERATION_KEYS: Record<string, string> = {
  read: "read", search: "search", edit: "edit", create: "create", review: "review", publish: "publish", restore: "restore", delete: "delete", cite: "cite", guide: "guide",
};
const RESULT_KEYS: Record<string, string> = {
  failed: "failed", conflict: "conflict", rejected_by_policy: "rejected_by_policy", rejected: "failed", denied: "rejected_by_policy", error: "failed", cancelled: "failed", partial: "partial", accepted: "accepted", needs_review: "accepted",
};
export function presentActivityFeedEvent(event: ActivityFeedEvent, count = 1) {
  const action = activityAction(event);
  const pendingReview = event.lane === "pending" && Boolean(event.review_item_id);
  let title: string;
  let body: string;
  if (isMcpExecution(event)) {
    const resultKey = event.result_status && Object.hasOwn(RESULT_KEYS, event.result_status) ? RESULT_KEYS[event.result_status] : null;
    const operationKey = event.operation && Object.hasOwn(MCP_OPERATION_KEYS, event.operation) ? MCP_OPERATION_KEYS[event.operation] : null;
    const verifiedTarget = event.navigation?.title;
    const knownSuccess = event.result_status === "succeeded";
    const target = verifiedTarget ?? i18n.t("activity:feed.resource");
    const lastCharacter = target.trim().charCodeAt(target.trim().length - 1);
    const particle = lastCharacter >= 0xac00 && lastCharacter <= 0xd7a3 && (lastCharacter - 0xac00) % 28 !== 0 ? "을" : "를";
    title = resultKey
      ? i18n.t(`activity:feed.mcpResults.${resultKey}`)
      : knownSuccess && operationKey
        ? i18n.t(`activity:feed.mcpOperations.${operationKey}`, { target, particle, count })
        : i18n.t("activity:feed.mcpFallback");
    body = "";
  } else {
    ({ title, body } = presentActivityItem(event));
    const targetTitle = event.navigation?.title ?? event.target_title;
    if (event.event_key && targetTitle && targetTitle !== title) {
      body = event.details && body ? body : title;
      title = targetTitle;
    }
  }
  const kind = event.navigation?.target_kind ?? event.target?.target_kind ?? "";
  const typeKeys: Record<string, string> = {
    context_page: "context", agent_page: "agent", source: "library", library_item: "library", mcp_tool_call: "mcp", workspace_tree_node: "tree", change_set: "review", job: "job",
  };
  const typeKey = pendingReview ? "review" : Object.hasOwn(typeKeys, kind) ? typeKeys[kind] : isMcpActivityItem(event) ? "mcp" : "activity";
  return { title, body: body === title ? "" : body, action, pendingReview, typeKey, typeLabel: i18n.t(`activity:feed.types.${typeKey}`) };
}

/** Accept only exact, same-app resource routes from the permission-filtered projection. */
export function activityNavigation(event: ActivityFeedEvent, query: ActivityQuery, workspaceId: string) {
  const pendingReview = event.lane === "pending" && Boolean(event.review_item_id);
  if (event.review_item_id) {
    return { href: workspacePath(workspaceId, `review/${encodeURIComponent(event.review_item_id)}`), fallbackHref: null, pendingReview };
  }
  const navigation = event.navigation;
  if (navigation) {
    const route = activityTargetHref({ ...event, target: { target_kind: navigation.target_kind, target_id: navigation.target_id } }, workspaceId);
    if (route && route === navigation.href) return { href: route, fallbackHref: null, pendingReview };
  }
  // Legacy resource targets have already passed server visibility checks. Tool-call IDs never route to Chats.
  const href = activityTargetHref(event, workspaceId);
  if (href) return { href, fallbackHref: null, pendingReview };
  const fallbackQuery = isMcpActivityItem(event)
    ? parseActivityQuery(activityQueryParams({ ...query, kind: "mcp" })) : query;
  return { href: null, fallbackHref: activityHref(fallbackQuery, event.lane, workspaceId), pendingReview };
}

/** A group lookup rechecks visibility. Replace the cached representative and
 * counts exclusively from the events the server still permits the reader to see. */
export function reconcileActivityReadGroup(entries: readonly ActivityFeedEntry[], token: string, events: readonly ActivityFeedEvent[]): ActivityFeedEntry[] {
  const visible = [...events].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id.localeCompare(a.id));
  return entries.flatMap(entry => {
    if (entry.group_token !== token) return [entry];
    if (visible.length === 0) return [];
    const latest = visible[0];
    return [{ ...entry, entry_type: visible.length === 1 ? "event" as const : "read_group" as const,
      id: visible.length === 1 ? latest.id : entry.id, event: latest, event_count: visible.length,
      first_occurred_at: visible.at(-1)!.created_at, last_occurred_at: latest.created_at,
      group_token: visible.length === 1 ? null : token }];
  });
}

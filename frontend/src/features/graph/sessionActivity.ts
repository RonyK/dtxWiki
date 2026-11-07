import type { TFunction } from "i18next";

import type { GraphEdge, GraphNode, GraphQueryPayload } from "../../shared/api/graph";
import { domainLabel } from "../../shared/lib/domainLabel";
import { chatPresentationLine } from "../mcp-chats/chatPresentation";
import { isSessionGroupNode, isSessionNode, sessionBadge } from "./shared/sessionNodes";

/**
 * Name each conversation once, the way its own screen names it: an MCP chat by
 * the Chats title rule, a session group by how many it stands for. Every later
 * consumer (canvas, lists, details, timeline) then reads node.label.
 */
export function withSessionLabels(
  payload: GraphQueryPayload,
  t: TFunction<"graph">,
  tChats: TFunction<"chats">,
): GraphQueryPayload {
  const relabel = (node: GraphNode): GraphNode => {
    if (node.chat_presentation) return { ...node, label: chatPresentationLine(node.chat_presentation, tChats) };
    if (isSessionGroupNode(node) && node.aggregate_count) {
      return { ...node, label: t("graph.session.more", { count: node.aggregate_count }) };
    }
    return node;
  };
  const named = (nodes: GraphNode[]) => nodes.some((node) => isSessionNode(node) || isSessionGroupNode(node))
    ? nodes.map(relabel) : nodes;
  return {
    ...payload,
    nodes: named(payload.nodes),
    timeline: payload.timeline?.map((event) => event.node_snapshots?.length
      ? { ...event, node_snapshots: named(event.node_snapshots) } : event),
  };
}

/** "Edited · 5 times · 11/5 10:00–10:02" for one session-to-document relation. */
/**
 * What a session did to one document, by action: "Edited ×2 · Viewed". The
 * relation carries the whole tally from the server's rollup; a relation from
 * an older server without it reads as its strongest action and total.
 */
export function sessionActionTally(edge: GraphEdge, t: TFunction<"graph">): string {
  const counts = edge.action_counts?.length
    ? edge.action_counts
    : [{ action_kind: edge.action_kind ?? edge.edge_kind, count: edge.count ?? 1 }];
  return counts.map(({ action_kind, count }) => {
    const label = domainLabel("graph", "graphActionKind", action_kind, t);
    return count > 1 ? `${label} ×${count}` : label;
  }).join(" · ");
}

export function sessionRelationSummary(edge: GraphEdge, t: TFunction<"graph">, formatTime: (value: string) => string): string {
  const parts = [sessionActionTally(edge, t)];
  if (edge.window_from && edge.window_to) {
    const from = formatTime(edge.window_from);
    const to = formatTime(edge.window_to);
    parts.push(from === to ? from : `${from} – ${to}`);
  }
  return parts.join(" · ");
}

// Version states (as the version history names them) still awaiting review,
// used only when the server sent no review status for the version.
const IN_REVIEW_VERSION_STATES = new Set(["current_draft", "alternative_draft"]);
const REVIEW_OUTCOMES = new Set(["pending", "changes_requested", "rejected"]);

/** A version in review: its review is pending (changes requested waits for the author). */
function versionInReview(node: Pick<GraphNode, "status" | "review_status">): boolean {
  return node.review_status ? node.review_status === "pending" : IN_REVIEW_VERSION_STATES.has(node.status ?? "");
}

/**
 * Where a version a session produced stands: its open or closed review when
 * there is one ("In review", "Changes requested", "Rejected"), otherwise its
 * lifecycle state in the version history's words.
 */
export function versionOutcome(node: Pick<GraphNode, "status" | "review_status">, t: TFunction<"graph">): string {
  if (node.review_status && REVIEW_OUTCOMES.has(node.review_status)) {
    return t(`graph.session.review.${node.review_status}`);
  }
  return domainLabel("graph", "graphStatus", node.status, t);
}

/**
 * One line over the action timeline, from the scene alone: "AI chats 10 ·
 * Claude 6 · Codex 4 · this document edited 2 · in review 1". Null when the
 * scene has no conversation.
 */
export function sessionActivityDigest(payload: GraphQueryPayload, t: TFunction<"graph">): string | null {
  const sessions = payload.nodes.filter((node) => isSessionNode(node));
  if (!sessions.length) return null;
  const byNode = new Map(payload.nodes.map((node) => [node.id, node]));
  // Chats count by client; an Ask session's actor is a person, so Ask
  // sessions count as one group of their own.
  const clients = new Map<string, number>();
  for (const session of sessions) {
    const badge = session.entity_kind === "ask_session"
      ? t("graph.historyUi.kind.ask") : sessionBadge(session);
    if (badge) clients.set(badge, (clients.get(badge) ?? 0) + 1);
  }
  const sessionIds = new Set(sessions.map((node) => node.id));
  const fromSessions = payload.edges.filter((edge) => sessionIds.has(edge.source));
  const editors = new Set(fromSessions.filter((edge) => byNode.get(edge.target)?.is_seed
    && ["created", "edited"].includes(edge.action_kind ?? "")).map((edge) => edge.source)).size;
  const inReview = new Set(fromSessions.filter((edge) => {
    const target = byNode.get(edge.target);
    return target?.entity_kind === "context_page_version" && versionInReview(target);
  }).map((edge) => edge.target)).size;
  return [
    t("graph.session.digest.sessions", { count: sessions.length }),
    ...[...clients.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([client, count]) => `${client} ${count}`),
    ...(editors ? [t("graph.session.digest.edited", { count: editors })] : []),
    ...(inReview ? [t("graph.session.digest.inReview", { count: inReview })] : []),
  ].join(" · ");
}

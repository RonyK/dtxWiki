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
export function sessionRelationSummary(edge: GraphEdge, t: TFunction<"graph">, formatTime: (value: string) => string): string {
  const parts = [domainLabel("graph", "graphActionKind", edge.action_kind ?? edge.edge_kind, t)];
  if (edge.count) parts.push(t("graph.session.times", { count: edge.count }));
  if (edge.window_from && edge.window_to) {
    const from = formatTime(edge.window_from);
    const to = formatTime(edge.window_to);
    parts.push(from === to ? from : `${from} – ${to}`);
  }
  return parts.join(" · ");
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
  const inReview = new Set(fromSessions.filter((edge) => byNode.get(edge.target)?.status === "draft"
    && byNode.get(edge.target)?.entity_kind === "context_page_version").map((edge) => edge.target)).size;
  return [
    t("graph.session.digest.sessions", { count: sessions.length }),
    ...[...clients.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([client, count]) => `${client} ${count}`),
    ...(editors ? [t("graph.session.digest.edited", { count: editors })] : []),
    ...(inReview ? [t("graph.session.digest.inReview", { count: inReview })] : []),
  ].join(" · ");
}

import type { GraphNode } from "../../../shared/api/graph";

/**
 * Conversations in a graph: one node per MCP chat or Ask session, and one node
 * standing for the sessions past the server's per-document cap. Pure helpers
 * only, so the renderer can use them without the app's i18n or chat modules.
 */
const SESSION_KINDS = new Set(["mcp_chat_session", "ask_session"]);
const SESSION_GROUP_KINDS = new Set(["mcp_chat_session_group", "ask_session_group"]);

/** Strongest first, as the server names a session's relation to a document. */
const SESSION_ACTION_ORDER = ["created", "edited", "cited", "included", "viewed", "referenced"];

export function isSessionNode(node: Pick<GraphNode, "entity_kind"> | null | undefined): boolean {
  return SESSION_KINDS.has(node?.entity_kind ?? "");
}

export function isSessionGroupNode(node: Pick<GraphNode, "entity_kind"> | null | undefined): boolean {
  return SESSION_GROUP_KINDS.has(node?.entity_kind ?? "");
}

export function sessionActionRank(actionKind: string | null | undefined): number {
  const index = SESSION_ACTION_ORDER.indexOf(actionKind ?? "");
  return index < 0 ? SESSION_ACTION_ORDER.length : index;
}

/** The client's first word ("Claude", "Codex"), drawn before a chat's title. */
export function sessionBadge(node: Pick<GraphNode, "entity_kind" | "actor_label">): string | null {
  if (!isSessionNode(node) || !node.actor_label) return null;
  const word = node.actor_label.trim().split(/\s+/u)[0] ?? "";
  return Array.from(word).slice(0, 10).join("") || null;
}

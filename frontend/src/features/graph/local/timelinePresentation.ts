import type { TFunction } from "i18next";
import type { GraphEdge, GraphHistoryMode, GraphNode, GraphTimelineEvent } from "../../../shared/api/graph";
import { domainLabel } from "../../../shared/lib/domainLabel";
import { graphActionFamily } from "../graphModel";
import { sessionActionTally } from "../sessionActivity";
import { isSessionNode } from "../shared/sessionNodes";

export type TimelineMarkKind = "version" | "review" | "ask" | "tool" | "added" | "removed" | "activity";
export type TimelinePresentation = { mark: TimelineMarkKind; lane: number; summary: string; detail: string };

export function timelinePresentation(event: GraphTimelineEvent, mode: GraphHistoryMode,
  nodes: ReadonlyMap<string, GraphNode>, edges: ReadonlyMap<string, GraphEdge>, t: TFunction): TimelinePresentation {
  const eventEdges = [...(event.edge_snapshots ?? []), ...(event.edge_ids ?? []).flatMap((id) => edges.get(id) ?? [])];
  const nodeIds = new Set([...(event.node_ids ?? []), ...eventEdges.flatMap((edge) => [edge.source, edge.target])]);
  const subjects = [...(event.node_snapshots ?? []), ...[...nodeIds].flatMap((id) => nodes.get(id) ?? [])];
  const hasKind = (...kinds: string[]) => subjects.some((node) => kinds.includes(node.entity_kind));
  // A conversation's event carries the documents and versions it touched;
  // it is still that conversation's event, not a version's.
  const session = subjects.find((node) => isSessionNode(node));
  if (session) return sessionPresentation(event, session, eventEdges, nodes, t);
  const version = hasKind("context_page_version");
  const actionFamily = graphActionFamily(event.action_kind);
  const relationship = !version && ["edge_created", "edge_removed"].includes(event.operation);
  const mark: TimelineMarkKind = version ? "version" : relationship ? event.operation === "edge_removed" ? "removed" : "added"
    : actionFamily === "governance" ? "review" : "activity";
  const lane = mode === "appearance" ? Number(!version)
    : Number(!["version", "review"].includes(mark) && !["authoring", "governance"].includes(actionFamily));
  const relation = eventEdges[0];
  const summary = relationship
    ? t(`graph.historyUi.${mark}Relation`, { kind: domainLabel("graph", "graphEdgeKind", relation?.edge_kind, t) })
    : version ? t(event.action_kind === "created" ? "graph.historyUi.versionCreated" : "graph.historyUi.versionEdited")
      : `${t(`graph.historyUi.kind.${mark}`)} · ${domainLabel("graph", "graphActionKind", event.action_kind, t)}`;
  const detail = relationship && relation
    ? `${nodes.get(relation.source)?.label ?? relation.source} → ${nodes.get(relation.target)?.label ?? relation.target}`
    : [...new Set(subjects.filter((node) => !node.is_seed).map((node) => node.label))].join(" · ") || event.label;
  return { mark, lane, summary, detail };
}

/** "Claude Code · Graph View 설계 편집" / "This document: Edited ×5 · 2 other documents". */
function sessionPresentation(event: GraphTimelineEvent, session: GraphNode, eventEdges: GraphEdge[],
  nodes: ReadonlyMap<string, GraphNode>, t: TFunction): TimelinePresentation {
  const own = eventEdges.filter((edge) => edge.source === session.id);
  const seedEdge = own.find((edge) => nodes.get(edge.target)?.is_seed);
  const others = new Set(own.flatMap((edge) => {
    const target = nodes.get(edge.target);
    return target && !target.is_seed && target.entity_kind !== "context_page_version" ? [target.id] : [];
  })).size;
  // The relation's own tally; a relation without an action names the event's.
  const verbs = seedEdge?.action_kind
    ? sessionActionTally(seedEdge, t) : domainLabel("graph", "graphActionKind", event.action_kind, t);
  return {
    mark: session.entity_kind === "ask_session" ? "ask" : "tool",
    lane: 1,
    summary: session.actor_label ? `${session.actor_label} · ${session.label}` : session.label,
    detail: others
      ? t("graph.historyUi.sessionDetailWithOthers", { verbs, count: others })
      : t("graph.historyUi.sessionDetail", { verbs }),
  };
}

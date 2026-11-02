import type { TFunction } from "i18next";
import type { GraphEdge, GraphHistoryMode, GraphNode, GraphTimelineEvent } from "../../../shared/api/graph";
import { domainLabel } from "../../../shared/lib/domainLabel";
import { graphActionFamily } from "../graphModel";

export type TimelineMarkKind = "version" | "review" | "ask" | "tool" | "added" | "removed" | "activity";
export type TimelinePresentation = { mark: TimelineMarkKind; lane: number; summary: string; detail: string };

export function timelinePresentation(event: GraphTimelineEvent, mode: GraphHistoryMode,
  nodes: ReadonlyMap<string, GraphNode>, edges: ReadonlyMap<string, GraphEdge>, t: TFunction): TimelinePresentation {
  const eventEdges = [...(event.edge_snapshots ?? []), ...(event.edge_ids ?? []).flatMap((id) => edges.get(id) ?? [])];
  const nodeIds = new Set([...(event.node_ids ?? []), ...eventEdges.flatMap((edge) => [edge.source, edge.target])]);
  const subjects = [...(event.node_snapshots ?? []), ...[...nodeIds].flatMap((id) => nodes.get(id) ?? [])];
  const hasKind = (...kinds: string[]) => subjects.some((node) => kinds.includes(node.entity_kind));
  const version = hasKind("context_page_version");
  const actionFamily = graphActionFamily(event.action_kind);
  const relationship = !version && ["edge_created", "edge_removed"].includes(event.operation);
  const mark: TimelineMarkKind = version ? "version" : relationship ? event.operation === "edge_removed" ? "removed" : "added"
    : hasKind("mcp_tool_call", "mcp_chat_session", "mcp_session") ? "tool" : hasKind("ask_turn", "ask_session", "chat_message", "chat_session") ? "ask"
      : actionFamily === "governance" ? "review" : "activity";
  const lane = mode === "appearance" ? Number(!version) : ["tool", "ask"].includes(mark) ? 1
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

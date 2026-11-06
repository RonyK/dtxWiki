import type { TFunction } from "i18next";

import type { GraphEdge, GraphNode } from "../../../shared/api/graph";
import { domainLabel } from "../../../shared/lib/domainLabel";
import { GRAPH_NODE_VISUALS } from "../shared/graphTokens";
import { GraphSeedCue } from "../shared/GraphCues";
import { RelationListStrokeCue } from "../shared/relationList/parts";
import type { RelationListEdgeView, RelationListNodeView, RelationListStroke } from "../shared/relationList/types";
import { localEdgeVisualKind } from "./edgeKinds";
import { LocalNodeCue, type LocalNodeVisuals } from "./LocalNodeCue";
import { localNodeVisualKind } from "./nodeVisual";

export type LocalViewContext = {
  /** The document the graph is opened from: its rows show the legend's "This document" glyph. */
  currentId: string | null;
  hrefFor: (node: GraphNode) => string | null;
  seeds: ReadonlySet<string>;
  t: TFunction<"graph">;
  visuals: LocalNodeVisuals;
};

/** How Details and the flow name a node kind ("this document", "this folder" …). */
export type LocalSelfKind = "document" | "library" | "folder" | "other";

export function localSelfKind(node: GraphNode): LocalSelfKind {
  const kind = localNodeVisualKind(node.entity_kind, node.family);
  // A version is one point in its document's history.
  return ["context", "version", "agent", "agentVersion"].includes(kind) ? "document"
    : kind === "library" ? "library" : kind === "folder" ? "folder" : "other";
}

export function localStroke(edge: GraphEdge): RelationListStroke {
  return localEdgeVisualKind(edge.edge_kind) ?? "other";
}

export const LOCAL_STROKE_ORDER: readonly RelationListStroke[] = ["link", "citation", "coAccess", "membership", "action", "other"];

export function localStrokeLabel(t: TFunction<"graph">) {
  return (stroke: RelationListStroke) => t(`relationList.lines.${stroke}`);
}

export function LocalSeedBadge({ t }: { t: TFunction<"graph"> }) {
  return <span className="graph-seed-badge">{t("graph.view.seed")}</span>;
}

/** Short chip name; activity-like nodes use their node type (conversation, ask turn …). */
function nodeChipLabel(node: GraphNode, t: TFunction<"graph">) {
  const kind = localNodeVisualKind(node.entity_kind, node.family);
  return kind === "conversation"
    ? domainLabel("graph", "graphNodeFamily", node.family, t)
    : t(`graph.listChips.node.${kind}`);
}

/** The names a node is shown with, for list search without building its view. */
export function localNodeSearchText(node: GraphNode, t: TFunction<"graph">) {
  return [node.label, nodeChipLabel(node, t), domainLabel("graph", "graphEntityKind", node.entity_kind, t)].join(" ");
}

export function localNodeView(node: GraphNode, context: LocalViewContext): RelationListNodeView {
  const { hrefFor, seeds, t, visuals } = context;
  return {
    badge: seeds.has(node.id) ? <LocalSeedBadge t={t} /> : null,
    chip: {
      cue: <LocalNodeCue node={node} visuals={visuals} />,
      label: nodeChipLabel(node, t),
      title: domainLabel("graph", "graphEntityKind", node.entity_kind, t),
      // Colour says whose: a version takes its document's colour.
      tone: GRAPH_NODE_VISUALS[localNodeVisualKind(node.entity_kind, node.family)].color,
    },
    cue: node.id === context.currentId ? <GraphSeedCue color={visuals.tokens.node.context} />
      : <LocalNodeCue node={node} visuals={visuals} />,
    href: hrefFor(node),
    id: node.id,
    label: node.label,
  };
}

/** The relation kind's full name: the stored relationship, or the recorded action. */
export function localEdgeKindLabel(edge: GraphEdge, t: TFunction<"graph">) {
  return localStroke(edge) === "action"
    ? domainLabel("graph", "graphActionKind", edge.action_kind ?? edge.edge_kind, t)
    : domainLabel("graph", "graphEdgeKind", edge.edge_kind, t);
}

export function localEdgeView(edge: GraphEdge, context: LocalViewContext, labelOf: (id: string) => string): RelationListEdgeView {
  const { t } = context;
  const stroke = localStroke(edge);
  const full = localEdgeKindLabel(edge, t);
  return {
    chip: {
      cue: <RelationListStrokeCue stroke={stroke} />,
      label: stroke === "action" ? full : t(`graph.listChips.edge.${edge.edge_kind}`, { defaultValue: full }),
      title: full,
      tone: stroke,
    },
    directed: edge.direction === "directed",
    id: edge.id,
    name: t(edge.direction === "directed" ? "relationList.relationName" : "relationList.relationNameUndirected", {
      kind: full, source: labelOf(edge.source), target: labelOf(edge.target),
    }),
    // A stored relationship can also carry the action that produced it (cited …).
    note: edge.action_kind && stroke !== "action" ? domainLabel("graph", "graphActionKind", edge.action_kind, t) : null,
    source: edge.source,
    stroke,
    target: edge.target,
  };
}

/** Everything a list search may match for a relation row: both ends and every kind name. */
export function localEdgeSearchText(edge: GraphEdge, view: RelationListEdgeView, labelOf: (id: string) => string) {
  return [labelOf(edge.source), labelOf(edge.target), view.chip.label, view.chip.title, view.note ?? ""].join(" ");
}

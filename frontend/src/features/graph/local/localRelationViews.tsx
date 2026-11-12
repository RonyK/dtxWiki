import type { TFunction } from "i18next";

import type { GraphEdge, GraphNode } from "../../../shared/api/graph";
import { domainLabel } from "../../../shared/lib/domainLabel";
import { GRAPH_NODE_VISUALS, isActionEdgeVisualKind } from "../shared/graphTokens";
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
  /** Where a node sits: the folder holding it in this scene. */
  locationOf: (node: GraphNode) => string | null;
  t: TFunction<"graph">;
  visuals: LocalNodeVisuals;
};

export function localStroke(edge: GraphEdge): RelationListStroke {
  const visual = localEdgeVisualKind(edge.edge_kind);
  // The shared list groups activity strokes; the canvas distinguishes verbs.
  return isActionEdgeVisualKind(visual) ? "action" : visual ?? "other";
}

export const LOCAL_STROKE_ORDER: readonly RelationListStroke[] = ["link", "citation", "coAccess", "membership", "action", "other"];

/** Line names come from the legend, as in the Knowledge Map. */
export function localStrokeLabel(t: TFunction<"graph">) {
  return (stroke: RelationListStroke) => t(`graph.legend.edge.${stroke}`);
}

/**
 * The current document is shown by its glyph alone; the glyph is hidden from
 * assistive technology, so it hears the legend's name for it instead.
 */
function LocalCurrentMark({ t }: { t: TFunction<"graph"> }) {
  return <span className="visually-hidden">{t("graph.legend.node.seed")}</span>;
}

/** Short chip name; activity-like nodes use their node type (conversation, ask turn …). */
function nodeChipLabel(node: GraphNode, t: TFunction<"graph">) {
  const kind = localNodeVisualKind(node.entity_kind, node.family);
  return kind === "conversation"
    ? domainLabel("graph", "graphNodeFamily", node.family, t)
    : t(`graph.listChips.node.${kind}`);
}

/** The names a node is shown with (name, location, kind), for list search without building its view. */
export function localNodeSearchText(node: GraphNode, context: LocalViewContext) {
  const { t } = context;
  return [node.label, context.locationOf(node) ?? "", nodeChipLabel(node, t), domainLabel("graph", "graphEntityKind", node.entity_kind, t)].join(" ");
}

export function localNodeView(node: GraphNode, context: LocalViewContext): RelationListNodeView {
  const { hrefFor, t, visuals } = context;
  return {
    badge: node.id === context.currentId ? <LocalCurrentMark t={t} /> : null,
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
    // Where the node sits, as in the Map's rows.
    meta: context.locationOf(node),
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

/**
 * A relation's own names (kind, full title, the action it carries). A search
 * matches a relation row by these or by the row's node ends: both ends for a
 * free-standing relation, only the far end for a row listed under a node,
 * whose near end is the same on every row.
 */
export function localRelationSearchText(view: RelationListEdgeView) {
  return [view.chip.label, view.chip.title, view.note ?? ""].join(" ");
}

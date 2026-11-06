import type { GraphEdgeVisualKind } from "../shared/graphTokens";

/** Stable Local legend and drawing order; the Workspace Map has a different lens. */
export const LOCAL_EDGE_VISUAL_KIND_ORDER = [
  "link",
  "citation",
  "coAccess",
  "membership",
  "action",
] as const satisfies readonly GraphEdgeVisualKind[];

/**
 * Canonical relationship/event kind -> line style. Classify by the kind itself,
 * never by layer or action metadata on a different stored relationship.
 * Action cases mirror backend ACTION_SEMANTICS: an event occurrence is not a
 * document link, citation, or membership even if it belongs to that layer.
 * A kind absent here has no designed line and is not drawn (nor listed in the
 * legend) until its visual meaning is explicitly added to this table.
 */
const LOCAL_EDGE_VISUAL_BY_KIND: Readonly<Record<string, GraphEdgeVisualKind>> = {
  wikilink: "link",
  citation_page: "citation",
  citation_source: "citation",
  citation_agent: "citation",
  ask_citation: "citation",
  co_access: "coAccess",
  tree: "membership",
  searched: "action",
  returned: "action",
  retrieved: "action",
  included: "action",
  cited: "action",
  created: "action",
  edited: "action",
  reviewed: "action",
  approved: "action",
  rejected: "action",
  request_changes: "action",
  published: "action",
  restored: "action",
  superseded: "action",
  merged: "action",
  promoted: "action",
  tool_produced: "action",
  referenced: "action",
};

export function localEdgeVisualKind(
  edgeKind: string | null | undefined,
): GraphEdgeVisualKind | null {
  return edgeKind && Object.hasOwn(LOCAL_EDGE_VISUAL_BY_KIND, edgeKind)
    ? LOCAL_EDGE_VISUAL_BY_KIND[edgeKind] : null;
}

/** The relationship kinds drawn with one line style, in table order. */
export function localEdgeKindsFor(visual: GraphEdgeVisualKind): string[] {
  return Object.entries(LOCAL_EDGE_VISUAL_BY_KIND)
    .filter(([, kind]) => kind === visual)
    .map(([edgeKind]) => edgeKind);
}

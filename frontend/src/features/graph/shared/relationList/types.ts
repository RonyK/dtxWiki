import type { ReactNode } from "react";

import type { GraphEdgeVisualKind } from "../graphTokens";

/** The map's line style for a relation; `other` is a kind with no designed stroke yet. */
export type RelationListStroke = GraphEdgeVisualKind | "other";

/**
 * A chip: the map symbol plus a short name. `tone` picks the chip colour
 * (a node visual kind or a stroke); `title` carries the full kind name.
 */
export type RelationListChipView = {
  cue: ReactNode;
  label: string;
  title: string;
  tone: string;
};

/** What a list needs to show one node; each screen adapts its own node type. */
export type RelationListNodeView = {
  badge?: ReactNode;
  chip: RelationListChipView;
  cue: ReactNode;
  href?: string | null;
  id: string;
  label: string;
  meta?: string | null;
};

/** What a list needs to show one relation; `name` is the row's accessible name. */
export type RelationListEdgeView = {
  chip: RelationListChipView;
  directed: boolean;
  id: string;
  name: string;
  note?: string | null;
  source: string;
  stroke: RelationListStroke;
  target: string;
};

export type RelationListDirection = "incoming" | "outgoing" | "undirected";

export type RelationListRelations<E> = Record<RelationListDirection, readonly E[]>;

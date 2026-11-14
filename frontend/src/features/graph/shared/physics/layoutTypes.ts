import type { GraphEdgeVisualKind } from "../graphTokens";
import type { GraphPoint } from "../rendererTypes";

// Only data that has already passed the current actor's visibility and Local
// lens filters may enter this input. Keys are stable (entity_kind:entity_id),
// never projection-row UUIDs or server-generated layout/community hints.
export type LocalLayoutNodeInput = {
  key: string;
  radiusPx: number;
  isSeed: boolean;
  parentKey?: string | null;
  primaryFolderKey?: string | null;
};

export type LocalLayoutEdgeInput = {
  sourceKey: string;
  targetKey: string;
  kind: GraphEdgeVisualKind;
  count: number;
  /** Only authored occurrence scenes apply the bounded attraction gain. */
  occurrences?: number;
};

export type LocalLayoutInput = {
  /** The visible primary query seed. null explicitly disables an anchor. */
  primarySeedKey?: string | null;
  nodes: readonly LocalLayoutNodeInput[];
  edges: readonly LocalLayoutEdgeInput[];
};

export type LocalLayoutPositions = Record<string, GraphPoint>;

// Changing any layout force or sizing input invalidates saved coordinates.
export const LOCAL_LAYOUT_PARAMS = {
  version: "local-d3-force-v2",
  preTicks: 300,
  relationDistance: 60,
  membershipDistance: 45,
  chargeStrength: -180,
  chargeDistanceMax: 500,
  axisStrength: 0.05,
  collidePadding: 2,
  dragAlphaTarget: 0.3,
} as const;

// A pin requested before its node entered the timeline frame waits for the
// arrival motion to calm down, then fixes the node where it stands.
export const LOCAL_ARRIVAL_PIN = {
  minMs: 2_000,
  maxMs: 5_000,
  stillWindowMs: 250,
  stillDistance: 1,
  // A longer frame gap (hidden tab, long task) adds at most this much time and
  // never counts as stillness; slow but steady frames still reach maxMs.
  maxFrameGapMs: 250,
} as const;

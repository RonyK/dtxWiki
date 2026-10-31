import type { GraphEdgeVisualKind } from "../shared/graphTokens";
import type { GraphPoint } from "../shared/rendererTypes";

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
};

export type LocalLayoutInput = {
  nodes: readonly LocalLayoutNodeInput[];
  edges: readonly LocalLayoutEdgeInput[];
};

export type LocalLayoutPositions = Record<string, GraphPoint>;

// Changing any layout force or sizing input invalidates saved coordinates.
export const LOCAL_LAYOUT_PARAMS = {
  version: "local-d3-force-v1",
  preTicks: 300,
  relationDistance: 60,
  membershipDistance: 45,
  chargeStrength: -180,
  chargeDistanceMax: 500,
  axisStrength: 0.05,
  collidePadding: 2,
  dragAlphaTarget: 0.3,
} as const;

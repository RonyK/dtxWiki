import type { GraphNode } from "../../../shared/api/graph";
import { GraphNodeCue } from "../shared/GraphCues";
import { GRAPH_NODE_VISUALS, type GraphVisualTokens } from "../shared/graphTokens";
import { localNodeVisualKind } from "./nodeVisual";

export type LocalNodeVisuals = { tokens: GraphVisualTokens; forcedColors: boolean };

/** A node's list glyph: the legend's shape and colour for its kind, in the canvas tokens. */
export function LocalNodeCue({ node, visuals }: { node: GraphNode; visuals: LocalNodeVisuals }) {
  const kind = localNodeVisualKind(node.entity_kind, node.family);
  return <GraphNodeCue className="graph-local-node-cue" color={visuals.tokens.node[GRAPH_NODE_VISUALS[kind].color]}
    forcedColors={visuals.forcedColors} kind={kind} surface={visuals.tokens.surface} />;
}

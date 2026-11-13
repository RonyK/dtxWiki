import { buildLocalScene } from "../../local/sceneInput";
import { localNodeVisualKind } from "../../local/nodeVisual";
import { localEdgeVisualKind, LOCAL_EDGE_VISUAL_KIND_ORDER } from "../../local/edgeKinds";
import type { GraphNodeVisualKind } from "../graphTokens";
import type { GraphRenderEdge, GraphRenderNode, GraphRenderScene, GraphVisibleIdentity } from "../rendererTypes";
import type { CosmosSceneColumns } from "./sceneTypes";

// These are this adapter's layout codes, separate from native point shapes.
const LOCAL_NODE_VISUAL_KINDS: readonly GraphNodeVisualKind[] = [
  "context", "version", "agent", "agentVersion", "library", "conversation", "folder", "cluster",
];

/**
 * Prepare Context expressions for the common renderer. Admission and semantic sorting are the existing
 * Local scene's; relation rows keep the wire direction and endpoint order,
 * including an undirected relationship whose force input is canonicalized.
 */
export function buildLocalCosmosColumns(
  scene: GraphRenderScene,
  identity: GraphVisibleIdentity,
): CosmosSceneColumns {
  const local = buildLocalScene(scene);
  const nodeIndex = new Map(local.nodes.map((node, index) => [node.key, index]));
  const wireNodes = new Map<string, GraphRenderNode>();
  for (const node of scene.nodes.filter((node) => !node.hidden).sort((left, right) => left.id.localeCompare(right.id))) {
    const key = `${node.entity_kind}:${node.entity_id}`;
    if (!wireNodes.has(key)) wireNodes.set(key, node);
  }
  // A duplicate wire ID makes Local's geometry ambiguous, but must not turn
  // one occurrence into a different raw relationship during preparation.
  const edgeKey = (id: string, source: string, target: string, kind: string) => JSON.stringify([id, source, target, kind]);
  const wireEdges = new Map<string, GraphRenderEdge[]>();
  for (const edge of scene.edges) {
    if (edge.hidden) continue;
    const kind = localEdgeVisualKind(edge.edge_kind);
    const source = local.keyByGraphId.get(edge.source), target = local.keyByGraphId.get(edge.target);
    if (!kind || !source || !target || source === target) continue;
    const [from, to] = edge.direction === "undirected" && source > target ? [target, source] : [source, target];
    const key = edgeKey(edge.id, from, to, kind);
    const rows = wireEdges.get(key) ?? [];
    rows.push(edge); wireEdges.set(key, rows);
  }
  const usedRows = new Map<string, number>();
  const originalEdges = local.edges.map((edge) => {
    const key = edgeKey(edge.graphId, edge.sourceKey, edge.targetKey, edge.kind);
    const used = usedRows.get(key) ?? 0;
    usedRows.set(key, used + 1);
    return wireEdges.get(key)![used];
  });
  const nodeVisualKinds = local.nodes.map((node) => localNodeVisualKind(node.entityKind, node.family));
  const edgeVisualKinds = local.edges.map((edge) => edge.kind);
  const edgeSources = Uint32Array.from(originalEdges, (edge) => nodeIndex.get(local.keyByGraphId.get(edge.source)!)!);
  const edgeTargets = Uint32Array.from(originalEdges, (edge) => nodeIndex.get(local.keyByGraphId.get(edge.target)!)!);
  return {
    identity: { ...identity },
    nodeIds: local.nodes.map((node) => node.graphId),
    nodeKeys: local.nodes.map((node) => node.key),
    nodeLabels: local.nodes.map((node) => node.label),
    nodeFolderKeys: local.nodes.map(() => null),
    nodeKinds: Uint8Array.from(nodeVisualKinds, (kind) => LOCAL_NODE_VISUAL_KINDS.indexOf(kind)),
    // The common renderer consumes CSS-pixel diameters; Local describes radii.
    nodeSizes: Float32Array.from(local.nodes, (node) => node.radiusPx * 2),
    nodeEntityKinds: local.nodes.map((node) => node.entityKind),
    nodeGrains: local.nodes.map((node) => wireNodes.get(node.key)?.grain ?? null),
    nodeVisualKinds,
    layoutInput: local.layout,
    localGeometryKey: local.geometryKey,
    nodeIsSeed: Uint8Array.from(local.nodes, node => Number(node.isSeed)),
    nodeIsCurrent: Uint8Array.from(local.nodes, node => Number(node.isCurrent)),
    nodeSeedGlyph: Uint8Array.from(local.nodes, node => Number(node.seedGlyph)),
    nodeMuted: Uint8Array.from(local.nodes, node => Number(Boolean(wireNodes.get(node.key)?.muted))),
    nodeHighlighted: Uint8Array.from(local.nodes, node => Number(Boolean(wireNodes.get(node.key)?.highlighted))),
    nodeSessions: Uint8Array.from(local.nodes, node => Number(node.session)),
    nodeBadges: local.nodes.map(node => node.badge),
    edgeIds: local.edges.map((edge) => edge.graphId),
    edgeSources,
    edgeTargets,
    edgeKinds: Uint8Array.from(edgeVisualKinds, (kind) => LOCAL_EDGE_VISUAL_KIND_ORDER.indexOf(kind)),
    edgeCounts: Uint32Array.from(local.edges, (edge) => edge.count),
    edgeWeights: Float64Array.from(local.edges, (edge) => edge.count),
    edgeCrossFolder: new Uint8Array(local.edges.length),
    edgeRelationKinds: originalEdges.map((edge) => edge.edge_kind!),
    edgeDirections: originalEdges.map((edge) => edge.direction ?? null),
    edgeVisualKinds,
    edgeLabels: originalEdges.map(edge => edge.label),
    edgeCaptions: originalEdges.map(edge => edge.eventCaption ?? null),
    edgeMuted: Uint8Array.from(originalEdges, edge => Number(Boolean(edge.muted))),
    edgeHighlighted: Uint8Array.from(originalEdges, edge => Number(Boolean(edge.highlighted))),
    edgeRemoved: Uint8Array.from(originalEdges, edge => Number(Boolean(edge.removed))),
  };
}

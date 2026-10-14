import type { GraphEdgeVisualKind } from "../shared/graphTokens";
import { GRAPH_NODE_RADIUS_PX } from "../shared/graphTokens";
import type { GraphRenderEdge, GraphRenderNode, GraphRenderScene } from "../shared/rendererTypes";
import { localEdgeVisualKind } from "./edgeKinds";
import type { LocalLayoutInput } from "./layoutTypes";

export type LocalSceneNode = {
  key: string;
  graphId: string;
  label: string;
  family: GraphRenderNode["family"];
  entityKind: string;
  status: string | null;
  isSeed: boolean;
  radiusPx: number;
};

export type LocalSceneEdge = {
  graphId: string;
  sourceKey: string;
  targetKey: string;
  kind: GraphEdgeVisualKind;
  count: number;
};

export type LocalScene = {
  layout: LocalLayoutInput;
  // Render-scene identity, NOT the force-layout/cache fingerprint. Includes
  // original wire IDs; null refuses reuse for ambiguous identities.
  geometryKey: string | null;
  nodes: LocalSceneNode[];
  edges: LocalSceneEdge[];
  keyByGraphId: Map<string, string>;
  graphIdByKey: Map<string, string>;
};

function semanticKey(node: GraphRenderNode): string | null {
  if (!node.entity_kind || !node.entity_id) return null;
  return `${node.entity_kind}:${node.entity_id}`;
}

function edgeCount(edge: GraphRenderEdge): number {
  const count = edge.count ?? 1;
  return Number.isFinite(count) && count > 0 ? Math.floor(count) || 1 : 1;
}

/**
 * Turn an already actor-visible, Local-filtered scene into stable semantic
 * layout input. Projection edge UUIDs may be used for UI selection but never
 * enter the force topology or coordinate-cache fingerprint.
 */
export function buildLocalScene(scene: GraphRenderScene): LocalScene {
  const keyByGraphId = new Map<string, string>();
  const graphIdByKey = new Map<string, string>();
  const nodeByKey = new Map<string, GraphRenderNode>();

  for (const node of [...scene.nodes].sort((a, b) => a.id.localeCompare(b.id))) {
    const key = semanticKey(node);
    if (!key) continue;
    keyByGraphId.set(node.id, key);
    if (!nodeByKey.has(key)) {
      nodeByKey.set(key, node);
      graphIdByKey.set(key, node.id);
    }
  }

  const edges: LocalSceneEdge[] = [];
  for (const edge of scene.edges) {
    const kind = localEdgeVisualKind(edge.edge_kind);
    const sourceKey = keyByGraphId.get(edge.source);
    const targetKey = keyByGraphId.get(edge.target);
    if (!kind || !sourceKey || !targetKey || sourceKey === targetKey) continue;
    const [source, target] = edge.direction === "undirected" && sourceKey > targetKey
      ? [targetKey, sourceKey]
      : [sourceKey, targetKey];
    edges.push({
      graphId: edge.id,
      sourceKey: source,
      targetKey: target,
      kind,
      count: edgeCount(edge),
    });
  }
  edges.sort((a, b) => a.kind.localeCompare(b.kind)
    || a.sourceKey.localeCompare(b.sourceKey)
    || a.targetKey.localeCompare(b.targetKey)
    || a.graphId.localeCompare(b.graphId));

  const degree = new Map<string, number>();
  const layoutEdges = new Map<string, LocalLayoutInput["edges"][number]>();
  for (const edge of edges) {
    degree.set(edge.sourceKey, (degree.get(edge.sourceKey) ?? 0) + edge.count);
    degree.set(edge.targetKey, (degree.get(edge.targetKey) ?? 0) + edge.count);
    const key = JSON.stringify([edge.kind, edge.sourceKey, edge.targetKey]);
    const previous = layoutEdges.get(key);
    layoutEdges.set(key, {
      kind: edge.kind,
      sourceKey: edge.sourceKey,
      targetKey: edge.targetKey,
      count: (previous?.count ?? 0) + edge.count,
    });
  }

  const { min, max } = GRAPH_NODE_RADIUS_PX.local;
  const nodes = [...nodeByKey.entries()].sort(([left], [right]) => left.localeCompare(right))
    .map(([key, node]): LocalSceneNode => ({
      key,
      graphId: node.id,
      label: node.label,
      family: node.family,
      entityKind: node.entity_kind ?? node.entityKind ?? "unknown",
      status: node.status ?? null,
      isSeed: Boolean(node.is_seed),
      radiusPx: Math.min(max, Math.max(min, min + 2 * Math.sqrt(degree.get(key) ?? 0))),
    }));

  const wireEdges = new Map(scene.edges.map((edge) => [edge.id, edge]));
  const uniqueIdentity = nodeByKey.size === scene.nodes.length
    && keyByGraphId.size === scene.nodes.length && wireEdges.size === scene.edges.length;
  const geometryKey = uniqueIdentity ? JSON.stringify([
    nodes.map((node) => [node.key, node.graphId, node.radiusPx, node.isSeed]),
    edges.map((edge) => {
      const wire = wireEdges.get(edge.graphId)!;
      return [edge.graphId, wire.source, wire.target, wire.edge_kind, wire.direction ?? null,
        edge.sourceKey, edge.targetKey, edge.kind, edge.count];
    }),
  ]) : null;

  return {
    nodes,
    edges,
    geometryKey,
    keyByGraphId,
    graphIdByKey,
    layout: {
      nodes: nodes.map((node) => ({
        key: node.key,
        radiusPx: node.radiusPx,
        isSeed: node.isSeed,
      })),
      edges: [...layoutEdges.values()].sort((a, b) => a.kind.localeCompare(b.kind)
        || a.sourceKey.localeCompare(b.sourceKey)
        || a.targetKey.localeCompare(b.targetKey)),
    },
  };
}

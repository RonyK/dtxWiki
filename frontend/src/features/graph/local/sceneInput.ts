import type { GraphEdgeVisualKind } from "../shared/graphTokens";
import { GRAPH_NODE_RADIUS_PX } from "../shared/graphTokens";
import type { GraphRenderEdge, GraphRenderNode, GraphRenderScene } from "../shared/rendererTypes";
import { isSessionNode, sessionBadge } from "../shared/sessionNodes";
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
  /** The document the graph is opened from; see GraphRenderScene.currentId. */
  isCurrent: boolean;
  /** Drawn with the seed glyph: the current document, or every seed when the scene names none. */
  seedGlyph: boolean;
  radiusPx: number;
  /** A conversation: its title yields to others' on a narrow canvas. */
  session: boolean;
  /** Short client word drawn before a conversation's title. */
  badge: string | null;
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
  const visibleNodes = scene.nodes.filter((node) => !node.hidden);
  const keyByGraphId = new Map<string, string>();
  const graphIdByKey = new Map<string, string>();
  const nodeByKey = new Map<string, GraphRenderNode>();

  for (const node of [...visibleNodes].sort((a, b) => a.id.localeCompare(b.id))) {
    const key = semanticKey(node);
    if (!key) continue;
    keyByGraphId.set(node.id, key);
    if (!nodeByKey.has(key)) {
      nodeByKey.set(key, node);
      graphIdByKey.set(key, node.id);
    }
  }

  const edges: LocalSceneEdge[] = [];
  const admittedWireEdges: GraphRenderEdge[] = [];
  for (const edge of scene.edges) {
    if (edge.hidden) continue;
    const kind = localEdgeVisualKind(edge.edge_kind);
    const sourceKey = keyByGraphId.get(edge.source);
    const targetKey = keyByGraphId.get(edge.target);
    if (!kind || !sourceKey || !targetKey || sourceKey === targetKey) continue;
    admittedWireEdges.push(edge);
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
  const currentKey = scene.currentId ? keyByGraphId.get(scene.currentId) ?? null : null;
  const nodes = [...nodeByKey.entries()].sort(([left], [right]) => left.localeCompare(right))
    .map(([key, node]): LocalSceneNode => ({
      key,
      graphId: node.id,
      label: node.label,
      family: node.family,
      entityKind: node.entity_kind ?? node.entityKind ?? "unknown",
      status: node.status ?? null,
      isSeed: Boolean(node.is_seed),
      isCurrent: key === currentKey,
      seedGlyph: scene.currentId === undefined ? Boolean(node.is_seed) : key === currentKey,
      radiusPx: Math.min(max, Math.max(min, min + 2 * Math.sqrt(degree.get(key) ?? 0))),
      session: isSessionNode({ entity_kind: node.entity_kind ?? "" }),
      badge: sessionBadge({ entity_kind: node.entity_kind ?? "", actor_label: node.actor_label ?? null }),
    }));

  const wireEdges = new Map(admittedWireEdges.map((edge) => [edge.id, edge]));
  const uniqueIdentity = nodeByKey.size === visibleNodes.length
    && keyByGraphId.size === visibleNodes.length && wireEdges.size === admittedWireEdges.length;
  const geometryKey = uniqueIdentity ? JSON.stringify([
    nodes.map((node) => [node.key, node.graphId, node.radiusPx, node.isSeed, node.isCurrent, node.seedGlyph]),
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

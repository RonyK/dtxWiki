import type { GraphEdge, GraphNode } from "../../../shared/api/graph";
import type { RelationListRelations } from "../shared/relationList/types";
import { localEdgeVisualKind } from "./edgeKinds";

type LocalScene = { nodes: readonly GraphNode[]; edges: readonly GraphEdge[] };

export type LocalNodeRelations = RelationListRelations<GraphEdge>;

/**
 * One pass over the displayed scene: what the list view and Details need to
 * place every node and relation relative to the seed documents. Only the
 * displayed scene is authoritative, never the full query or tree.
 */
export type LocalSceneAnalysis = {
  /** Relations the renderer draws: both ends shown, a designed stroke, not a self-link. */
  edges: GraphEdge[];
  /** Distance from the nearest seed, ignoring direction. Absent: not connected. */
  hop: Map<string, number>;
  nodesById: Map<string, GraphNode>;
  /** Neighbours one step closer to a seed, in scene order. */
  parents: Map<string, string[]>;
  /** The same distances from the primary seed alone, for "how is this reached from the seed". */
  primary: { hop: Map<string, number>; parents: Map<string, string[]> };
  relationsByNode: Map<string, LocalNodeRelations>;
  /** Seed node ids, primary seed first. */
  seeds: string[];
};

export type LocalSceneRings = {
  /** Both ends are direct neighbours of a seed. */
  neighbors: GraphEdge[];
  /** Two or more steps from the seeds, closest first. */
  ringNodes: GraphNode[];
  /** Relations beyond the direct neighbours (neither end is a seed). */
  ringRelations: GraphEdge[];
  /** Nodes and relations with no path to a seed in this scene. */
  unlinkedEdges: GraphEdge[];
  unlinkedNodes: GraphNode[];
};

function drawable(edge: GraphEdge, nodesById: ReadonlyMap<string, GraphNode>) {
  const source = nodesById.get(edge.source);
  const target = nodesById.get(edge.target);
  if (!source || !target || !localEdgeVisualKind(edge.edge_kind)) return false;
  // The renderer omits self-links, including aliases of one semantic node.
  return !(source.entity_kind === target.entity_kind && source.entity_id === target.entity_id);
}

export function analyzeLocalScene(
  scene: LocalScene,
  seedOrder: readonly { kind: string; id: string }[] = [],
): LocalSceneAnalysis {
  const nodesById = new Map(scene.nodes.map((node) => [node.id, node]));
  const order = (node: GraphNode) => {
    const index = seedOrder.findIndex((seed) => seed.kind === node.entity_kind && seed.id === node.entity_id);
    return index < 0 ? seedOrder.length : index;
  };
  const seeds = scene.nodes.filter((node) => node.is_seed)
    .sort((left, right) => order(left) - order(right)).map((node) => node.id);
  const edges = scene.edges.filter((edge) => drawable(edge, nodesById));
  const adjacency = new Map<string, string[]>();
  for (const edge of edges) {
    adjacency.set(edge.source, [...(adjacency.get(edge.source) ?? []), edge.target]);
    adjacency.set(edge.target, [...(adjacency.get(edge.target) ?? []), edge.source]);
  }
  const { hop, parents } = breadthFirst(seeds, adjacency);
  const primary = breadthFirst(seeds.slice(0, 1), adjacency);
  const relationsByNode = new Map<string, LocalNodeRelations>();
  const bucket = (id: string) => {
    let relations = relationsByNode.get(id);
    if (!relations) {
      relations = { incoming: [], outgoing: [], undirected: [] };
      relationsByNode.set(id, relations);
    }
    return relations as { incoming: GraphEdge[]; outgoing: GraphEdge[]; undirected: GraphEdge[] };
  };
  for (const edge of edges) {
    if (edge.direction === "directed") {
      bucket(edge.source).outgoing.push(edge);
      bucket(edge.target).incoming.push(edge);
    } else {
      bucket(edge.source).undirected.push(edge);
      bucket(edge.target).undirected.push(edge);
    }
  }
  const seedSet = new Set(seeds);
  const label = (id: string) => nodesById.get(id)?.label ?? "";
  for (const [nodeId, relations] of relationsByNode) {
    const other = (edge: GraphEdge) => edge.source === nodeId ? edge.target : edge.source;
    // Relations with a seed come first; then by the other end's name.
    const compare = (left: GraphEdge, right: GraphEdge) =>
      Number(seedSet.has(other(right))) - Number(seedSet.has(other(left)))
      || label(other(left)).localeCompare(label(other(right)))
      || left.id.localeCompare(right.id);
    for (const list of Object.values(relations) as GraphEdge[][]) list.sort(compare);
  }
  return { edges, hop, nodesById, parents, primary, relationsByNode, seeds };
}

/** Breadth-first from every start at once, ignoring direction: O(nodes + relations). */
function breadthFirst(starts: readonly string[], adjacency: ReadonlyMap<string, string[]>) {
  const hop = new Map<string, number>();
  const parents = new Map<string, string[]>();
  let frontier = [...starts];
  for (const id of frontier) hop.set(id, 0);
  while (frontier.length) {
    const next: string[] = [];
    for (const id of frontier) {
      const distance = hop.get(id)! + 1;
      for (const other of adjacency.get(id) ?? []) {
        if (!hop.has(other)) {
          hop.set(other, distance);
          parents.set(other, [id]);
          next.push(other);
        } else if (hop.get(other) === distance && !parents.get(other)!.includes(id)) {
          parents.get(other)!.push(id);
        }
      }
    }
    frontier = next;
  }
  return { hop, parents };
}

const NO_RELATIONS: LocalNodeRelations = { incoming: [], outgoing: [], undirected: [] };

export function localNodeRelations(analysis: LocalSceneAnalysis, nodeId: string): LocalNodeRelations {
  return analysis.relationsByNode.get(nodeId) ?? NO_RELATIONS;
}

/** Group what the relation scope added beyond the seeds' own relations. */
export function localSceneRings(analysis: LocalSceneAnalysis): LocalSceneRings {
  const seedSet = new Set(analysis.seeds);
  const rings: LocalSceneRings = { neighbors: [], ringNodes: [], ringRelations: [], unlinkedEdges: [], unlinkedNodes: [] };
  for (const edge of analysis.edges) {
    if (seedSet.has(edge.source) || seedSet.has(edge.target)) continue;
    const source = analysis.hop.get(edge.source);
    const target = analysis.hop.get(edge.target);
    if (source === undefined || target === undefined) rings.unlinkedEdges.push(edge);
    else if (source === 1 && target === 1) rings.neighbors.push(edge);
    else rings.ringRelations.push(edge);
  }
  for (const node of analysis.nodesById.values()) {
    const distance = analysis.hop.get(node.id);
    if (distance === undefined) rings.unlinkedNodes.push(node);
    else if (distance >= 2) rings.ringNodes.push(node);
  }
  rings.ringNodes.sort((left, right) => analysis.hop.get(left.id)! - analysis.hop.get(right.id)!
    || left.label.localeCompare(right.label) || left.id.localeCompare(right.id));
  return rings;
}

/**
 * One shortest path to the node (seed first) and the other routes through a
 * different last step. Measured from the primary seed, so an expanded seed
 * still shows how it is reached; otherwise from the nearest seed.
 */
export function localSeedPath(analysis: LocalSceneAnalysis, nodeId: string) {
  const fromPrimary = (analysis.primary.hop.get(nodeId) ?? 0) > 0;
  const { hop, parents } = fromPrimary ? analysis.primary : analysis;
  const distance = hop.get(nodeId);
  if (distance === undefined || distance === 0) return null;
  const chain = [nodeId];
  while (hop.get(chain[chain.length - 1])! > 0) chain.push(parents.get(chain[chain.length - 1])![0]);
  chain.reverse();
  const alternatives = (parents.get(nodeId) ?? []).slice(1);
  const between = (left: string, right: string) => analysis.edges.filter((edge) =>
    (edge.source === left && edge.target === right) || (edge.source === right && edge.target === left));
  return {
    alternatives,
    chain,
    links: chain.slice(1).map((id, index) => between(chain[index], id)),
    steps: distance,
  };
}

/** A node or relation matches when its names (and the relation's ends) contain the query. */
export function localListMatches(query: string) {
  const normalized = query.trim().toLocaleLowerCase();
  return (...texts: (string | null | undefined)[]) => !normalized
    || texts.some((text) => text?.toLocaleLowerCase().includes(normalized));
}

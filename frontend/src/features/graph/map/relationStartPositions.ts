import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";

export type WorkspaceRelationStartEdge = Readonly<{
  source: number;
  target: number;
  weight: number;
}>;

export type WorkspaceRelationStartInput = Readonly<{
  nodeCount: number;
  workspaceSeed: number;
  edges: readonly WorkspaceRelationStartEdge[];
}>;

type LayoutNode = SimulationNodeDatum & { id: number };
type LayoutLink = SimulationLinkDatum<LayoutNode> & {
  sourceIndex: number;
  targetIndex: number;
  weight: number;
};

const MAX_FULL_NODE_COUNT = 2000;
const TICK_COUNT = 180;
const NODE_RADIUS = 10;
const LINK_DISTANCE = NODE_RADIUS * 2 + 90;
const COLLISION_RADIUS = NODE_RADIUS + 25;
const REPULSION = -400;
const SPACE_CENTER = 2048;
const SPACE_HALF_EXTENT = 1800;

function canonicalEdges(input: WorkspaceRelationStartInput): WorkspaceRelationStartEdge[] {
  if (!Number.isSafeInteger(input.nodeCount) || input.nodeCount < 0) {
    throw new Error("Relation warm-start node count must be a nonnegative safe integer");
  }
  if (!Number.isInteger(input.workspaceSeed)
    || input.workspaceSeed < 0 || input.workspaceSeed > 0xffffffff) {
    throw new Error("Relation warm-start workspace seed must be a uint32 integer");
  }
  if (!Array.isArray(input.edges)) {
    throw new Error("Relation warm-start edges must be an array");
  }
  const seen = new Set<string>();
  const result: WorkspaceRelationStartEdge[] = [];
  for (const edge of input.edges) {
    if (edge == null || typeof edge !== "object") {
      throw new Error("Relation warm-start edges must contain edge objects");
    }
    for (const endpoint of [edge.source, edge.target]) {
      if (!Number.isSafeInteger(endpoint) || endpoint < 0 || endpoint >= input.nodeCount) {
        throw new Error("Relation warm-start edge has an invalid visible endpoint");
      }
    }
    if (!Number.isSafeInteger(edge.weight) || edge.weight <= 0) {
      throw new Error("Relation warm-start occurrence weight must be a positive safe integer");
    }
    if (edge.source === edge.target) {
      throw new Error("Relation warm-start canonical input must not contain a self edge");
    }
    const source = Math.min(edge.source, edge.target);
    const target = Math.max(edge.source, edge.target);
    const key = `${source}:${target}`;
    if (seen.has(key)) {
      throw new Error("Relation warm-start canonical input contains a duplicate pair");
    }
    seen.add(key);
    result.push({ source, target, weight: edge.weight });
  }
  return result.sort((left, right) => left.source - right.source || left.target - right.target);
}

function seededRandom(seed: number): () => number {
  let state = seed || 0x9e3779b9;
  return () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return (state >>> 0) / 0x100000000;
  };
}

function finiteCoordinates(node: LayoutNode): readonly [number, number] {
  if (node.x === undefined || node.y === undefined
    || !Number.isFinite(node.x) || !Number.isFinite(node.y)) {
    throw new Error("Relation warm-start simulation returned non-finite coordinates");
  }
  return [node.x, node.y];
}

function fitPositions(nodes: readonly LayoutNode[]): Float32Array {
  let meanX = 0;
  let meanY = 0;
  for (const node of nodes) {
    const [x, y] = finiteCoordinates(node);
    meanX += x / nodes.length;
    meanY += y / nodes.length;
  }
  let extent = 0;
  for (const node of nodes) {
    const [x, y] = finiteCoordinates(node);
    extent = Math.max(extent, Math.abs(x - meanX), Math.abs(y - meanY));
  }
  // Only shrink oversized layouts; do not inflate a small linked scene to the
  // whole space or erase occurrence-driven differences between warm starts.
  const scale = extent > SPACE_HALF_EXTENT ? SPACE_HALF_EXTENT / extent : 1;
  const result = new Float32Array(nodes.length * 2);
  nodes.forEach((node, index) => {
    const [x, y] = finiteCoordinates(node);
    result[index * 2] = SPACE_CENTER + (x - meanX) * scale;
    result[index * 2 + 1] = SPACE_CENTER + (y - meanY) * scale;
  });
  return result;
}

/**
 * Worker-only, bounded full-document initial layout; never a live renderer.
 * Input endpoints index the caller's already actor-visible node catalog.
 * The caller retains the scalable fallback for >2K and all-isolate scenes.
 *
 * This uses the Neo4j Browser Classic radius/distance/charge source profile
 * with D3's degree-normalized link force and translation-only forceCenter.
 * Authored occurrences amplify D3 strength up to its stable default maximum.
 * That warm-start normalization is not Cosmos's square-root shader profile;
 * live GPU physics continues to use the separate exact occurrence helper.
 */
export function buildWorkspaceRelationStartPositions(
  input: WorkspaceRelationStartInput,
): Float32Array | null {
  // Validate before either fallback, but allocate no simulation nodes above 2K.
  const edges = canonicalEdges(input);
  if (input.nodeCount > MAX_FULL_NODE_COUNT || edges.length === 0) return null;

  const degrees = new Uint32Array(input.nodeCount);
  const nodes: LayoutNode[] = Array.from({ length: input.nodeCount }, (_, id) => ({ id }));
  const links: LayoutLink[] = edges.map((edge) => {
    degrees[edge.source] += 1;
    degrees[edge.target] += 1;
    return {
      source: edge.source,
      target: edge.target,
      sourceIndex: edge.source,
      targetIndex: edge.target,
      weight: edge.weight,
    };
  });
  const linkForce = forceLink<LayoutNode, LayoutLink>(links)
    .id((node) => node.id)
    .distance(LINK_DISTANCE)
    .strength((edge) => {
      const gain = Math.min(16, 1 + Math.log2(edge.weight));
      const minimumDegree = Math.min(degrees[edge.sourceIndex], degrees[edge.targetIndex]);
      return Math.min(1, gain / minimumDegree);
    });

  // Stop the automatically created timer before adding forces: only these
  // fixed ticks advance positions, independently of worker scheduling.
  const simulation = forceSimulation<LayoutNode>(nodes).stop();
  try {
    simulation
      .randomSource(seededRandom(input.workspaceSeed))
      .force("charge", forceManyBody<LayoutNode>().strength(REPULSION))
      .force("link", linkForce)
      .force("collision", forceCollide<LayoutNode>(COLLISION_RADIUS))
      .force("center", forceCenter<LayoutNode>(0, 0))
      .tick(TICK_COUNT);
    return fitPositions(nodes);
  } finally {
    simulation.stop();
  }
}

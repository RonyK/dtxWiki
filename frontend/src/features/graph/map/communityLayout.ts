import { UndirectedGraph } from "graphology";
import louvain from "graphology-communities-louvain";
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation } from "d3-force";
import { buildWorkspaceRelationStartPositions, type WorkspaceRelationStartEdge } from "./relationStartPositions";

/** The worker receives only columns already filtered for the current actor. */
export type WorkspaceMapCommunityInput = {
  workspaceId: string;
  nodeKeys: string[];
  edgeSources: Uint32Array;
  edgeTargets: Uint32Array;
  /** Distinct relation counts are not Louvain weights. */
  edgeWeights: Float64Array;
};

export type WorkspaceMapCommunities = {
  /** Indexed exactly like the input nodeKeys; IDs are ordered by minimum key. */
  communityIds: Uint32Array;
  communityCount: number;
  /** Interleaved x/y graph coordinates, rounded to three decimal places. */
  initialPositions: Float32Array;
};

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
const COARSE_COMMUNITY_LIMIT = 256;
type PairWeights = ReadonlyMap<number, ReadonlyMap<number, number>>;

/** A small relation quotient supplies starting anchors, never document physics. */
function seedRelatedCommunityCenters(
  centers: Float64Array,
  sizes: Uint32Array,
  radii: Float64Array,
  communityIds: Uint32Array,
  weights: PairWeights,
  workspaceId: string,
): void {
  if (sizes.length < 2 || sizes.length > COARSE_COMMUNITY_LIMIT) return;
  const between = new Map<number, Map<number, number>>();
  for (const source of [...weights.keys()].sort(compareNumber)) {
    const row = weights.get(source)!;
    for (const target of [...row.keys()].sort(compareNumber)) {
      const weight = row.get(target)!;
      const a = communityIds[source], b = communityIds[target];
      if (a === b) continue;
      const lower = Math.min(a, b), upper = Math.max(a, b);
      let totals = between.get(lower);
      if (!totals) { totals = new Map(); between.set(lower, totals); }
      totals.set(upper, (totals.get(upper) ?? 0) + weight);
    }
  }
  if (between.size === 0) return;
  const links = [...between].sort(([a], [b]) => a - b).flatMap(([source, row]) =>
    [...row].sort(([a], [b]) => a - b).map(([target, weight]) => ({
      source, target, affinity: weight / Math.sqrt(sizes[source] * sizes[target]),
      distance: radii[source] + radii[target] + 80 / Math.sqrt(weight / Math.sqrt(sizes[source] * sizes[target])),
    })));
  const connected = [...new Set(links.flatMap(({ source, target }) => [source, target]))].sort(compareNumber);
  // The old ring/grid order is unrelated to topology and can trap opposite
  // linked groups behind unrelated neighbors. Relax a compact deterministic
  // seed for linked groups; isolated documents retain their bounded packing.
  const neighbors = new Map<number, Array<{ id: number; affinity: number }>>();
  links.forEach(({ source, target, affinity }) => {
    neighbors.set(source, [...(neighbors.get(source) ?? []), { id: target, affinity }]);
    neighbors.set(target, [...(neighbors.get(target) ?? []), { id: source, affinity }]);
  });
  const visited = new Set<number>();
  const topologyOrder: number[] = [];
  const visit = (id: number) => {
    if (visited.has(id)) return;
    visited.add(id); topologyOrder.push(id);
    for (const neighbor of (neighbors.get(id) ?? []).sort((a, b) => b.affinity - a.affinity || a.id - b.id)) visit(neighbor.id);
  };
  connected.forEach(visit);
  const width = Math.ceil(Math.sqrt(connected.length));
  const step = connected.reduce((sum, id) => sum + radii[id] * 2 + 40, 0) / connected.length;
  const seedPositions = new Map(topologyOrder.map((id, ordinal) => {
    const row = Math.floor(ordinal / width), column = ordinal % width;
    return [id, [((row % 2 ? width - 1 - column : column) - (width - 1) / 2) * step,
      (row - (width - 1) / 2) * step]];
  }));
  const points = connected.map((id) => ({ id, x: seedPositions.get(id)![0], y: seedPositions.get(id)![1] }));
  // Only <=256 coarse anchors are relaxed in the already-existing worker.
  // No hub enters native positions/links; all original documents continue
  // moving under Cosmos GPU springs, drag/pin and pause/resume after this seed.
  const simulation = forceSimulation(points).stop()
    .randomSource(seededRandom(workspaceCommunitySeed(workspaceId)))
    .alphaDecay(.04)
    .force("links", forceLink<typeof points[number], typeof links[number]>(links).id((point) => point.id)
      .distance((link) => link.distance)
      .strength((link) => Math.min(.45, .08 + .14 * Math.sqrt(link.affinity))))
    .force("charge", forceManyBody<typeof points[number]>().strength((point) => -(radii[point.id] ** 2) * .4))
    .force("collision", forceCollide<typeof points[number]>((point) => radii[point.id] + 20).iterations(2))
    .force("center", forceCenter(0, 0));
  simulation.tick(180);
  simulation.stop();
  points.forEach((point) => { centers[point.id * 2] = point.x; centers[point.id * 2 + 1] = point.y; });
}

function roundPosition(value: number): number {
  return Math.round(value * 1_000) / 1_000;
}

function communityStartPositions(
  nodeKeys: string[],
  communityIds: Uint32Array,
  communityCount: number,
  workspaceId: string,
  weights?: PairWeights,
): Float32Array {
  const positions = new Float32Array(nodeKeys.length * 2);
  if (communityCount === 0) return positions;
  const sizes = new Uint32Array(communityCount);
  const firstMember = new Uint32Array(communityCount);
  firstMember.fill(0xffffffff);
  communityIds.forEach((community, index) => {
    sizes[community] += 1;
    if (index < firstMember[community]) firstMember[community] = index;
  });
  const radii = Float64Array.from(sizes, (size) => 24 * Math.sqrt(size) + 36);
  const globalPhase = workspaceCommunitySeed(workspaceId) / 0x100000000 * 2 * Math.PI;
  const centers = new Float64Array(communityCount * 2);
  if (communityCount > 1 && communityCount <= 12) {
    // A bounded ring makes a few relation-derived groups immediately legible.
    // At dozens of groups its circumference forces global downscaling and
    // collapses each group's internal points into a single dot.
    const sectors = Float64Array.from(radii, (radius) => radius * 2 + 80);
    const circumference = sectors.reduce((sum, sector) => sum + sector, 0);
    const ringRadius = Math.max(240, circumference / (2 * Math.PI) * 1.3);
    let precedingSector = 0;
    for (let community = 0; community < communityCount; community += 1) {
      const angle = globalPhase + (precedingSector + sectors[community] / 2)
        / circumference * 2 * Math.PI;
      centers[community * 2] = ringRadius * Math.cos(angle);
      centers[community * 2 + 1] = ringRadius * Math.sin(angle);
      precedingSector += sectors[community];
    }
  } else if (communityCount > 12) {
    // Pack many groups as size-weighted squares. This preserves their inner
    // spacing at 2k nodes and also bounds the 20k-isolate case inside Cosmos.
    const order = Array.from({ length: communityCount }, (_unused, index) => index)
      .sort((left, right) => radii[right] - radii[left] || left - right);
    const sides = Float64Array.from(radii, (radius) => radius * 2 + 80);
    const totalArea = sides.reduce((sum, side) => sum + side * side, 0);
    const widest = sides.reduce((max, side) => Math.max(max, side), 0);
    const targetWidth = Math.max(widest, Math.sqrt(totalArea) * 1.15);
    let rowX = 0;
    let rowY = 0;
    let rowHeight = 0;
    let packedWidth = 0;
    for (const community of order) {
      const side = sides[community];
      if (rowX > 0 && rowX + side > targetWidth) {
        rowY += rowHeight;
        rowX = 0;
        rowHeight = 0;
      }
      centers[community * 2] = rowX + side / 2;
      centers[community * 2 + 1] = rowY + side / 2;
      rowX += side;
      rowHeight = Math.max(rowHeight, side);
      packedWidth = Math.max(packedWidth, rowX);
    }
    const packedHeight = rowY + rowHeight;
    for (let community = 0; community < communityCount; community += 1) {
      centers[community * 2] -= packedWidth / 2;
      centers[community * 2 + 1] -= packedHeight / 2;
    }
  }
  if (weights) seedRelatedCommunityCenters(centers, sizes, radii, communityIds, weights, workspaceId);
  const nextOrdinal = new Uint32Array(communityCount);
  const phases = Float64Array.from(firstMember, (index) =>
    workspaceCommunitySeed(`${workspaceId}\u0000${nodeKeys[index]}`) / 0x100000000 * 2 * Math.PI);
  communityIds.forEach((community, index) => {
    const ordinal = nextOrdinal[community]++;
    const radius = ordinal === 0 ? 0 : 24 * Math.sqrt(ordinal);
    const angle = ordinal * GOLDEN_ANGLE + phases[community];
    positions[index * 2] = centers[community * 2] + radius * Math.cos(angle);
    positions[index * 2 + 1] = centers[community * 2 + 1] + radius * Math.sin(angle);
  });
  // Cosmos maps [0, spaceSize] to clip coordinates and clamps its GPU physics
  // to that interval. Center our graph inside its 4096-unit simulation space;
  // signed coordinates would collapse against the x=0/y=0 boundaries.
  let farthest = 0;
  for (const position of positions) farthest = Math.max(farthest, Math.abs(position));
  const scale = farthest > 1800 ? 1800 / farthest : 1;
  for (let index = 0; index < positions.length; index += 1) {
    positions[index] = roundPosition(2048 + positions[index] * scale);
  }
  return positions;
}

function compareNumber(left: number, right: number): number {
  return left - right;
}

/** Stable UTF-8 FNV-1a seed; the workspace ID, not a hidden row, seeds Louvain. */
export function workspaceCommunitySeed(workspaceId: string): number {
  let hash = 0x811c9dc5;
  for (const byte of new TextEncoder().encode(workspaceId)) {
    hash ^= byte;
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0 || 0x9e3779b9;
}

function seededRandom(seed: number): () => number {
  let state = seed;
  return () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return (state >>> 0) / 0x100000000;
  };
}

/**
 * Run only inside communityWorker in production. The function is exported for
 * deterministic unit tests; callers must not run a 20k-node layout on the UI
 * thread. Directed and parallel relations become one weighted undirected edge.
 */
export function computeWorkspaceMapCommunities(
  input: WorkspaceMapCommunityInput,
): WorkspaceMapCommunities {
  const { nodeKeys, edgeSources, edgeTargets, edgeWeights } = input;
  if (edgeSources.length !== edgeTargets.length || edgeSources.length !== edgeWeights.length) {
    throw new Error("Workspace Map community columns have mismatched edge lengths.");
  }
  for (let index = 1; index < nodeKeys.length; index += 1) {
    if (nodeKeys[index - 1] >= nodeKeys[index]) {
      throw new Error("Workspace Map community node keys must be unique and sorted.");
    }
  }
  if (nodeKeys.length === 0) {
    return {
      communityIds: new Uint32Array(0),
      communityCount: 0,
      initialPositions: new Float32Array(0),
    };
  }

  // Combining the two directions (and repeated relation kinds) before graph
  // insertion removes API row order as a source of Louvain nondeterminism.
  const weights = new Map<number, Map<number, number>>();
  for (let index = 0; index < edgeSources.length; index += 1) {
    const source = edgeSources[index];
    const target = edgeTargets[index];
    const count = edgeWeights[index];
    if (!Number.isSafeInteger(count) || count < 0) {
      throw new Error("Workspace Map community occurrence weight must be a nonnegative safe integer.");
    }
    if (source >= nodeKeys.length || target >= nodeKeys.length) {
      throw new Error("Workspace Map community edge has a missing visible endpoint.");
    }
    if (source === target || count === 0) continue;
    const lower = Math.min(source, target);
    const upper = Math.max(source, target);
    let row = weights.get(lower);
    if (!row) {
      row = new Map<number, number>();
      weights.set(lower, row);
    }
    const weight = (row.get(upper) ?? 0) + count;
    if (!Number.isSafeInteger(weight)) {
      throw new Error("Workspace Map community edge weight exceeds the safe integer range.");
    }
    row.set(upper, weight);
  }
  if (weights.size === 0) {
    // Louvain cannot join disconnected isolates. Avoid allocating a 20k-node
    // graph merely to obtain identity communities and static positions.
    const communityIds = Uint32Array.from(nodeKeys, (_key, index) => index);
    return {
      communityIds,
      communityCount: nodeKeys.length,
      initialPositions: communityStartPositions(
        nodeKeys, communityIds, nodeKeys.length, input.workspaceId,
      ),
    };
  }

  const relationEdges: WorkspaceRelationStartEdge[] = [];
  const graph = new UndirectedGraph<{ index: number }, { weight: number }>();
  nodeKeys.forEach((key, index) => graph.addNode(key, { index }));
  for (const lower of [...weights.keys()].sort(compareNumber)) {
    const row = weights.get(lower)!;
    for (const upper of [...row.keys()].sort(compareNumber)) {
      relationEdges.push({ source: lower, target: upper, weight: row.get(upper)! });
      graph.addUndirectedEdge(nodeKeys[lower], nodeKeys[upper], {
        weight: row.get(upper)!,
      });
    }
  }

  const partition = louvain(graph, {
    getEdgeWeight: "weight",
    resolution: 1.0,
    randomWalk: true,
    rng: seededRandom(workspaceCommunitySeed(input.workspaceId)),
  });
  const smallestMember = new Map<number, number>();
  nodeKeys.forEach((key, index) => {
    const oldId = partition[key];
    const previous = smallestMember.get(oldId);
    if (previous === undefined || index < previous) smallestMember.set(oldId, index);
  });
  const normalized = new Map<number, number>();
  [...smallestMember.entries()]
    .sort((left, right) => left[1] - right[1])
    .forEach(([oldId], newId) => normalized.set(oldId, newId));
  const communityIds = Uint32Array.from(nodeKeys.map((key) => normalized.get(partition[key])!));
  return {
    communityIds,
    communityCount: normalized.size,
    // At the current 2K gate, start original documents from their actual
    // relations, not index-order spirals inside fixed community circles.
    // Louvain remains a colour/summary annotation. Cosmos owns all subsequent
    // live GPU motion; this worker never supplies persistent force anchors.
    initialPositions: buildWorkspaceRelationStartPositions({
      nodeCount: nodeKeys.length,
      workspaceSeed: workspaceCommunitySeed(input.workspaceId),
      edges: relationEdges,
    }) ?? communityStartPositions(nodeKeys, communityIds, normalized.size, input.workspaceId, weights),
  };
}

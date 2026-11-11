import { workspaceBaseBodyDiameterCss } from "../shared/workspaceGlyphGeometry";
import type { GraphVisualTokens } from "../shared/graphTokens";
import { rgb } from "./cosmosColor";
import { estimateWorkspaceCountWidthCss, workspaceClusterCountText, workspaceClusterDiameterCss, type WorkspaceCountMeasure } from "./pointImages";
import { WORKSPACE_MAP_NODE_KIND, workspaceMapAdmissionCounts, type WorkspaceMapColumns } from "../map/sceneColumns";
import { isWorkspaceMapWithinBudget, WORKSPACE_MAP_DEFAULT_BUDGET, type WorkspaceMapBudget } from "../shared/workspaceMapBudget";
import { buildWorkspaceRelationLinkStrength } from "./relationPhysics";

/** The Map owns only typed GPU inputs and a bounded adjacency index, never per-node DOM. */
export type CosmosSceneBuffers = {
  positions: Float32Array;
  pointColors: Float32Array;
  pointShapes: Float32Array;
  /** Relation-based sizes: the only sizes that layout and collision may read. */
  pointSizes: Float32Array;
  /**
   * What is drawn and picked: minimum visible diameters and each cluster's
   * count disc. Forces read `pointSizes`; only collision reads a cluster's disc.
   */
  drawPointSizes: Float32Array;
  /** Collision sizes: `pointSizes`, except a cluster occupies its drawn count disc. */
  collisionPointSizes: Float32Array;
  /**
   * Per point: an index into `[agent outline, ...countImages]`, or -1. The
   * adapter turns that list into the native graph's SDF images.
   */
  pointImageIndices: Float32Array;
  pointImageSizes: Float32Array;
  /** Distinct cluster counts and the disc each is drawn in, after the agent outline image. */
  countImages: Array<{ text: string; diameterCss: number }>;
  links: Float32Array;
  linkColors: Float32Array;
  /** Legend widths relative to a link; the adapter scales them for density. */
  linkStyles: Float32Array;
  linkStrength: Float32Array;
  adjacencyOffsets: Uint32Array;
  adjacencyNodes: Uint32Array;
  adjacencyEdges: Uint32Array;
};

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

/** Presentation follows the admitted budget without altering its ink constants. */
export function isReadableWorkspaceMapScene(
  nodeCount: number,
  edgeCount: number,
  budget: WorkspaceMapBudget = WORKSPACE_MAP_DEFAULT_BUDGET,
): boolean {
  return isWorkspaceMapWithinBudget({ nodeCount, edgeCount }, budget);
}

/** Dense overview ink stays separate from small maps and unadmitted budgets. */
export function isDenseWorkspaceMapOverview(
  columns: WorkspaceMapColumns,
  budget: WorkspaceMapBudget = WORKSPACE_MAP_DEFAULT_BUDGET,
): boolean {
  const admitted = workspaceMapAdmissionCounts(columns);
  return admitted.nodeCount > 1_000
    && isReadableWorkspaceMapScene(admitted.nodeCount, admitted.edgeCount, budget);
}

/** Presentation-only colors: no edge is removed and folder metadata is unused. */
export function buildCosmosRelationColors(
  columns: WorkspaceMapColumns,
  baseColors: Float32Array,
  communityIds?: Uint32Array,
  incidentLinks: readonly number[] = [],
  budget: WorkspaceMapBudget = WORKSPACE_MAP_DEFAULT_BUDGET,
): Float32Array {
  if (baseColors.length !== columns.edgeIds.length * 4
    || (communityIds && communityIds.length !== columns.nodeIds.length)) {
    throw new Error("Workspace Map relation color columns have mismatched lengths.");
  }
  if (!isDenseWorkspaceMapOverview(columns, budget)) return baseColors;
  const colors = baseColors.slice();
  const emphasized = new Set(incidentLinks);
  for (let edge = 0; edge < columns.edgeIds.length; edge++) {
    const crossCommunity = communityIds
      && communityIds[columns.edgeSources[edge]] !== communityIds[columns.edgeTargets[edge]];
    // Cross-community lines remain visible, but need less overview ink than
    // local structure. Direct incident stars win over both density bands.
    colors[edge * 4 + 3] = emphasized.has(edge) ? .95
      : columns.edgeKinds[edge] === 1 ? (crossCommunity ? .2 : .38)
        : crossCommunity ? .18 : .35;
  }
  return colors;
}

function stableHash(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

/**
 * Native point shape 9 (generated Cosmos patch): the legend's folder/grouping
 * ring, a constant GRAPH_RING_BAND_PX band in the point colour over a
 * background-coloured disc, drawn analytically at every size and zoom.
 */
export const WORKSPACE_RING_POINT_SHAPE = 9;
/** Image list given to the native graph: the forced-colours Agent outline, then the cluster counts. */
export const WORKSPACE_AGENT_OUTLINE_IMAGE = 0;
const FIRST_COUNT_IMAGE = 1;

function writeColor(destination: Float32Array, index: number, color: [number, number, number], alpha: number) {
  const offset = index * 4;
  destination[offset] = color[0];
  destination[offset + 1] = color[1];
  destination[offset + 2] = color[2];
  destination[offset + 3] = alpha;
}

function initialPositions(columns: WorkspaceMapColumns): Float32Array {
  // A lightweight global start while relation-derived worker hints arrive.
  // Directory assignment is metadata, not a force or spatial constraint.
  const phase = (stableHash(columns.identity.workspaceId) % 360) * Math.PI / 180;
  const positions = new Float32Array(columns.nodeIds.length * 2);
  for (let node = 0; node < columns.nodeIds.length; node += 1) {
    const radius = 24 * Math.sqrt(node);
    const angle = node * GOLDEN_ANGLE + phase;
    // Cosmos 3.4's simulation and shader use [0, spaceSize] coordinates. A
    // centered signed layout is clamped to the left/bottom edges on the GPU.
    positions[node * 2] = 2048 + radius * Math.cos(angle);
    positions[node * 2 + 1] = 2048 + radius * Math.sin(angle);
  }
  let furthestFromCenter = 0;
  for (const position of positions) {
    furthestFromCenter = Math.max(furthestFromCenter, Math.abs(position - 2048));
  }
  if (furthestFromCenter > 1800) {
    const scale = 1800 / furthestFromCenter;
    for (let index = 0; index < positions.length; index += 1) {
      positions[index] = 2048 + (positions[index] - 2048) * scale;
    }
  }
  return positions;
}

export function buildCosmosSceneBuffers(
  columns: WorkspaceMapColumns,
  tokens: GraphVisualTokens,
  budget: WorkspaceMapBudget = WORKSPACE_MAP_DEFAULT_BUDGET,
  {
    forcedColors = false,
    countText = (count: number) => workspaceClusterCountText(count),
    measureCount = estimateWorkspaceCountWidthCss,
  }: {
    forcedColors?: boolean;
    /** The cluster's document count as the reader's locale writes it. */
    countText?: (count: number) => string;
    /** Width of a count in the cluster font (CSS px); a canvas measure in the browser. */
    measureCount?: WorkspaceCountMeasure;
  } = {},
): CosmosSceneBuffers {
  const count = columns.nodeIds.length;
  const edges = columns.edgeIds.length;
  const admitted = workspaceMapAdmissionCounts(columns);
  const documentCounts = columns.nodeDocumentCounts;
  // The GPU cap counts what the budget admits; folders and containment ride on top.
  if (admitted.nodeCount > 25_000 || admitted.edgeCount > 100_000 ||
      columns.nodeKinds.length !== count || columns.nodeSizes.length !== count ||
      (documentCounts !== undefined && documentCounts.length !== count) ||
      columns.nodeFolderKeys.length !== count ||
      columns.edgeSources.length !== edges || columns.edgeTargets.length !== edges ||
      columns.edgeKinds.length !== edges) {
    throw new Error("Workspace Map column budget or shape is invalid");
  }
  const pointColors = new Float32Array(count * 4);
  const pointShapes = new Float32Array(count);
  const pointSizes = new Float32Array(count);
  const context = rgb(tokens.node.context);
  const library = rgb(tokens.node.library);
  const agent = rgb(tokens.node.agent);
  // Folder ring and cluster disc share the grouping grey.
  const grouping = rgb(tokens.node.folder);
  const overviewScale = admitted.nodeCount > 1_000 ? 0.8 : 2;
  for (let node = 0; node < count; node += 1) {
    const kind = columns.nodeKinds[node];
    if (kind > 4) throw new Error("Unknown Workspace Map node kind");
    writeColor(pointColors, node, kind === 0 ? context : kind === 1 ? library : kind === 4 ? agent : grouping, 1);
    // Native Library square, legend folder ring; documents and cluster discs are circles.
    pointShapes[node] = kind === WORKSPACE_MAP_NODE_KIND.library ? 1
      : kind === WORKSPACE_MAP_NODE_KIND.folder ? WORKSPACE_RING_POINT_SHAPE : 0;
    // At a 2k-node fit, full-size points cover their short intra-community
    // links. Retain the degree ordering but reserve enough pixel space for
    // relationships in the overview; focused zoom can show the full scene.
    pointSizes[node] = admitted.nodeCount > 1_000
      ? Math.max(2, Math.min(12, columns.nodeSizes[node] * overviewScale))
      : Math.max(3, Math.min(17, columns.nodeSizes[node] * overviewScale));
  }
  // Improve visible bodies without changing force, collision or degree inputs.
  let drawPointSizes = pointSizes;
  for (let node = 0; node < count; node += 1) {
    const diameter = workspaceBaseBodyDiameterCss(pointSizes[node], count);
    if (pointSizes[node] >= diameter) continue;
    if (drawPointSizes === pointSizes) drawPointSizes = pointSizes.slice();
    drawPointSizes[node] = diameter;
  }
  // A cluster is a grey disc holding its document count: wide enough for the
  // text and never below the base, so the number stays readable. Collision
  // follows that disc; forces keep the relation-based size.
  const pointImageIndices = new Float32Array(count).fill(-1);
  const pointImageSizes = new Float32Array(count);
  const countImages: CosmosSceneBuffers["countImages"] = [];
  const countImageIndex = new Map<string, number>();
  let collisionPointSizes = pointSizes;
  for (let node = 0; node < count; node += 1) {
    const kind = columns.nodeKinds[node];
    if (kind === WORKSPACE_MAP_NODE_KIND.agent && forcedColors) {
      pointImageIndices[node] = WORKSPACE_AGENT_OUTLINE_IMAGE;
      pointImageSizes[node] = drawPointSizes[node];
    }
    if (kind !== WORKSPACE_MAP_NODE_KIND.cluster) continue;
    const text = countText(documentCounts?.[node] ?? 1);
    const diameterCss = Math.max(drawPointSizes[node], workspaceClusterDiameterCss(text, measureCount));
    if (drawPointSizes === pointSizes) drawPointSizes = pointSizes.slice();
    if (collisionPointSizes === pointSizes) collisionPointSizes = pointSizes.slice();
    drawPointSizes[node] = collisionPointSizes[node] = diameterCss;
    const key = `${text}\u0000${diameterCss}`;
    let image = countImageIndex.get(key);
    if (image === undefined) {
      image = FIRST_COUNT_IMAGE + countImages.length;
      countImageIndex.set(key, image);
      countImages.push({ text, diameterCss });
    }
    pointImageIndices[node] = image;
    pointImageSizes[node] = diameterCss;
  }
  const links = new Float32Array(edges * 2);
  const linkColors = new Float32Array(edges * 4);
  const linkStyles = new Float32Array(edges);
  const offsets = new Uint32Array(count + 1);
  const solid = rgb(tokens.edge.link);
  const citation = rgb(tokens.edge.citation);
  const membership = rgb(tokens.edge.membership);
  const readableScene = isReadableWorkspaceMapScene(admitted.nodeCount, admitted.edgeCount, budget);
  const denseOverview = isDenseWorkspaceMapOverview(columns, budget);
  for (let edge = 0; edge < edges; edge += 1) {
    const source = columns.edgeSources[edge];
    const target = columns.edgeTargets[edge];
    const kind = columns.edgeKinds[edge];
    if (source >= count || target >= count || kind > 2) throw new Error("Invalid Workspace Map edge");
    links[edge * 2] = source;
    links[edge * 2 + 1] = target;
    // Legend strokes: solid link, dashed citation, short-dash containment (Cosmos style per kind).
    linkStyles[edge] = kind;
    const alpha = denseOverview ? (kind === 1 ? .38 : .35)
      : kind === 1 ? (readableScene ? 0.4 : 0.35) : (readableScene ? 0.45 : 0.5);
    writeColor(
      linkColors,
      edge,
      kind === 1 ? citation : kind === 2 ? membership : solid,
      alpha,
    );
    offsets[source + 1] += 1;
    offsets[target + 1] += 1;
  }
  for (let node = 1; node <= count; node += 1) offsets[node] += offsets[node - 1];
  const cursors = offsets.slice(0, count);
  const adjacencyNodes = new Uint32Array(edges * 2);
  const adjacencyEdges = new Uint32Array(edges * 2);
  for (let edge = 0; edge < edges; edge += 1) {
    const source = columns.edgeSources[edge];
    const target = columns.edgeTargets[edge];
    let slot = cursors[source]++;
    adjacencyNodes[slot] = target;
    adjacencyEdges[slot] = edge;
    slot = cursors[target]++;
    adjacencyNodes[slot] = source;
    adjacencyEdges[slot] = edge;
  }
  return {
    positions: initialPositions(columns), pointColors, pointShapes, pointSizes, drawPointSizes, collisionPointSizes,
    pointImageIndices, pointImageSizes, countImages,
    links, linkColors, linkStyles, linkStrength: buildWorkspaceRelationLinkStrength(columns),
    adjacencyOffsets: offsets, adjacencyNodes, adjacencyEdges,
  };
}

export function incidentStar(buffers: CosmosSceneBuffers, center: number): {
  points: number[];
  links: number[];
} {
  if (!Number.isInteger(center) || center < 0 || center + 1 >= buffers.adjacencyOffsets.length) {
    return { points: [], links: [] };
  }
  const points = [center];
  const links = [];
  const visited = new Set([center]);
  for (let index = buffers.adjacencyOffsets[center]; index < buffers.adjacencyOffsets[center + 1]; index += 1) {
    const neighbor = buffers.adjacencyNodes[index];
    if (!visited.has(neighbor)) {
      visited.add(neighbor);
      points.push(neighbor);
    }
    links.push(buffers.adjacencyEdges[index]);
  }
  return { points, links };
}

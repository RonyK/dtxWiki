import { workspaceBaseBodyDiameterCss } from "../shared/workspaceGlyphGeometry";
import type { GraphVisualTokens } from "../shared/graphTokens";
import { FORCED_OUTLINE_INSETS, RING_BAND_FACTOR } from "../shared/nodeShapes";
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
   * What is drawn and picked, including minimum visible diameters and cluster
   * emphasis. Layout and collision continue to read only `pointSizes`.
   */
  drawPointSizes: Float32Array;
  /** Per point: an image index (cluster ring, forced-colours Agent outline) or -1 (no image). */
  pointImageIndices: Float32Array;
  pointImageSizes: Float32Array;
  links: Float32Array;
  linkColors: Float32Array;
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

/** Image list given to the native graph: the grouping ring, then the Agent outline gap. */
export const WORKSPACE_CLUSTER_RING_IMAGE = 0;
export const WORKSPACE_AGENT_OUTLINE_IMAGE = 1;
// Close to the drawn sizes: the atlas is sampled without mipmaps, so a much
// larger source would alias into a dotted ring when scaled down.
const RING_IMAGE_SIZE = 40;

/** An antialiased annulus between two radii (fractions of the half-size). */
function annulusImage(color: string, innerFactor: number, outerFactor: number): ImageData | null {
  if (typeof ImageData === "undefined") return null;
  const [red, green, blue] = rgb(color).map((value) => Math.round(value * 255));
  const size = RING_IMAGE_SIZE;
  const half = size / 2;
  const outer = half * outerFactor;
  const inner = half * innerFactor;
  const data = new Uint8ClampedArray(size * size * 4);
  const samples = 4;
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      let covered = 0;
      for (let sy = 0; sy < samples; sy += 1) {
        for (let sx = 0; sx < samples; sx += 1) {
          const distance = Math.hypot(x + (sx + .5) / samples - half, y + (sy + .5) / samples - half);
          if (distance <= outer && distance >= inner) covered += 1;
        }
      }
      const offset = (y * size + x) * 4;
      data[offset] = red;
      data[offset + 1] = green;
      data[offset + 2] = blue;
      data[offset + 3] = Math.round((covered / (samples * samples)) * 255);
    }
  }
  return new ImageData(data, size, size);
}

/**
 * The images the Map draws over native discs. A cluster is a grouping, drawn
 * like a Local folder: a grey ring over a surface-coloured disc that keeps its
 * picking footprint. In forced colours an Agent disc gets a surface-coloured
 * gap, the same thin outline the Local graph and the legends draw.
 */
export function buildWorkspacePointImages(tokens: GraphVisualTokens): ImageData[] | null {
  const ring = annulusImage(tokens.node.folder, 1 - RING_BAND_FACTOR, 1);
  const outline = annulusImage(tokens.surface, FORCED_OUTLINE_INSETS.circle[1], FORCED_OUTLINE_INSETS.circle[0]);
  return ring && outline ? [ring, outline] : null;
}

/** CSS tokens are resolved once per scene; WebGL receives normalized RGBA. */
function rgb(color: string): [number, number, number] {
  const hex = color.trim().match(/^#([\da-f]{3}|[\da-f]{6})$/i);
  if (hex) {
    const value = hex[1].length === 3
      ? [...hex[1]].map((digit) => digit + digit).join("")
      : hex[1];
    return [0, 2, 4].map((start) => Number.parseInt(value.slice(start, start + 2), 16) / 255) as [number, number, number];
  }
  const functional = color.trim().match(/^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
  if (functional) return [1, 2, 3].map((index) => Math.min(255, Number(functional[index])) / 255) as [number, number, number];
  // A forced-colors system keyword is not a WebGL color; use high-contrast ink.
  return [0, 0, 0];
}

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

/**
 * A cluster stands for many documents but is sized by its few links, so the
 * overview draws it no larger than a well-linked note (6.8 px). From ten
 * documents its glyph grows with log10(members), bounded, and never below its
 * relation-based size. The ceiling stays inside the native picker's 12 px
 * accessible target. Render emphasis only: it lives in `drawPointSizes`, so
 * columns, fingerprint, layout, collision and every cluster-free
 * scene are untouched.
 */
const CLUSTER_EMPHASIS = { fromDocuments: 10, fullAtDocuments: 10_000, minCss: 7, maxCss: 22 } as const;

function clusterDiameterCss(documents: number): number {
  const { fromDocuments, fullAtDocuments, minCss, maxCss } = CLUSTER_EMPHASIS;
  if (!(documents >= fromDocuments)) return 0;
  const progress = Math.log(Math.min(documents, fullAtDocuments) / fromDocuments)
    / Math.log(fullAtDocuments / fromDocuments);
  return minCss + (maxCss - minCss) * progress;
}

export function buildCosmosSceneBuffers(
  columns: WorkspaceMapColumns,
  tokens: GraphVisualTokens,
  budget: WorkspaceMapBudget = WORKSPACE_MAP_DEFAULT_BUDGET,
  { forcedColors = false }: { forcedColors?: boolean } = {},
): CosmosSceneBuffers {
  const count = columns.nodeIds.length;
  const edges = columns.edgeIds.length;
  const admitted = workspaceMapAdmissionCounts(columns);
  const documentCounts = columns.nodeDocumentCounts;
  if (count > 25_000 || edges > 100_000 ||
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
  const folder = rgb(tokens.node.folder);
  const surface = rgb(tokens.surface);
  const overviewScale = admitted.nodeCount > 1_000 ? 0.8 : 2;
  for (let node = 0; node < count; node += 1) {
    const kind = columns.nodeKinds[node];
    if (kind > 4) throw new Error("Unknown Workspace Map node kind");
    // A cluster disc takes the surface colour; its grey ring is an image on top.
    writeColor(pointColors, node, kind === 0 ? context : kind === 1 ? library : kind === 4 ? agent
      : kind === WORKSPACE_MAP_NODE_KIND.folder ? folder : surface, 1);
    // Native Library square, TreeRail folder diamond; documents and cluster discs are circles.
    pointShapes[node] = kind === WORKSPACE_MAP_NODE_KIND.library ? 1 : kind === WORKSPACE_MAP_NODE_KIND.folder ? 3 : 0;
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
  if (documentCounts) {
    for (let node = 0; node < count; node += 1) {
      if (columns.nodeKinds[node] !== WORKSPACE_MAP_NODE_KIND.cluster) continue;
      const emphasised = clusterDiameterCss(documentCounts[node]);
      if (!(emphasised > drawPointSizes[node])) continue;
      if (drawPointSizes === pointSizes) drawPointSizes = pointSizes.slice();
      drawPointSizes[node] = emphasised;
    }
  }
  const pointImageIndices = new Float32Array(count).fill(-1);
  const pointImageSizes = new Float32Array(count);
  for (let node = 0; node < count; node += 1) {
    const kind = columns.nodeKinds[node];
    const image = kind === WORKSPACE_MAP_NODE_KIND.cluster ? WORKSPACE_CLUSTER_RING_IMAGE
      : forcedColors && kind === WORKSPACE_MAP_NODE_KIND.agent ? WORKSPACE_AGENT_OUTLINE_IMAGE : -1;
    if (image < 0) continue;
    pointImageIndices[node] = image;
    pointImageSizes[node] = drawPointSizes[node];
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
    // Solid link, dashed citation, dotted TreeRail containment.
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
    positions: initialPositions(columns), pointColors, pointShapes, pointSizes, drawPointSizes,
    pointImageIndices, pointImageSizes,
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

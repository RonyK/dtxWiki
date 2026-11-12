import { GRAPH_LINE_OPACITY, GRAPH_NODE_VISUALS, type GraphVisualTokens } from "../graphTokens";
import { nativeGlyphDiameterScale, nativeGlyphShape } from "../workspaceGlyphGeometry";
import { rgb } from "../../workspace/cosmosColor";
import type { CosmosSceneColumns } from "./sceneTypes";
import type { CosmosSceneBuffers } from "./buffers";
import { estimateWorkspaceCountWidthCss, workspaceClusterCountText, workspaceClusterDiameterCss,
  type WorkspaceCountMeasure } from "./glyphImages";

/** The same original indexed rows drive drawing, picking and interaction stars. */
export function buildCosmosAdjacency(columns: CosmosSceneColumns) {
  const nodes = columns.nodeIds.length, edges = columns.edgeIds.length;
  if (columns.edgeSources.length !== edges || columns.edgeTargets.length !== edges) {
    throw new Error("Cosmos relation endpoints have mismatched lengths");
  }
  const links = new Float32Array(edges * 2), offsets = new Uint32Array(nodes + 1);
  for (let edge = 0; edge < edges; edge++) {
    const source = columns.edgeSources[edge], target = columns.edgeTargets[edge];
    if (source >= nodes || target >= nodes) throw new Error("Invalid Cosmos relation endpoint");
    links[edge * 2] = source; links[edge * 2 + 1] = target;
    offsets[source + 1]++; offsets[target + 1]++;
  }
  for (let node = 1; node <= nodes; node++) offsets[node] += offsets[node - 1];
  const cursors = offsets.slice(0, nodes), adjacencyNodes = new Uint32Array(edges * 2), adjacencyEdges = new Uint32Array(edges * 2);
  for (let edge = 0; edge < edges; edge++) {
    const source = columns.edgeSources[edge], target = columns.edgeTargets[edge];
    let slot = cursors[source]++;
    adjacencyNodes[slot] = target; adjacencyEdges[slot] = edge;
    slot = cursors[target]++;
    adjacencyNodes[slot] = source; adjacencyEdges[slot] = edge;
  }
  return { links, adjacencyOffsets: offsets, adjacencyNodes, adjacencyEdges };
}

/**
 * Prepare glyph presentation without a surface-specific size or ink tier.
 * The supplying binding owns positions, force strengths and any Map overview
 * presentation. Context can exercise this contract before its screen migrates.
 */
export function buildCosmosPresentationBuffers(
  columns: CosmosSceneColumns,
  tokens: GraphVisualTokens,
  options: { forcedColors?: boolean; positions?: Float32Array; linkStrength?: Float32Array;
    countText?(count: number): string; measureCount?: WorkspaceCountMeasure } = {},
): CosmosSceneBuffers {
  const count = columns.nodeIds.length, edges = columns.edgeIds.length;
  if (columns.nodeVisualKinds?.length !== count || columns.edgeVisualKinds?.length !== edges
    || columns.nodeSizes.length !== count || columns.nodeSizes.some(size => !Number.isFinite(size) || size <= 0)
    || options.positions && options.positions.length !== count * 2
    || options.linkStrength && options.linkStrength.length !== edges) {
    throw new Error("Cosmos visual columns have mismatched lengths or invalid sizes");
  }
  const pointColors = new Float32Array(count * 4), pointShapes = new Float32Array(count);
  const pointSizes = new Float32Array(count), pointImageIndices = new Float32Array(count).fill(-1);
  const pointImageSizes = new Float32Array(count);
  const outlineShapes = columns.nodeVisualKinds.includes("agentVersion") ? [0, 2] : [0];
  let drawPointSizes = pointSizes, collisionPointSizes = pointSizes;
  const countImages: CosmosSceneBuffers["countImages"] = [], countImageIndex = new Map<string, number>();
  columns.nodeVisualKinds.forEach((kind, index) => {
    const visual = GRAPH_NODE_VISUALS[kind], shape = nativeGlyphShape(visual.shape);
    pointColors.set([...rgb(tokens.node[visual.color]), 1], index * 4);
    pointShapes[index] = shape;
    pointSizes[index] = columns.nodeSizes[index] * nativeGlyphDiameterScale(visual.shape);
    if (options.forcedColors && visual.forcedOutline) {
      pointImageIndices[index] = outlineShapes.indexOf(shape);
      pointImageSizes[index] = pointSizes[index];
    }
  });
  columns.nodeVisualKinds.forEach((kind, index) => {
    if (kind === "cluster") {
      const text = (options.countText ?? workspaceClusterCountText)(columns.nodeDocumentCounts?.[index] ?? 1);
      const diameterCss = Math.max(pointSizes[index], workspaceClusterDiameterCss(text,
        options.measureCount ?? estimateWorkspaceCountWidthCss));
      if (drawPointSizes === pointSizes) drawPointSizes = pointSizes.slice();
      if (collisionPointSizes === pointSizes) collisionPointSizes = pointSizes.slice();
      drawPointSizes[index] = collisionPointSizes[index] = diameterCss;
      const key = `${text}\u0000${diameterCss}`;
      let image = countImageIndex.get(key);
      if (image === undefined) {
        image = outlineShapes.length + countImages.length;
        countImages.push({ text, diameterCss }); countImageIndex.set(key, image);
      }
      pointImageIndices[index] = image; pointImageSizes[index] = diameterCss;
    }
  });
  const linkColors = new Float32Array(edges * 4);
  columns.edgeVisualKinds.forEach((kind, index) => {
    linkColors.set([...rgb(tokens.edge[kind]), options.forcedColors ? 1
      : kind === "citation" ? GRAPH_LINE_OPACITY.citation : GRAPH_LINE_OPACITY.line], index * 4);
  });
  return {
    positions: options.positions ?? new Float32Array(count * 2),
    pointColors, pointShapes, pointSizes, drawPointSizes, collisionPointSizes,
    pointImageIndices, pointImageSizes, countImages, outlineShapes,
    ...buildCosmosAdjacency(columns), linkColors,
    linkStyles: new Float32Array(edges),
    linkStrength: options.linkStrength ?? new Float32Array(edges).fill(1),
  };
}

/** Indexed GPU buffers and bounded adjacency shared by Cosmos surfaces. */
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
  /** Optional forced-colour outline atlas; legacy Map images start with a circle. */
  outlineShapes?: number[];
  links: Float32Array;
  linkColors: Float32Array;
  /** Native styles supplied by the existing surface binding. */
  linkStyles: Float32Array;
  linkStrength: Float32Array;
  adjacencyOffsets: Uint32Array;
  adjacencyNodes: Uint32Array;
  adjacencyEdges: Uint32Array;
};

export function sameCosmosColumn(left: ArrayLike<unknown>, right: ArrayLike<unknown>): boolean {
  if (left.length !== right.length) return false;
  for (let index = 0; index < left.length; index++) if (left[index] !== right[index]) return false;
  return true;
}

/** Metadata-only scene updates repaint only when the actual GPU presentation changed. */
export function sameCosmosPresentation(left: CosmosSceneBuffers, right: CosmosSceneBuffers): boolean {
  const fields = ["pointColors", "pointShapes", "drawPointSizes", "collisionPointSizes", "pointImageIndices", "pointImageSizes",
    "linkColors", "linkStyles"] as const;
  return fields.every(field => sameCosmosColumn(left[field], right[field]))
    && JSON.stringify(left.outlineShapes) === JSON.stringify(right.outlineShapes)
    && JSON.stringify(left.countImages) === JSON.stringify(right.countImages);
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

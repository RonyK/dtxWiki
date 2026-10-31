import type { WorkspaceMapColumns } from "../map/sceneColumns";

/** Bound authored influence without making a high-degree hub a rigid attractor. */
export const WORKSPACE_RELATION_LINK_WEIGHT_CAP = 16;

/** Immutable graph-space calibration, not the current camera's pixel radius. */
export type WorkspaceRelationCollisionBasis = { radius: number; padding: number };

/**
 * Match pinned Cosmos3.4 Zoom.getTransform for this adapter's .08 Fit padding
 * and rescalePositions:false. Its scaleX/Y only translate/flip world positions;
 * native integer CSS dimensions (not DPR backing pixels) determine the scale.
 * Pure visible-seed inputs keep worker timing, camera and pins out of physics.
 */
export function buildWorkspaceRelationCollisionBasis(
  seed: Float32Array,
  pointSizes: Float32Array,
  width: number,
  height: number,
): WorkspaceRelationCollisionBasis | null {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0
    || seed.length !== pointSizes.length * 2 || !seed.length
    || !pointSizes.every((size) => Number.isFinite(size) && size > 0)) return null;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (let index = 0; index < seed.length; index += 2) {
    const x = seed[index], y = seed[index + 1];
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return null;
  const dx = maxX - minX, dy = maxY - minY;
  // Cosmos expands an exactly-zero extent by one unit, not all subunit spans.
  const inset = 1 - 2 * .08;
  const zoom = Math.max(.001, Math.min(width * inset / (dx === 0 ? 1 : dx),
    height * inset / (dy === 0 ? 1 : dy)));
  const largest = pointSizes.reduce((maximum, size) => Math.max(maximum, size), 0);
  return { radius: largest / (2 * zoom), padding: 1 / zoom };
}

/**
 * Cosmos 3.4.0 replaces, rather than multiplies, its default strength with a
 * custom column, then takes the square root (ForceLink/index.ts:62-70). Keep
 * that degree normalization exactly for weight=1 and only amplify authored
 * occurrences. No folder, position, label or hidden-entity input participates.
 */
export function buildWorkspaceRelationLinkStrength(columns: WorkspaceMapColumns): Float32Array {
  const edges = columns.edgeIds.length;
  if (columns.edgeWeights.length !== edges || columns.edgeSources.length !== edges
    || columns.edgeTargets.length !== edges) throw new Error("Invalid relation physics columns");
  const degree = new Uint32Array(columns.nodeIds.length);
  for (let edge = 0; edge < edges; edge++) {
    const source = columns.edgeSources[edge], target = columns.edgeTargets[edge];
    const weight = columns.edgeWeights[edge];
    if (source >= degree.length || target >= degree.length || !Number.isSafeInteger(weight) || weight < 1) {
      throw new Error("Invalid relation physics endpoint or occurrence weight");
    }
    degree[source]++; degree[target]++;
  }
  return Float32Array.from(columns.edgeWeights, (weight, edge) => {
    const minimumDegree = Math.max(1, Math.min(degree[columns.edgeSources[edge]], degree[columns.edgeTargets[edge]]));
    return Math.min(WORKSPACE_RELATION_LINK_WEIGHT_CAP, 1 + Math.log2(weight)) / minimumDegree;
  });
}

export type WorkspaceRelationForceConfig = Readonly<{
  simulationGravity: number;
  simulationRepulsion: number;
  simulationLinkSpring: number;
  simulationLinkDistance: number;
  simulationLinkDistRandomVariationRange: [number, number];
}>;

/**
 * Convert pinned Cosmos3.4's default profile (installed source-map
 * src/variables.ts: gravity .25, spring 1, repulsion 1, link distance 10).
 * Inverse-distance charge has squared-length units; the other coefficients
 * stay unchanged. Deterministic rest [1,1] replaces the SDK's [1,1.2] jitter.
 * This is a dimensional-profile candidate, not validated 2K UX acceptance.
 */
export function buildWorkspaceRelationForceConfig(
  basis: WorkspaceRelationCollisionBasis,
): WorkspaceRelationForceConfig {
  if (!Number.isFinite(basis.radius) || basis.radius <= 0
    || !Number.isFinite(basis.padding) || basis.padding < 0) {
    throw new Error("Invalid relation force collision basis");
  }
  // One collision diameter of breathing room, in the scene's immutable units.
  const distance = 4 * (basis.radius + basis.padding);
  const scale = distance / 10;
  const repulsion = scale ** 2;
  // GPU uniforms are Float32: reject both overflow and lost positive charge.
  for (const value of [distance, repulsion]) {
    if (!Number.isFinite(value) || !Number.isFinite(Math.fround(value)) || Math.fround(value) <= 0) {
      throw new Error("Relation force units are not representable in Float32");
    }
  }
  return {
    simulationGravity: .25,
    simulationRepulsion: repulsion,
    simulationLinkSpring: 1,
    simulationLinkDistance: distance,
    simulationLinkDistRandomVariationRange: [1, 1],
  };
}

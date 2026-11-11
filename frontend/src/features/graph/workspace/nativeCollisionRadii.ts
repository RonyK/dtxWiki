import { workspaceNominalGlyphRadiusCss } from "../shared/workspaceGlyphGeometry";
import type { WorkspaceRelationCollisionBasis } from "./relationPhysics";

/**
 * Convert each actual glyph to the existing immutable seed-Fit world units.
 * `pointSizes` (the basis's relation sizes) fixes the reference zoom; each
 * body is `collisionSizes`, which differs only where a cluster's count disc
 * is wider.
 */
export function buildWorkspaceNativeCollisionRadii(
  pointSizes: Float32Array, pointShapes: Float32Array, basis: WorkspaceRelationCollisionBasis, maxDiameterCss = Infinity,
  collisionSizes: Float32Array = pointSizes,
): Float32Array {
  if (!pointSizes.length || pointSizes.length !== pointShapes.length || collisionSizes.length !== pointSizes.length
    || !Number.isFinite(basis.radius) || basis.radius <= 0
    || !(maxDiameterCss > 0)) throw new Error("Invalid native collision geometry");
  const largest = pointSizes.reduce((maximum, size) => Math.max(maximum, size), 0);
  // The existing basis is largestDiameter / (2 * referenceZoom). Preserve it
  // for force coefficients; only collision replaces that largest body per row.
  const inverseZoom = 2 * basis.radius / largest;
  // Match the native fixed-size shader at the immutable reference Fit, also
  // for compact seeds. Later user zoom never changes collision geometry.
  const zoomFactor = Math.min(5, Math.max(1, .01 / inverseZoom));
  const radii = Float32Array.from(collisionSizes, (diameter, index) =>
    workspaceNominalGlyphRadiusCss(Math.min(diameter * zoomFactor, maxDiameterCss), pointShapes[index]) * inverseZoom);
  if (!radii.every(radius => Number.isFinite(radius) && radius > 0)) throw new Error("Invalid native collision radius");
  return radii;
}

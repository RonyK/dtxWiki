import { workspaceNominalGlyphRadiusCss } from "./forceAtlas2Units";
import type { WorkspaceRelationCollisionBasis } from "./relationPhysics";

/** Convert each actual glyph to the existing immutable seed-Fit world units. */
export function buildWorkspaceNativeCollisionRadii(
  pointSizes: Float32Array, pointShapes: Float32Array, basis: WorkspaceRelationCollisionBasis, maxDiameterCss = Infinity,
): Float32Array {
  if (!pointSizes.length || pointSizes.length !== pointShapes.length
    || !Number.isFinite(basis.radius) || basis.radius <= 0
    || !(maxDiameterCss > 0)) throw new Error("Invalid native collision geometry");
  const largest = pointSizes.reduce((maximum, size) => Math.max(maximum, size), 0);
  // The existing basis is largestDiameter / (2 * referenceZoom). Preserve it
  // for force coefficients; only collision replaces that largest body per row.
  const inverseZoom = 2 * basis.radius / largest;
  // Match the native fixed-size shader at the immutable reference Fit, also
  // for compact seeds. Later user zoom never changes collision geometry.
  const zoomFactor = Math.min(5, Math.max(1, .01 / inverseZoom));
  const radii = Float32Array.from(pointSizes, (diameter, index) =>
    workspaceNominalGlyphRadiusCss(Math.min(diameter * zoomFactor, maxDiameterCss), pointShapes[index]) * inverseZoom);
  if (!radii.every(radius => Number.isFinite(radius) && radius > 0)) throw new Error("Invalid native collision radius");
  return radii;
}

import type { WorkspaceRelationForceConfig } from "./relationPhysics";

/** The pinned native profile in canonical world units, independent of glyphs/Fit. */
export function buildWorkspaceNativeCanonicalForceConfig(nativeScale: number): WorkspaceRelationForceConfig {
  if (!Number.isFinite(nativeScale) || nativeScale <= 0 || nativeScale > 1) {
    throw new RangeError("Native force scale must be positive and at most one");
  }
  const distance = 10 * nativeScale;
  const charge = nativeScale ** 2;
  if (Math.fround(distance) <= 0 || Math.fround(charge) <= 0) {
    throw new RangeError("Native force units must remain positive Float32");
  }
  return {
    simulationGravity: .25,
    simulationRepulsion: charge,
    simulationLinkSpring: 1,
    simulationLinkDistance: distance,
    simulationLinkDistRandomVariationRange: [1, 1],
  };
}

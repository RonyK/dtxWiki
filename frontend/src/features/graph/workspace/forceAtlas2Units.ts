/** Scene-owned units for a complete FA2 driver, independent of camera updates. */
export type WorkspaceForceAtlas2Units = Readonly<{
  nodeCount: number;
  origin: readonly [number, number];
  referenceViewport: Readonly<{ width: number; height: number }>;
  referenceZoom: number;
  /** Preserve the reference's original scale: FA2 log(1+d) is not scale-covariant. */
  rendererUnitsPerModelUnit: 1;
  nominalRadiusCss: readonly number[];
  /** Circumscribed glyph radius plus half the pair gap, in model units. */
  collisionRadiusModel: readonly number[];
  pairGapCss: number;
}>;

function requirePositiveFloat32(value: number, name: string): number {
  const packed = Math.fround(value);
  if (!Number.isFinite(packed) || packed <= 0) throw new Error(`${name} must be positive finite Float32`);
  return packed;
}

function requirePositionRows(positions: ArrayLike<number>, count: number): void {
  if (positions.length !== count * 2) throw new Error("Position rows disagree with scene units");
  for (let index = 0; index < positions.length; index++) {
    if (!Number.isFinite(positions[index]) || !Number.isFinite(Math.fround(positions[index]))) {
      throw new Error("Finite Float32 coordinates required");
    }
  }
}

function canonicalReferenceZoom(positions: ArrayLike<number>, width: number, height: number): number {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (let index = 0; index < positions.length; index += 2) {
    minX = Math.min(minX, positions[index]); maxX = Math.max(maxX, positions[index]);
    minY = Math.min(minY, positions[index + 1]); maxY = Math.max(maxY, positions[index + 1]);
  }
  const dx = maxX - minX, dy = maxY - minY;
  return Math.max(.001, Math.min(width * .84 / (dx === 0 ? 1 : dx), height * .84 / (dy === 0 ? 1 : dy)));
}

/** Shared native Circle/Square nominal geometry for CPU and GPU collision inputs. */
export function workspaceNominalGlyphRadiusCss(diameter: number, shape: number): number {
  requirePositiveFloat32(diameter, "Native CSS diameter");
  if (shape !== 0 && shape !== 1) throw new Error("Unknown native glyph shape");
  return diameter * (shape === 0 ? .5 : .4 * Math.SQRT2);
}

/**
 * Takes the accepted scene seed and actual native glyph diameters in CSS pixels,
 * NOT current camera coordinates, theme, folders, DPR or normalized edge weights.
 * Circles have radius d/2; Cosmos squares have half-side .4d (not .5d).
 * Square disks conservatively enclose the nominal corners, excluding AA/rings.
 * The default 2px pair gap preserves the existing 1px padding on each node.
 * Fixed CSS-size glyphs may overlap after zooming below this reference Fit: no
 * camera-dependent physics recalibration or false nonoverlap guarantee is made.
 */
export function buildWorkspaceForceAtlas2Units(input: Readonly<{
  positions: ArrayLike<number>;
  diametersCss: ArrayLike<number>;
  pointShapes: ArrayLike<number>;
  origin: readonly [number, number];
  width: number;
  height: number;
  pairGapCss?: number;
}>): WorkspaceForceAtlas2Units {
  const count = input.diametersCss.length;
  if (!Number.isSafeInteger(count) || count <= 0 || input.pointShapes.length !== count) throw new Error("Glyph rows disagree with scene units");
  requirePositionRows(input.positions, count);
  const { width, height } = input;
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new Error("Positive integer CSS viewport required");
  }
  if (input.origin.length !== 2 || !input.origin.every(Number.isFinite)) throw new Error("Finite scene origin required");
  requirePositionRows(input.origin, 1);
  const pairGapCss = input.pairGapCss ?? 2;
  if (!Number.isFinite(pairGapCss) || pairGapCss < 0) throw new Error("Nonnegative CSS pair gap required");
  const nominalRadiusCss: number[] = [];
  for (let index = 0; index < count; index++) {
    const diameter = input.diametersCss[index], shape = input.pointShapes[index];
    nominalRadiusCss.push(workspaceNominalGlyphRadiusCss(diameter, shape));
  }
  // Pinned native Fit uses .08 padding, substitutes 1 only for zero spans, and
  // clamps the minimum zoom to .001. CSS viewport dimensions exclude DPR.
  const referenceZoom = canonicalReferenceZoom(input.positions, width, height);
  // Normalizing the largest glyph to one model unit would also change log(1+d)
  // and the adaptive integration; it is not a harmless renderer conversion.
  // Keep the preceding official numerical reference's coordinateScale=1.
  const rendererUnitsPerModelUnit = 1;
  const collisionRadiusModel = nominalRadiusCss.map(radius =>
    requirePositiveFloat32((radius + pairGapCss / 2) / referenceZoom, "Model collision radius"));
  requirePositiveFloat32(2 * Math.max(...collisionRadiusModel), "Model pair clearance");
  return Object.freeze({ nodeCount: count,
    origin: Object.freeze([input.origin[0], input.origin[1]]) as readonly [number, number],
    referenceViewport: Object.freeze({ width, height }), referenceZoom, rendererUnitsPerModelUnit,
    nominalRadiusCss: Object.freeze(nominalRadiusCss), collisionRadiusModel: Object.freeze(collisionRadiusModel), pairGapCss });
}

/** Admit only immutable, internally consistent same-scene remount units. */
export function isReusableWorkspaceForceAtlas2Units(
  candidate: unknown, nodeCount: number,
): candidate is WorkspaceForceAtlas2Units {
  if (!candidate || typeof candidate !== "object" || !Object.isFrozen(candidate)) return false;
  const units = candidate as WorkspaceForceAtlas2Units;
  const positiveFloat32 = (value: number) => Number.isFinite(value) && value > 0
    && Number.isFinite(Math.fround(value)) && Math.fround(value) > 0;
  if (units.nodeCount !== nodeCount || nodeCount <= 0 || units.rendererUnitsPerModelUnit !== 1
    || !Array.isArray(units.origin) || units.origin.length !== 2 || !Object.isFrozen(units.origin)
    || !units.origin.every(value => Number.isFinite(value) && Number.isFinite(Math.fround(value)))
    || !units.referenceViewport || !Object.isFrozen(units.referenceViewport)
    || ![units.referenceViewport.width, units.referenceViewport.height].every(value => Number.isInteger(value) && value > 0)
    || !Number.isFinite(units.referenceZoom) || units.referenceZoom < .001
    || !Number.isFinite(units.pairGapCss) || units.pairGapCss < 0
    || !Array.isArray(units.nominalRadiusCss) || !Object.isFrozen(units.nominalRadiusCss)
    || !Array.isArray(units.collisionRadiusModel) || !Object.isFrozen(units.collisionRadiusModel)
    || units.nominalRadiusCss.length !== nodeCount || units.collisionRadiusModel.length !== nodeCount
    || !units.nominalRadiusCss.every(positiveFloat32) || !units.collisionRadiusModel.every(positiveFloat32)
    || !positiveFloat32(2 * Math.max(...units.collisionRadiusModel))) return false;
  return units.collisionRadiusModel.every((radius, index) => radius
    === Math.fround((units.nominalRadiusCss[index] + units.pairGapCss / 2) / units.referenceZoom));
}

/**
 * One structural-handoff calibration, never a live-camera adjustment. Keep the
 * seed record intact and freeze a separate contact record at its original CSS
 * viewport. A contracted layout must not shrink its existing physical radii.
 * Subsequent expansion or zoom below this reference remains a distinct visual
 * boundary, not a claim of arbitrary-zoom nonoverlap.
 */
export function buildWorkspaceForceAtlas2ContactUnits(
  seed: WorkspaceForceAtlas2Units, positions: ArrayLike<number>,
): WorkspaceForceAtlas2Units {
  if (!isReusableWorkspaceForceAtlas2Units(seed, seed.nodeCount)) throw new Error("Valid immutable seed units required for contact");
  requirePositionRows(positions, seed.nodeCount);
  const boundaryZoom = canonicalReferenceZoom(positions, seed.referenceViewport.width, seed.referenceViewport.height);
  const referenceZoom = Math.min(seed.referenceZoom, boundaryZoom);
  const collisionRadiusModel = seed.nominalRadiusCss.map(radius =>
    requirePositiveFloat32((radius + seed.pairGapCss / 2) / referenceZoom, "Contact collision radius"));
  requirePositiveFloat32(2 * Math.max(...collisionRadiusModel), "Contact pair clearance");
  return Object.freeze({ ...seed, referenceZoom, collisionRadiusModel: Object.freeze(collisionRadiusModel) });
}

/** Admit only same-scene immutable contact metadata alongside its seed record. */
export function isReusableWorkspaceForceAtlas2ContactUnits(
  contact: unknown, seed: WorkspaceForceAtlas2Units,
): contact is WorkspaceForceAtlas2Units {
  if (!isReusableWorkspaceForceAtlas2Units(seed, seed.nodeCount)
    || !isReusableWorkspaceForceAtlas2Units(contact, seed.nodeCount)) return false;
  return contact.referenceZoom <= seed.referenceZoom
    && contact.pairGapCss === seed.pairGapCss
    && contact.rendererUnitsPerModelUnit === seed.rendererUnitsPerModelUnit
    && contact.referenceViewport.width === seed.referenceViewport.width
    && contact.referenceViewport.height === seed.referenceViewport.height
    && contact.origin.every((value, index) => value === seed.origin[index])
    && contact.nominalRadiusCss.every((value, index) => value === seed.nominalRadiusCss[index]);
}

/** Explicit one-time signed affine mapping. No box clamp or per-frame rescale. */
export function workspaceRendererToModelPositions(units: WorkspaceForceAtlas2Units, positions: ArrayLike<number>): Float32Array {
  requirePositionRows(positions, units.nodeCount);
  return Float32Array.from(positions, (value, index) => {
    const packed = Math.fround((value - units.origin[index % 2]) / units.rendererUnitsPerModelUnit);
    if (!Number.isFinite(packed)) throw new Error("Model coordinates overflow Float32");
    return packed;
  });
}

/**
 * Render the same physical frame, even outside the former SDK [0,4096] box.
 * A driver keeps exact renderer pin rows separately (including new drag pins),
 * avoiding affine Float32 round-trip drift. It must also pin the model state.
 */
export function workspaceModelToRendererPositions(
  units: WorkspaceForceAtlas2Units,
  positions: ArrayLike<number>,
  rendererPins: ReadonlyMap<number, readonly [number, number]> = new Map(),
): Float32Array {
  requirePositionRows(positions, units.nodeCount);
  const result = Float32Array.from(positions, (value, index) => {
    const packed = Math.fround(value * units.rendererUnitsPerModelUnit + units.origin[index % 2]);
    if (!Number.isFinite(packed)) throw new Error("Renderer coordinates overflow Float32");
    return packed;
  });
  for (const [index, pin] of rendererPins) {
    if (!Number.isInteger(index) || index < 0 || index >= units.nodeCount || pin.length !== 2) {
      throw new Error("Invalid renderer pin row");
    }
    requirePositionRows(pin, 1);
    result[index * 2] = pin[0]; result[index * 2 + 1] = pin[1];
  }
  return result;
}

import { workspaceBaseBodyDiameterCss, workspaceNominalGlyphRadiusCss } from "../shared/workspaceGlyphGeometry";

export type WorkspaceD3UnitBasis = Readonly<{
  nodeCount: number;
  referenceViewport: Readonly<{ width: number; height: number }>;
  /** Canonical graph units to CSS pixels at the initial, scene-owned Fit. */
  referenceZoom: number;
  nominalRadiusCss: readonly number[];
  radiusModel: readonly number[];
  calibrationPasses: number;
}>;

export function workspaceD3FitZoom(positions: ArrayLike<number>, viewport: { width: number; height: number }): number {
  if (!positions.length || positions.length % 2 || !Number.isFinite(viewport.width)
    || !Number.isFinite(viewport.height) || viewport.width <= 0 || viewport.height <= 0) {
    throw new Error("D3 scene calibration requires finite coordinates and a CSS viewport");
  }
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (let index = 0; index < positions.length; index += 2) {
    const x = positions[index], y = positions[index + 1];
    if (!Number.isFinite(Math.fround(x)) || !Number.isFinite(Math.fround(y))) throw new Error("Invalid D3 calibration coordinate");
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
  }
  // Pinned Cosmos Fit(.08), CSS dimensions, and rescalePositions:false.
  return Math.max(.001, Math.min(viewport.width * .84 / (maxX - minX || 1),
    viewport.height * .84 / (maxY - minY || 1)));
}

export function buildWorkspaceD3UnitBasis(input: {
  positions: ArrayLike<number>;
  diameters: ArrayLike<number>;
  shapes: ArrayLike<number>;
  viewport: { width: number; height: number };
  maxDiameterCss?: number;
  referenceZoom?: number;
  calibrationPasses?: number;
}): WorkspaceD3UnitBasis {
  const nodeCount = input.diameters.length;
  if (!nodeCount || input.positions.length !== nodeCount * 2 || input.shapes.length !== nodeCount) {
    throw new Error("D3 calibration catalog disagrees with glyph rows");
  }
  const fit = workspaceD3FitZoom(input.positions, input.viewport);
  const referenceZoom = input.referenceZoom ?? fit;
  if (!Number.isFinite(referenceZoom) || referenceZoom < .001) throw new Error("Invalid D3 reference zoom");
  const maximum = input.maxDiameterCss ?? Infinity;
  if (!(maximum > 0)) throw new Error("Invalid native point diameter limit");
  const shaderZoom = Math.min(5, Math.max(1, referenceZoom * .01));
  const nominalRadiusCss = Array.from({ length: nodeCount }, (_, index) => workspaceNominalGlyphRadiusCss(
    Math.min(workspaceBaseBodyDiameterCss(input.diameters[index], nodeCount) * shaderZoom, maximum), input.shapes[index]));
  const radiusModel = nominalRadiusCss.map(radius => Math.fround(radius / referenceZoom));
  if (!radiusModel.every(radius => Number.isFinite(radius) && radius > 0)) throw new Error("D3 collision radius overflows model units");
  return Object.freeze({ nodeCount, referenceViewport: Object.freeze({ ...input.viewport }), referenceZoom,
    nominalRadiusCss: Object.freeze(nominalRadiusCss), radiusModel: Object.freeze(radiusModel),
    calibrationPasses: input.calibrationPasses ?? 0 });
}

/** Circumscribed ordinary body discs; cosmetic cluster emphasis is excluded. */
export function measureWorkspaceD3BodyOverlap(positions: ArrayLike<number>, radiiCss: readonly number[], zoom: number,
  materialPenetrationCss = .5, fixedIndices?: ReadonlySet<number>): { overlappingNodes: number; fraction: number; maximumPenetrationCss: number } {
  if (positions.length !== radiiCss.length * 2 || !Number.isFinite(zoom) || zoom <= 0) {
    throw new Error("Invalid projected D3 body rows");
  }
  const cell = Math.max(1, 2 * Math.max(...radiiCss));
  const cells = new Map<string, { free: number[]; fixed: number[] }>(), touching = new Set<number>();
  let maximumPenetrationCss = 0;
  for (let index = 0; index < radiiCss.length; index++) {
    const x = positions[index * 2] * zoom, y = positions[index * 2 + 1] * zoom;
    const cx = Math.floor(x / cell), cy = Math.floor(y / cell);
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
      const bucket = cells.get(`${cx + dx}:${cy + dy}`);
      // Authored fixed bodies may overlap each other. They cannot be moved by
      // any solver, so only free/free and free/fixed contacts block readiness.
      const candidates = !bucket ? [] : fixedIndices?.has(index) ? bucket.free : [...bucket.free, ...bucket.fixed];
      for (const other of candidates) {
        const penetration = radiiCss[index] + radiiCss[other] - Math.hypot(x - positions[other * 2] * zoom,
          y - positions[other * 2 + 1] * zoom);
        maximumPenetrationCss = Math.max(maximumPenetrationCss, penetration);
        if (penetration > materialPenetrationCss) { touching.add(index); touching.add(other); }
      }
    }
    const key = `${cx}:${cy}`;
    let bucket = cells.get(key);
    if (!bucket) { bucket = { free: [], fixed: [] }; cells.set(key, bucket); }
    (fixedIndices?.has(index) ? bucket.fixed : bucket.free).push(index);
  }
  return { overlappingNodes: touching.size, fraction: touching.size / radiiCss.length, maximumPenetrationCss };
}

export function isReusableWorkspaceD3UnitBasis(value: unknown, nodeCount: number): value is WorkspaceD3UnitBasis {
  if (!value || typeof value !== "object") return false;
  const basis = value as WorkspaceD3UnitBasis;
  return basis.nodeCount === nodeCount && Number.isFinite(basis.referenceZoom) && basis.referenceZoom >= .001
    && !!basis.referenceViewport && [basis.referenceViewport.width, basis.referenceViewport.height]
      .every(size => Number.isFinite(size) && size > 0)
    && Array.isArray(basis.nominalRadiusCss) && Array.isArray(basis.radiusModel)
    && basis.nominalRadiusCss.length === nodeCount && basis.radiusModel.length === nodeCount
    && basis.nominalRadiusCss.every(radius => Number.isFinite(radius) && radius > 0)
    && basis.radiusModel.every((radius, index) => Number.isFinite(radius) && radius > 0
      && radius === Math.fround(basis.nominalRadiusCss[index] / basis.referenceZoom))
    && Number.isInteger(basis.calibrationPasses) && basis.calibrationPasses >= 0;
}

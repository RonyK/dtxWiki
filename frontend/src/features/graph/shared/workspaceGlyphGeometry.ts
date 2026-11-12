/** Native glyphs the Map draws; the ring (9) is a disc with a background-coloured inside. */
export const WORKSPACE_NATIVE_GLYPH_SHAPES: readonly number[] = [0, 1, 3, 9];

/** Native Circle/Square/Diamond/Ring geometry shared by CPU and GPU collision. */
export function workspaceNominalGlyphRadiusCss(diameter: number, shape: number): number {
  if (!Number.isFinite(Math.fround(diameter)) || Math.fround(diameter) <= 0) {
    throw new Error("Native CSS diameter must be positive finite Float32");
  }
  if (!WORKSPACE_NATIVE_GLYPH_SHAPES.includes(shape)) throw new Error("Unknown native glyph shape");
  // Native diamond SDF: abs(x) + abs(y) / 1.2 <= .8 in half-diameter units.
  return diameter * (shape === 1 ? .4 * Math.SQRT2 : shape === 3 ? .48 : .5);
}

/** The visible base body floor, independent of cluster document-count emphasis. */
export function workspaceBaseBodyDiameterCss(diameter: number, nodeCount: number): number {
  return Math.max(diameter, nodeCount > 1_000 ? 3 : 5);
}

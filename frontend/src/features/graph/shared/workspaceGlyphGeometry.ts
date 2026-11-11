/** Native Circle/Square/Diamond geometry shared by CPU and GPU collision. */
export function workspaceNominalGlyphRadiusCss(diameter: number, shape: number): number {
  if (!Number.isFinite(Math.fround(diameter)) || Math.fround(diameter) <= 0) {
    throw new Error("Native CSS diameter must be positive finite Float32");
  }
  if (shape !== 0 && shape !== 1 && shape !== 3) throw new Error("Unknown native glyph shape");
  // Native diamond SDF: abs(x) + abs(y) / 1.2 <= .8 in half-diameter units.
  return diameter * (shape === 0 ? .5 : shape === 3 ? .48 : .4 * Math.SQRT2);
}

/** The visible base body floor, independent of cluster document-count emphasis. */
export function workspaceBaseBodyDiameterCss(diameter: number, nodeCount: number): number {
  return Math.max(diameter, nodeCount > 1_000 ? 3 : 5);
}

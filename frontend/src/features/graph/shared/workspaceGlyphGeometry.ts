import type { GraphNodeShape } from "./graphTokens";
import { shapeOutline, TRIANGLE_RADIUS_FACTOR, type ShapeOutline } from "./nodeShapes";

/** Native shape IDs: the ring (9) is a disc with a background-coloured inside. */
const NATIVE_GLYPH_SHAPE = {
  circle: 0, square: 1, triangle: 2, diamond: 3, ring: 9, countDisc: 0,
} as const satisfies Record<GraphNodeShape, number>;

export const WORKSPACE_NATIVE_GLYPH_SHAPES: readonly number[] = [...new Set(Object.values(NATIVE_GLYPH_SHAPE))];

/** Same shape vocabulary as the legends and Context canvas, without surface-specific enums. */
export function nativeGlyphShape(shape: GraphNodeShape): number {
  return NATIVE_GLYPH_SHAPE[shape];
}

/** A triangle keeps the existing legend's enlarged circumradius; other native bodies retain their size. */
export function nativeGlyphDiameterScale(shape: GraphNodeShape): number {
  return shape === "triangle" ? TRIANGLE_RADIUS_FACTOR : 1;
}

const SQUARE_HALF_EXTENT = .4;
const DIAMOND_ASPECT = 1.2;
const DIAMOND_HALF_EXTENT = SQUARE_HALF_EXTENT * DIAMOND_ASPECT;

/** The native painted body, in CSS pixels from its centre; y grows down the screen. */
export function nativeGlyphContainsPoint(shape: number, diameter: number, dx: number, dy: number): boolean {
  if (shape === NATIVE_GLYPH_SHAPE.square) {
    return Math.abs(dx) <= diameter * SQUARE_HALF_EXTENT && Math.abs(dy) <= diameter * SQUARE_HALF_EXTENT;
  }
  if (shape === NATIVE_GLYPH_SHAPE.triangle) {
    return dy >= -diameter * .5 && dy <= diameter * .25
      && Math.abs(dx) <= (dy + diameter * .5) / Math.sqrt(3);
  }
  if (shape === NATIVE_GLYPH_SHAPE.diamond) {
    return Math.abs(dx) + Math.abs(dy) / DIAMOND_ASPECT <= diameter * SQUARE_HALF_EXTENT;
  }
  return dx * dx + dy * dy <= diameter * diameter * .25;
}

/** Shader counterpart of the CPU body test, sharing the reviewed shape constants. */
export const NATIVE_GLYPH_HIT_GLSL = `
bool nativeGlyphContainsPoint(float shape, float diameter, vec2 delta) {
  if (shape == ${NATIVE_GLYPH_SHAPE.square}.0) return all(lessThanEqual(abs(delta), vec2(diameter * ${SQUARE_HALF_EXTENT})));
  if (shape == ${NATIVE_GLYPH_SHAPE.triangle}.0) return delta.y >= -diameter * 0.5 && delta.y <= diameter * 0.25
    && abs(delta.x) <= (delta.y + diameter * 0.5) / sqrt(3.0);
  if (shape == ${NATIVE_GLYPH_SHAPE.diamond}.0) return abs(delta.x) + abs(delta.y) / ${DIAMOND_ASPECT} <= diameter * ${SQUARE_HALF_EXTENT};
  return dot(delta, delta) <= diameter * diameter * 0.25;
}`;

/** An outward parallel outline of the actual native body, for selection and forced-colour cues. */
export function nativeGlyphOutline(shape: number, cx: number, cy: number, diameter: number, offset = 0): ShapeOutline {
  if (shape === NATIVE_GLYPH_SHAPE.square) return shapeOutline("square", cx, cy, diameter * SQUARE_HALF_EXTENT, offset);
  if (shape === NATIVE_GLYPH_SHAPE.triangle) {
    return shapeOutline("triangle", cx, cy, diameter / (2 * TRIANGLE_RADIUS_FACTOR), offset);
  }
  if (shape === NATIVE_GLYPH_SHAPE.diamond) {
    // x/a + y/b = 1; offset grows the constant by the length of its normal.
    const normal = Math.hypot(1 / SQUARE_HALF_EXTENT, 1 / DIAMOND_HALF_EXTENT);
    const x = SQUARE_HALF_EXTENT * (diameter + offset * normal);
    const y = DIAMOND_HALF_EXTENT * (diameter + offset * normal);
    return { kind: "polygon", points: [[cx, cy - y], [cx + x, cy], [cx, cy + y], [cx - x, cy]] };
  }
  return shapeOutline("circle", cx, cy, diameter / 2, offset);
}

/** Scale a native body about its centre, including the triangle's enlarged nominal size. */
export function nativeGlyphInsetOutline(shape: number, cx: number, cy: number, diameter: number, factor: number): ShapeOutline {
  return nativeGlyphOutline(shape, cx, cy, diameter * factor);
}

/** Actual native glyph envelope shared by CPU and GPU collision. Diameter already includes the visual shape scale. */
export function workspaceNominalGlyphRadiusCss(diameter: number, shape: number): number {
  if (!Number.isFinite(Math.fround(diameter)) || Math.fround(diameter) <= 0) {
    throw new Error("Native CSS diameter must be positive finite Float32");
  }
  if (!WORKSPACE_NATIVE_GLYPH_SHAPES.includes(shape)) throw new Error("Unknown native glyph shape");
  // Native diamond SDF: abs(x) + abs(y) / 1.2 <= .8 in half-diameter units.
  return diameter * (shape === NATIVE_GLYPH_SHAPE.square ? SQUARE_HALF_EXTENT * Math.SQRT2
    : shape === NATIVE_GLYPH_SHAPE.diamond ? DIAMOND_HALF_EXTENT : .5);
}

/** The visible base body floor, independent of cluster document-count emphasis. */
export function workspaceBaseBodyDiameterCss(diameter: number, nodeCount: number): number {
  return Math.max(diameter, nodeCount > 1_000 ? 3 : 5);
}

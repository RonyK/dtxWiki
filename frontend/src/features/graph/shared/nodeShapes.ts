import type { GraphNodeShape } from "./graphTokens";

/**
 * Node geometry shared by the canvases, their rings and the SVG legend cues.
 * `r` is the rendered node radius (Sigma's scaled size). A square circumscribes
 * that disc (Sigma's NodeSquareProgram), a diamond is the same square turned,
 * and a triangle's circumradius is enlarged so its area does not read smaller.
 */
export const TRIANGLE_RADIUS_FACTOR = 1.35;
/** Folder/grouping ring: band width in CSS pixels at every size and zoom. */
export const GRAPH_RING_BAND_PX = 2;
/** Forced-colours Agent outline: [inner gap radius, inner fill radius] as fractions of the outer extent. */
export const FORCED_OUTLINE_INSETS = {
  circle: [0.8, 0.55],
  triangle: [0.72, 0.42],
} as const;
/** Emphasis/selection ring: surface gap and band, in CSS pixels outside the shape. */
export const GRAPH_RING_GAP_PX = 2;
export const GRAPH_RING_WIDTH_PX = 2;

export type ShapeOutline =
  | { kind: "circle"; cx: number; cy: number; r: number }
  | { kind: "polygon"; points: Array<[number, number]> };

function regularPolygon(cx: number, cy: number, radius: number, sides: number, rotation: number) {
  return Array.from({ length: sides }, (_, index): [number, number] => {
    const angle = rotation + (2 * Math.PI * index) / sides;
    return [cx + radius * Math.cos(angle), cy + radius * Math.sin(angle)];
  });
}

/**
 * The outline of `shape` at node radius `r`, pushed outward by `offset`
 * pixels along its normals (a polygon's apothem grows by exactly `offset`).
 * Screen coordinates: y grows downward, so a triangle points up.
 */
export function shapeOutline(shape: GraphNodeShape, cx: number, cy: number, r: number, offset = 0): ShapeOutline {
  switch (shape) {
    case "square":
      return { kind: "polygon", points: regularPolygon(cx, cy, Math.SQRT2 * (r + offset), 4, Math.PI / 4) };
    case "diamond":
      return { kind: "polygon", points: regularPolygon(cx, cy, Math.SQRT2 * (r + offset), 4, -Math.PI / 2) };
    case "triangle":
      // Apothem = R / 2, so an outward offset d adds 2d to the circumradius.
      return { kind: "polygon", points: regularPolygon(cx, cy, TRIANGLE_RADIUS_FACTOR * r + 2 * offset, 3, -Math.PI / 2) };
    default:
      return { kind: "circle", cx, cy, r: r + offset };
  }
}

/** Scale a shape about its centre, for the forced-colours inner outline. */
export function insetOutline(shape: GraphNodeShape, cx: number, cy: number, r: number, factor: number): ShapeOutline {
  return shape === "triangle"
    ? { kind: "polygon", points: regularPolygon(cx, cy, TRIANGLE_RADIUS_FACTOR * r * factor, 3, -Math.PI / 2) }
    : shapeOutline(shape, cx, cy, r * factor);
}

const round = (value: number) => Math.round(value * 100) / 100;

export function outlinePathData(outline: ShapeOutline): string {
  if (outline.kind === "circle") {
    const { cx, cy, r } = outline;
    return `M${round(cx - r)} ${round(cy)}a${round(r)} ${round(r)} 0 1 0 ${round(2 * r)} 0a${round(r)} ${round(r)} 0 1 0 ${round(-2 * r)} 0Z`;
  }
  return `M${outline.points.map(([x, y]) => `${round(x)} ${round(y)}`).join("L")}Z`;
}

export function traceOutline(context: CanvasRenderingContext2D, outline: ShapeOutline) {
  context.beginPath();
  if (outline.kind === "circle") {
    context.arc(outline.cx, outline.cy, outline.r, 0, 2 * Math.PI);
  } else {
    outline.points.forEach(([x, y], index) => index ? context.lineTo(x, y) : context.moveTo(x, y));
  }
  context.closePath();
}

/**
 * Draw an emphasis ring that follows the node's own shape: a surface-coloured
 * gap, then the ring band, both outside the glyph so the glyph is untouched.
 */
export function drawShapeRing(context: CanvasRenderingContext2D, shape: GraphNodeShape,
  cx: number, cy: number, r: number, color: string, surface: string, extraOffset = 0) {
  context.save();
  context.lineJoin = "miter";
  context.lineWidth = GRAPH_RING_GAP_PX;
  context.strokeStyle = surface;
  traceOutline(context, shapeOutline(shape, cx, cy, r, extraOffset + GRAPH_RING_GAP_PX / 2));
  context.stroke();
  context.lineWidth = GRAPH_RING_WIDTH_PX;
  context.strokeStyle = color;
  traceOutline(context, shapeOutline(shape, cx, cy, r, extraOffset + GRAPH_RING_GAP_PX + GRAPH_RING_WIDTH_PX / 2));
  context.stroke();
  context.restore();
}

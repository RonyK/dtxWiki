import { rgb } from "./cosmosColor";

/**
 * Point images for the native graph's `pointImageSdf` mode: RGB is the ink and
 * alpha a signed distance field (0.5 on the edge), so the shader keeps text and
 * outlines crisp at every drawn size, zoom and pixel ratio.
 */

/** A cluster's count: weight-600 10 px text, at least an 18 px disc, 2 px padding. */
export const WORKSPACE_CLUSTER_COUNT = { fontWeight: 600, fontSizeCss: 10, minDiameterCss: 18, paddingCss: 2 } as const;
/** Image pixels per CSS pixel of the base disc; the distance field upsamples cleanly. */
const IMAGE_SCALE = 2;
/** Mask supersampling for the distance transform. */
const MASK_SCALE = 3;
/** Distance (image px) between alpha 0.5 and 0 or 1. */
const SPREAD = 3;
/** Fallback advance widths (CSS px at 600 10px) where no canvas can measure. */
const ESTIMATED_DIGIT_CSS = 7;
const ESTIMATED_SEPARATOR_CSS = 3;

export type WorkspaceCountMeasure = (text: string) => number;

/** The document count as the reader's locale writes it, e.g. "12,345". */
export function workspaceClusterCountText(count: number, locale?: string): string {
  return new Intl.NumberFormat(locale).format(Math.max(0, Math.round(count)));
}

/** Width estimate without a canvas (tests, workers): digits and separators. */
export const estimateWorkspaceCountWidthCss: WorkspaceCountMeasure = (text) => {
  let width = 0;
  for (const character of text) width += /\d/u.test(character) ? ESTIMATED_DIGIT_CSS : ESTIMATED_SEPARATOR_CSS;
  return width;
};

/** The base disc that holds the count: never below 18 px, always 2 px clear on each side. */
export function workspaceClusterDiameterCss(text: string, measure: WorkspaceCountMeasure = estimateWorkspaceCountWidthCss): number {
  const { minDiameterCss, paddingCss } = WORKSPACE_CLUSTER_COUNT;
  return Math.max(minDiameterCss, Math.ceil(measure(text)) + 2 * paddingCss);
}

function countFont(fontFamily: string, scale: number): string {
  const { fontWeight, fontSizeCss } = WORKSPACE_CLUSTER_COUNT;
  return `${fontWeight} ${fontSizeCss * scale}px ${fontFamily}`;
}

function canvas2d(width: number, height: number): CanvasRenderingContext2D | null {
  if (typeof document === "undefined") return null;
  const canvas = document.createElement("canvas");
  canvas.width = width; canvas.height = height;
  try {
    return canvas.getContext("2d", { willReadFrequently: true });
  } catch {
    return null;
  }
}

/** Measures with the real font where a canvas exists, caching each text. */
export function createWorkspaceCountMeasure(fontFamily: string): WorkspaceCountMeasure {
  const context = canvas2d(1, 1);
  if (!context || typeof context.measureText !== "function") return estimateWorkspaceCountWidthCss;
  context.font = countFont(fontFamily, 1);
  const widths = new Map<string, number>();
  return (text) => {
    let width = widths.get(text);
    if (width === undefined) {
      const metrics = context.measureText(text);
      width = Number.isFinite(metrics.width) && metrics.width > 0 ? metrics.width : estimateWorkspaceCountWidthCss(text);
      widths.set(text, width);
    }
    return width;
  };
}

/** Exact 1D squared Euclidean distance transform (Felzenszwalb & Huttenlocher). */
function distanceTransform1d(f: Float64Array, n: number, d: Float64Array, v: Int32Array, z: Float64Array) {
  let k = 0;
  v[0] = 0; z[0] = -Infinity; z[1] = Infinity;
  for (let q = 1; q < n; q++) {
    let s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
    while (s <= z[k]) { k--; s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]); }
    k++; v[k] = q; z[k] = s; z[k + 1] = Infinity;
  }
  k = 0;
  for (let q = 0; q < n; q++) {
    while (z[k + 1] < q) k++;
    d[q] = (q - v[k]) * (q - v[k]) + f[v[k]];
  }
}

/** Squared distance from every cell to the nearest cell where `target` holds. */
function squaredDistanceTo(target: Uint8Array, size: number): Float64Array {
  const grid = new Float64Array(size * size);
  for (let index = 0; index < grid.length; index++) grid[index] = target[index] ? 0 : 1e20;
  const f = new Float64Array(size), d = new Float64Array(size), v = new Int32Array(size), z = new Float64Array(size + 1);
  for (let x = 0; x < size; x++) {
    for (let y = 0; y < size; y++) f[y] = grid[y * size + x];
    distanceTransform1d(f, size, d, v, z);
    for (let y = 0; y < size; y++) grid[y * size + x] = d[y];
  }
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) f[x] = grid[y * size + x];
    distanceTransform1d(f, size, d, v, z);
    for (let x = 0; x < size; x++) grid[y * size + x] = d[x];
  }
  return grid;
}

function sdfImage(ink: string, size: number, signedDistance: (x: number, y: number) => number): ImageData | null {
  if (typeof ImageData === "undefined") return null;
  const [red, green, blue] = rgb(ink).map((value) => Math.round(value * 255));
  const data = new Uint8ClampedArray(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const offset = (y * size + x) * 4;
      data[offset] = red; data[offset + 1] = green; data[offset + 2] = blue;
      data[offset + 3] = Math.round(Math.min(1, Math.max(0, .5 + signedDistance(x + .5, y + .5) / (2 * SPREAD))) * 255);
    }
  }
  return new ImageData(data, size, size);
}

/** One glyph's signed distance (image px) on a grid around its pen, at the baseline. */
type GlyphField = { width: number; height: number; penX: number; baselineY: number; distance: Float32Array };

/** Each character's field, computed once per face: a count is composed from them. */
const glyphFields = new Map<string, GlyphField | null>();

function glyphField(character: string, fontFamily: string): GlyphField | null {
  const key = `${fontFamily}\u0000${character}`;
  if (glyphFields.has(key)) return glyphFields.get(key)!;
  const probe = canvas2d(1, 1);
  let field: GlyphField | null = null;
  if (probe && typeof probe.measureText === "function") {
    probe.font = countFont(fontFamily, IMAGE_SCALE * MASK_SCALE);
    const metrics = probe.measureText(character);
    const ascent = Math.ceil(metrics.actualBoundingBoxAscent ?? 0), descent = Math.ceil(metrics.actualBoundingBoxDescent ?? 0);
    const left = Math.ceil(metrics.actualBoundingBoxLeft ?? 0), right = Math.ceil(metrics.actualBoundingBoxRight ?? metrics.width);
    const pad = (SPREAD + 1) * MASK_SCALE;
    const maskWidth = left + right + 2 * pad, maskHeight = ascent + descent + 2 * pad;
    const context = canvas2d(maskWidth, maskHeight);
    if (context && typeof context.getImageData === "function") {
      context.font = probe.font;
      context.textAlign = "left";
      context.textBaseline = "alphabetic";
      context.fillStyle = "#000";
      context.fillText(character, pad + left, pad + ascent);
      const alpha = context.getImageData(0, 0, maskWidth, maskHeight).data;
      const side = Math.max(maskWidth, maskHeight);
      const inside = new Uint8Array(side * side), outside = new Uint8Array(side * side).fill(1);
      for (let y = 0; y < maskHeight; y++) for (let x = 0; x < maskWidth; x++) {
        const covered = alpha[(y * maskWidth + x) * 4 + 3] >= 128 ? 1 : 0;
        inside[y * side + x] = covered; outside[y * side + x] = 1 - covered;
      }
      const toInside = squaredDistanceTo(inside, side), toOutside = squaredDistanceTo(outside, side);
      const width = Math.ceil(maskWidth / MASK_SCALE), height = Math.ceil(maskHeight / MASK_SCALE);
      const distance = new Float32Array(width * height);
      for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
        const index = Math.min(side - 1, Math.floor((y + .5) * MASK_SCALE)) * side + Math.min(side - 1, Math.floor((x + .5) * MASK_SCALE));
        distance[y * width + x] = (inside[index] ? Math.sqrt(toOutside[index]) : -Math.sqrt(toInside[index])) / MASK_SCALE;
      }
      field = { width, height, penX: (pad + left) / MASK_SCALE, baselineY: (pad + ascent) / MASK_SCALE, distance };
    }
  }
  glyphFields.set(key, field);
  return field;
}

/**
 * The count drawn into a disc of `diameterCss`: its glyph box (not the em box
 * or a middle baseline) is centred, so digits without descenders sit optically
 * in the middle. Composed from per-character fields, the union of glyphs.
 */
export function workspaceCountImage(text: string, diameterCss: number, ink: string, fontFamily: string): ImageData | null {
  const size = Math.max(1, Math.ceil(diameterCss * IMAGE_SCALE));
  const context = canvas2d(1, 1);
  if (!context || typeof context.measureText !== "function") return null;
  context.font = countFont(fontFamily, IMAGE_SCALE);
  const metrics = context.measureText(text);
  const left = metrics.actualBoundingBoxLeft ?? 0, right = metrics.actualBoundingBoxRight ?? metrics.width;
  const ascent = metrics.actualBoundingBoxAscent ?? 0, descent = metrics.actualBoundingBoxDescent ?? 0;
  const startX = size / 2 - (right - left) / 2, baseline = size / 2 + (ascent - descent) / 2;
  const union = new Float32Array(size * size).fill(-SPREAD);
  const characters = [...text];
  let prefix = "";
  for (const character of characters) {
    const field = glyphField(character, fontFamily);
    if (!field) return null;
    const originX = Math.round(startX + context.measureText(prefix).width - field.penX);
    const originY = Math.round(baseline - field.baselineY);
    for (let y = 0; y < field.height; y++) {
      const row = originY + y;
      if (row < 0 || row >= size) continue;
      for (let x = 0; x < field.width; x++) {
        const column = originX + x;
        if (column < 0 || column >= size) continue;
        const index = row * size + column;
        union[index] = Math.max(union[index], field.distance[y * field.width + x]);
      }
    }
    prefix += character;
  }
  return sdfImage(ink, size, (x, y) => union[Math.floor(y) * size + Math.floor(x)]);
}

/** An annulus between two radii (fractions of the half-size), as a distance field. */
export function workspaceAnnulusImage(ink: string, innerFactor: number, outerFactor: number, size = 40): ImageData | null {
  const half = size / 2, inner = half * innerFactor, outer = half * outerFactor;
  return sdfImage(ink, size, (x, y) => {
    const radius = Math.hypot(x - half, y - half);
    return Math.min(radius - inner, outer - radius);
  });
}

/** One SDF image per distinct (text, diameter), kept across scene and image-list rebuilds. */
export function createWorkspaceCountImageCache(ink: string, fontFamily: string) {
  const images = new Map<string, ImageData | null>();
  return (text: string, diameterCss: number): ImageData | null => {
    const key = `${text}\u0000${diameterCss}`;
    if (!images.has(key)) images.set(key, workspaceCountImage(text, diameterCss, ink, fontFamily));
    return images.get(key) ?? null;
  };
}

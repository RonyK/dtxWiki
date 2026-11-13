import assert from "node:assert/strict";
import { COSMOS_COLLISION_RADII_PATCH_HUNKS } from "./cosmos-collision-radii-patch.mjs";
import { COSMOS_POINT_PICKER_PATCH_HUNKS } from "./cosmos-point-picker-patch.mjs";
import { COSMOS_POINT_RING_PATCH_HUNKS } from "./cosmos-point-ring-patch.mjs";
import { COSMOS_POINT_IMAGE_SDF_PATCH_HUNKS } from "./cosmos-point-image-sdf-patch.mjs";
import { COSMOS_SOURCE_SHA256, COSMOS_VERSION, sha256 } from "./cosmos-source.mjs";
import {
  GRAPH_EDGE_ENDPOINTS, GRAPH_EDGE_ENDPOINT_SIZE_PX, GRAPH_EDGE_PATTERN, GRAPH_EDGE_TRIANGLE_SIZE_PX,
  GRAPH_LINE_OPACITY, GRAPH_LINE_STYLE_ROWS, GRAPH_REMOVED_EDGE_PATTERN,
} from "../frontend/src/features/graph/shared/graphTokens.ts";

// One line table draws every graph line on both screens. A line's style value
// selects its row (past the table when emphasized); each row is a stroke
// pattern plus source and target markers. The table, marker sizes, node gap,
// marker shaft and emphasis are generated from graphTokens.ts, so no value is
// copied here. Markers reuse the point footprint texture, so their tips follow
// current GPU positions without per-line readback or DOM.
// The table replaces upstream's stroke-style branch and arrowhead. Upstream's
// arrow and dash code and config fields stay in place, unused by dtxWiki, so
// this patch remains a small, reviewed delta.
const glsl = value => Number.isInteger(value) ? `${value}.0` : String(value);
const vec = values => `vec${values.length}(${values.map(glsl).join(", ")})`;
const SOURCE_MARKER = { none: 0, "open-diamond": 1 };
const TARGET_MARKER = { none: 0, "open-arrow": 1, "filled-triangle": 2 };
const rows = GRAPH_LINE_STYLE_ROWS.map(row => {
  const pattern = row === "removed" ? GRAPH_REMOVED_EDGE_PATTERN : GRAPH_EDGE_PATTERN[row] ?? [];
  const endpoints = row === "removed" ? { source: "none", target: "none" } : GRAPH_EDGE_ENDPOINTS[row];
  assert.ok(pattern.length <= 4 && endpoints.source in SOURCE_MARKER && endpoints.target in TARGET_MARKER, `Line row ${row}`);
  return { pattern: [...pattern, 0, 0, 0, 0].slice(0, 4),
    markers: [SOURCE_MARKER[endpoints.source], TARGET_MARKER[endpoints.target]] };
});
const size = GRAPH_EDGE_ENDPOINT_SIZE_PX, triangle = GRAPH_EDGE_TRIANGLE_SIZE_PX;
const lineTable = `
// dtxWiki line table, generated from graphTokens.ts (GRAPH_LINE_STYLE_ROWS order).
const int LINE_STYLE_ROWS = ${rows.length};
// Screen-px pattern: dash, gap, dot, dot gap; all zero is a solid stroke.
const vec4 LINE_PATTERNS[${rows.length}] = vec4[${rows.length}](${rows.map(row => vec(row.pattern)).join(", ")});
// Markers: source (0 none, 1 open diamond), target (0 none, 1 open arrow, 2 filled triangle).
const vec2 LINE_MARKERS[${rows.length}] = vec2[${rows.length}](${rows.map(row => vec(row.markers)).join(", ")});
const vec2 LINE_ARROW = ${vec([size.arrowLength, size.arrowHalfWidth])};
const vec2 LINE_DIAMOND = ${vec([size.diamondLength, size.diamondHalfWidth])};
const vec2 LINE_TRIANGLE = ${vec([triangle.length, triangle.halfWidth])};
const float LINE_NODE_GAP = ${glsl(size.nodeGap)};
const float LINE_EMPHASIS = ${glsl(GRAPH_LINE_OPACITY.emphasis)};
const float LINE_MARKER_SHAFT = ${glsl(size.markerShaft)};

int lineStyleRow(float style) { return int(mod(style, float(LINE_STYLE_ROWS))); }

float endpointNodeRadius(vec4 footprint, vec2 direction) {
  float zoom = endpointScalePointsOnZoom > 0.5 ? transformationMatrix[0][0]
    : min(5.0, max(1.0, transformationMatrix[0][0] * 0.01));
  float diameter = min(footprint.r * endpointPointSizeScale * zoom, maxPointSize);
  float radius = max(diameter, 1.0 / pixelRatio) * 0.5;
  // Native glyph boundaries: square (1), upward triangle (2), diamond (3);
  // every other shape, including rings and count discs, is a circle.
  if (footprint.g == 1.0) radius *= 0.8 / max(max(abs(direction.x), abs(direction.y)), 0.0001);
  if (footprint.g == 3.0) radius = diameter * 0.4 / max(abs(direction.x) + abs(direction.y) / 1.2, 0.0001);
  if (footprint.g == 2.0) {
    direction.y = -direction.y;
    float vertical = direction.y < 0.0 ? diameter * 0.5 / max(-direction.y, 0.0001)
      : diameter * 0.25 / max(direction.y, 0.0001);
    float diagonal = sqrt(3.0) * abs(direction.x) - direction.y;
    radius = min(vertical, diagonal > 0.0 ? diameter * 0.5 / diagonal : vertical);
  }
  return radius + LINE_NODE_GAP;
}
`;
const fragmentFunctions = `
float endpointSegmentDistance(vec2 p, vec2 a, vec2 b) {
  vec2 ab = b - a;
  float t = clamp(dot(p - a, ab) / max(dot(ab, ab), 0.0001), 0.0, 1.0);
  return length(p - a - t * ab);
}
float endpointInk(float distance, float halfWidth) {
  float aa = max(fwidth(distance), 0.0001) * 0.5;
  return 1.0 - smoothstep(halfWidth - aa, halfWidth + aa, distance);
}
// A row's dash, then an optional dot, repeating along the line.
float linePatternMask(float phase, vec4 pattern) {
  float period = max(pattern.x + pattern.y + pattern.z + pattern.w, 0.001);
  // Half a ramp in the pattern's units: fwidth(phase) is the change in phase per device pixel.
  float aa = max(fwidth(phase), 1e-4) * (EDGE_RAMP_PX * 0.5);
  float mask = strokeMask(phase, pattern.x, period, aa);
  if (pattern.z > 0.0) mask = max(mask, strokeMask(phase - pattern.x - pattern.y, pattern.z, period, aa));
  return mask;
}
`;
// Every derivative is taken before a per-fragment branch; branches on flat
// varyings or uniforms are uniform across a primitive.
const lineFragment = `  // dtxWiki: the line runs between its two node boundaries. The visible pass
  // inks the shaft, masks it with the row's pattern and draws the row's
  // markers; the picking pass keeps the whole corridor, gaps included.
  vec2 point = vec2(pos.x * vEndpointAxes.x, pos.y * vEndpointAxes.y);
  float start = vEndpointAxes.z;
  float end = vEndpointAxes.x - vEndpointAxes.w;
  if (renderMode < 0.5) {
    float halfStroke = vEndpointStroke.x * 0.5;
    float markerAa = max(max(fwidth(point.x), fwidth(point.y)), 0.0001) * 0.5;
    // A filled head narrows to its tip; the shaft ends at its base.
    float shaftEnd = end - (vEndpointStroke.y > 1.5 ? vEndpointSpec.x : 0.0);
    coverage = endpointInk(abs(point.y), halfStroke);
    if (vLinePattern.x > 0.0) coverage *= linePatternMask(clamp(pos.x, 0.0, 1.0) * vLinkDashSpan, vLinePattern);
    if (point.x < start + vEndpointSpec.z || point.x > shaftEnd) coverage = 0.0;
    if (vEndpointStroke.y > 1.5) {
      float along = end - point.x;
      float halfHeight = vEndpointSpec.y * along / max(vEndpointSpec.x, 0.0001);
      coverage = max(coverage, endpointInk(max(abs(point.y) - halfHeight, max(-along, along - vEndpointSpec.x)), 0.0));
    }
    // Only endpoint neighbourhoods pay for segment distances.
    float distance = 1000000.0;
    if (vEndpointStroke.y > 0.5 && vEndpointStroke.y < 1.5 && point.x >= end - vEndpointSpec.x - vEndpointStroke.x) {
      vec2 target = vec2(end, 0.0);
      distance = min(endpointSegmentDistance(point, target - vec2(vEndpointSpec.x, vEndpointSpec.y), target),
        endpointSegmentDistance(point, target - vec2(vEndpointSpec.x, -vEndpointSpec.y), target));
    }
    if (vEndpointSpec.z > 0.0 && point.x <= start + vEndpointSpec.z + vEndpointStroke.x) {
      vec2 left = vec2(start, 0.0), right = vec2(start + vEndpointSpec.z, 0.0);
      vec2 top = vec2(start + vEndpointSpec.z * 0.5, vEndpointSpec.w);
      vec2 bottom = vec2(start + vEndpointSpec.z * 0.5, -vEndpointSpec.w);
      distance = min(distance, min(min(endpointSegmentDistance(point, left, top), endpointSegmentDistance(point, top, right)),
        min(endpointSegmentDistance(point, right, bottom), endpointSegmentDistance(point, bottom, left))));
    }
    coverage = max(coverage, 1.0 - smoothstep(halfStroke - markerAa, halfStroke + markerAa, distance));
  }
  if (point.x < start || point.x > end || end <= start) coverage = 0.0;
`;
const lineVertex = `  // dtxWiki: every line clips at both node boundaries and draws its row's markers.
  if (linkDistPx <= 0.0001) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
  vec2 lineMarkers = LINE_MARKERS[lineStyleRow(linkStyle)];
  vec2 lineDirection = xBasis / linkDist;
  float sourceClip = endpointNodeRadius(texelFetch(endpointFootprints, pointTexelA, 0), lineDirection);
  float targetClip = endpointNodeRadius(texelFetch(endpointFootprints, pointTexelB, 0), -lineDirection);
  vec2 targetMarker = lineMarkers.y > 1.5 ? LINE_TRIANGLE : lineMarkers.y > 0.5 ? LINE_ARROW : vec2(0.0);
  float sourceMarkerLength = lineMarkers.x > 0.5 ? LINE_DIAMOND.x : 0.0;
  float markerScale = min(1.0, max(linkDistPx - sourceClip - targetClip, 0.0)
    / (sourceMarkerLength + targetMarker.x + LINE_MARKER_SHAFT));
  vEndpointSpec = vec4(targetMarker, sourceMarkerLength, LINE_DIAMOND.y) * markerScale;
  vEndpointStroke = vec2(max((linkWidthPx - smoothingPx) * transformationMatrix[0][0], 0.1), lineMarkers.y);
  // Every quad has room for the widest marker, so every line picks alike.
  float markerWidth = 2.0 * max(LINE_ARROW.y, LINE_DIAMOND.y) + vEndpointStroke.x;
  linkWidthPx = max(linkWidthPx, markerWidth / transformationMatrix[0][0] + smoothingPx);
  vEndpointAxes = vec4(linkDistPx, linkWidthPx * transformationMatrix[0][0], sourceClip, targetClip);

  // Quad width (stroke + ramp) in vLinkDashSpan's pattern-space units; the fragment takes the ramp back off.
`;
// Node footprints for the markers come from these existing point settings.
const fields = ["endpointPointSizeScale", "endpointScalePointsOnZoom"];
const values = config => [`${config}.pointSizeScale`, `${config}.scalePointsOnZoom ? 1 : 0`];
const uniformLines = (indent, config) => fields.map((field, index) => `${indent}${field}: ${values(config)[index]},\n`).join("");
const varying = direction => `flat ${direction} vec4 vEndpointAxes;\nflat ${direction} vec4 vEndpointSpec;\nflat ${direction} vec2 vEndpointStroke;\nflat ${direction} vec4 vLinePattern;\n`;

export const COSMOS_LINK_ENDPOINT_PATCH_HUNKS = Object.freeze([
  { id: "line-style-sanitizer", before: "(!Number.isInteger(s) || s < 0 || s > 2) && (n[o] = i);",
    after: `(!Number.isInteger(s) || s < 0 || s >= ${2 * rows.length}) && (n[o] = i);` },
  { id: "endpoint-varyings-fragment", before: "flat in vec4 vEndpointColorB;\n", after: `flat in vec4 vEndpointColorB;\n${varying("in")}` },
  { id: "endpoint-varyings-vertex", before: "flat out vec4 vEndpointColorB;\n", after: `flat out vec4 vEndpointColorB;\n${varying("out")}` },
  { id: "endpoint-vertex-sampler", before: "uniform sampler2D pointColorsTexture;\n", after: "uniform sampler2D pointColorsTexture;\nuniform sampler2D endpointFootprints;\n" },
  { id: "endpoint-uniform-block", before: "  float pixelRatio;\n} drawLine;", after: `  float pixelRatio;\n${fields.map(f=>`  float ${f};\n`).join("")}} drawLine;` },
  { id: "endpoint-uniform-defines", before: "#define pixelRatio drawLine.pixelRatio\n", after: `#define pixelRatio drawLine.pixelRatio\n${fields.map(f=>`#define ${f} drawLine.${f}\n`).join("")}` },
  { id: "endpoint-uniform-fallback", before: "uniform float pixelRatio;\n#endif\n\nout vec4 rgbaColor;", after: `uniform float pixelRatio;\n${fields.map(f=>`uniform float ${f};\n`).join("")}#endif\n\nout vec4 rgbaColor;${lineTable}` },
  { id: "line-style-row", before: "  vLinkStyle = linkStyle;\n", after: `  vLinkStyle = linkStyle;
  vEndpointAxes = vec4(0.0);
  vEndpointSpec = vec4(0.0);
  vEndpointStroke = vec2(0.0);
  vLinePattern = LINE_PATTERNS[lineStyleRow(linkStyle)];
` },
  // The table's markers replace the upstream arrowhead on every line.
  { id: "line-no-upstream-arrow", before: "  useArrow = arrow;\n", after: "  useArrow = 0.0;\n" },
  { id: "line-endpoint-geometry", before: "  // Quad width (stroke + ramp) in vLinkDashSpan's pattern-space units; the fragment takes the ramp back off.\n", after: lineVertex },
  { id: "line-emphasis-opacity",
    before: "  float opacity = lineColor.a * linkOpacity * max(linkVisibilityMinTransparency, map(linkDistPx, linkVisibilityDistanceRange.g, linkVisibilityDistanceRange.r, 0.0, 1.0));",
    after: `  // An emphasized line keeps at least the emphasis and never fades with distance.
  bool lineEmphasized = linkStyle >= float(LINE_STYLE_ROWS);
  float opacity = lineColor.a * (lineEmphasized ? max(linkOpacity, LINE_EMPHASIS) : linkOpacity)
    * (lineEmphasized ? 1.0 : max(linkVisibilityMinTransparency, map(linkDistPx, linkVisibilityDistanceRange.g, linkVisibilityDistanceRange.r, 0.0, 1.0)));` },
  { id: "endpoint-fragment-functions", before: "void main() {\n  // Geometric coverage only (stroke / arrow / dash).", after: `${fragmentFunctions}\nvoid main() {\n  // Geometric coverage only (stroke / arrow / dash).` },
  { id: "line-fragment", before: "  // Dashed / dotted stroke patterns. Applied to the visible pass only (renderMode == 0.0)\n  // so that gaps stay fully pickable in the index pass. The arrowhead region is left solid.\n  if (renderMode < 0.5 && (vLinkStyle == LINK_STYLE_DASHED || vLinkStyle == LINK_STYLE_DOTTED)) {\n    bool inArrowHead = (useArrow > 0.5) && (pos.x > start_arrow) && (pos.x < end_arrow);\n    if (!inArrowHead) {\n      // Distance along the link in the dash pattern's space (screen px or world units; see the vertex shader).\n      // fwidth() gives the screen-space rate of change, so the ramp is EDGE_RAMP_PX device pixels in either space.\n      float phase = clamp(pos.x, 0.0, 1.0) * vLinkDashSpan;\n      if (vLinkStyle == LINK_STYLE_DASHED) {\n        float period = max(linkDashLength + linkDashGap, 0.001);\n        // Half a ramp in the pattern's units: fwidth(phase) is the change in phase per device pixel.\n        float aa = max(fwidth(phase), 1e-4) * (EDGE_RAMP_PX * 0.5);\n        coverage *= strokeMask(phase, linkDashLength, period, aa);\n      } else {\n        // Dotted: round dots sized to the stroke, the quad less its ramp; spaced by diameter + gap.\n        float diameter = vLinkDashWidth * (1.0 - smoothing);\n        if (linkBlending < 0.5) {\n          // Unblended keeps only pixels whose centre is inside the dot. A dot under \u221a2 device\n          // px can miss every pixel centre and vanish; at \u221a2 it always holds one. Pattern units\n          // per device pixel from the phase's gradient (the pattern space is isotropic).\n          float pxInPattern = max(length(vec2(dFdx(phase), dFdy(phase))), 1e-4);\n          diameter = max(diameter, 1.4142136 * pxInPattern);\n        }\n        float period = max(diameter + linkDashGap, 0.001);\n        float localX = mod(phase, period) - period * 0.5;\n        float localY = pos.y * vLinkDashWidth;\n        float r = length(vec2(localX, localY));\n        // Half a ramp in the pattern's units: fwidth(r) is the change in r per device pixel.\n        float aa = max(fwidth(r), 1e-4) * (EDGE_RAMP_PX * 0.5);\n        coverage *= 1.0 - smoothstep(diameter * 0.5 - aa, diameter * 0.5 + aa, r);\n      }\n    }\n  }\n\n", after: lineFragment },
  { id: "endpoint-uniform-types", before: '          pixelRatio: "f32"\n        },\n        defaultUniforms: {\n          transformationMatrix: i.transformationMatrix4x4,', after: `          pixelRatio: "f32",\n${fields.map(f=>`          ${f}: "f32",\n`).join("")}        },\n        defaultUniforms: {\n          transformationMatrix: i.transformationMatrix4x4,` },
  { id: "endpoint-default-uniforms", before: "          pixelRatio: t.pixelRatio\n        }\n      },\n      drawLineFragmentUniforms:", after: `          pixelRatio: t.pixelRatio,\n${uniformLines("          ", "t")}        }\n      },\n      drawLineFragmentUniforms:` },
  { id: "endpoint-draw-uniforms", before: "        pixelRatio: t.pixelRatio\n      },\n      drawLineFragmentUniforms:", after: `        pixelRatio: t.pixelRatio,\n${uniformLines("        ", "t")}      },\n      drawLineFragmentUniforms:` },
  { id: "endpoint-pick-uniforms", before: "        pixelRatio: e.pixelRatio\n      },\n      drawLineFragmentUniforms:", after: `        pixelRatio: e.pixelRatio,\n${uniformLines("        ", "e")}      },\n      drawLineFragmentUniforms:` },
  { id: "endpoint-draw-footprints", before: "    this.updateLinkBlending();\n", after: "    this.updateLinkBlending();\n    if (i.isSizeTextureStale || !i.sizeTexture) i.updateSizeTexture();\n" },
  { id: "endpoint-pick-footprints", before: "    const n = e.highlightedLinkIndices !== void 0;\n    this.drawLineUniformStore.setUniforms", after: "    if (t.isSizeTextureStale || !t.sizeTexture) t.updateSizeTexture();\n    const n = e.highlightedLinkIndices !== void 0;\n    this.drawLineUniformStore.setUniforms" },
  { id: "endpoint-draw-binding", before: "      pointColorsTexture: i.pointColorsTexture ?? i.currentPositionTexture\n", after: "      pointColorsTexture: i.pointColorsTexture ?? i.currentPositionTexture,\n      endpointFootprints: i.sizeTexture ?? i.currentPositionTexture\n" },
  { id: "endpoint-pick-binding", before: "      pointColorsTexture: t.pointColorsTexture ?? t.currentPositionTexture\n", after: "      pointColorsTexture: t.pointColorsTexture ?? t.currentPositionTexture,\n      endpointFootprints: t.sizeTexture ?? t.currentPositionTexture\n" },
  { id: "endpoint-footprint-shape", before: "      n[o * 4] = i.getResolvedPointFootprint(o);\n", after: "      n[o * 4] = i.getResolvedPointFootprint(o), n[o * 4 + 1] = i.pointShapes?.[o] ?? 0;\n" },
  { id: "endpoint-stale-shape", before: "    const i = t.pointShapes, n = i.byteLength;\n", after: "    this.markFootprintsStale();\n    const i = t.pointShapes, n = i.byteLength;\n" },
  { id: "endpoint-stale-picking", before: "this.points && this.lines && (this.isPointPositionsUpdateNeeded &&",
    after: "this.points && this.lines && ((this.isPointSizeUpdateNeeded || this.isPointShapeUpdateNeeded || this.isPointImageIndicesUpdateNeeded || this.isPointImageSizesUpdateNeeded) && (this.lines.isLinkIndexBufferStale = !0), this.isPointPositionsUpdateNeeded &&" },
].map(hunk => Object.freeze(hunk)));

export function patchCosmosLinkEndpoints(source, version) {
  assert.equal(version, COSMOS_VERSION, "Review the line table shader and footprint uniforms before upgrading Cosmos");
  let original = source;
  for (const hunks of [COSMOS_POINT_IMAGE_SDF_PATCH_HUNKS, COSMOS_POINT_RING_PATCH_HUNKS,
    COSMOS_POINT_PICKER_PATCH_HUNKS, COSMOS_COLLISION_RADII_PATCH_HUNKS]) {
    for (const { id, before, after } of [...hunks].reverse()) {
      assert.equal(original.split(after).length, 2, `Expected exactly one reviewed ${id} seam`);
      original = original.replace(after, before);
    }
  }
  assert.equal(sha256(original), COSMOS_SOURCE_SHA256, "Cosmos source differs from the reviewed package");
  let modified = source;
  for (const { id, before, after } of COSMOS_LINK_ENDPOINT_PATCH_HUNKS) {
    assert.equal(modified.split(before).length, 2, `Expected exactly one ${id} seam`);
    modified = modified.replace(before, after);
  }
  let reversed = modified;
  for (const { id, before, after } of [...COSMOS_LINK_ENDPOINT_PATCH_HUNKS].reverse()) {
    assert.equal(reversed.split(after).length, 2, `Expected exactly one patched ${id} seam`);
    reversed = reversed.replace(after, before);
  }
  assert.equal(reversed, source, "Unexpected extra line table SDK change");
  return modified;
}

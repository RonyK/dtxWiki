import assert from "node:assert/strict";
import { COSMOS_COLLISION_RADII_PATCH_HUNKS } from "./cosmos-collision-radii-patch.mjs";
import { COSMOS_POINT_PICKER_PATCH_HUNKS } from "./cosmos-point-picker-patch.mjs";
import { COSMOS_LINK_PATTERN_PATCH_HUNKS } from "./cosmos-link-pattern-patch.mjs";
import { COSMOS_POINT_RING_PATCH_HUNKS } from "./cosmos-point-ring-patch.mjs";
import { COSMOS_POINT_IMAGE_SDF_PATCH_HUNKS } from "./cosmos-point-image-sdf-patch.mjs";
import { COSMOS_SOURCE_SHA256, COSMOS_VERSION, sha256 } from "./cosmos-source.mjs";

// Optional batched endpoint geometry. Reuse the point footprint texture so
// marker tips follow current GPU positions without per-edge readback or DOM.
const fields = ["linkEndpointMarkers", "linkEndpointArrowLength", "linkEndpointArrowHalfWidth",
  "linkEndpointDiamondLength", "linkEndpointDiamondHalfWidth", "linkEndpointNodeGap",
  "endpointPointSizeScale", "endpointScalePointsOnZoom"];
const values = config => [ `${config}.linkEndpointMarkers ? 1 : 0`, `${config}.linkEndpointArrowLength`,
  `${config}.linkEndpointArrowHalfWidth`, `${config}.linkEndpointDiamondLength`,
  `${config}.linkEndpointDiamondHalfWidth`, `${config}.linkEndpointNodeGap`,
  `${config}.pointSizeScale`, `${config}.scalePointsOnZoom ? 1 : 0` ];
const uniformLines = (indent, config) => fields.map((field, index) => `${indent}${field}: ${values(config)[index]},\n`).join("");
const varying = direction => `flat ${direction} vec4 vEndpointAxes;\nflat ${direction} vec4 vEndpointSpec;\nflat ${direction} vec2 vEndpointStroke;\n`;
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
`;
const vertexFunctions = `
float endpointNodeRadius(vec4 footprint, vec2 direction) {
  float zoom = endpointScalePointsOnZoom > 0.5 ? transformationMatrix[0][0]
    : min(5.0, max(1.0, transformationMatrix[0][0] * 0.01));
  float diameter = min(footprint.r * endpointPointSizeScale * zoom, maxPointSize);
  float radius = max(diameter, 1.0 / pixelRatio) * 0.5;
  // Native square body (shape 1); folder rings and count discs use a circle.
  if (footprint.g > 0.5 && footprint.g < 1.5) radius *= 0.8 / max(max(abs(direction.x), abs(direction.y)), 0.0001);
  return radius + linkEndpointNodeGap;
}
`;

export const COSMOS_LINK_ENDPOINT_PATCH_HUNKS = Object.freeze([
  { id: "endpoint-config", before: "  linkArrowsSizeScale: 1,\n", after: `  linkArrowsSizeScale: 1,
  linkEndpointMarkers: !1,
  linkEndpointArrowLength: 6,
  linkEndpointArrowHalfWidth: 4,
  linkEndpointDiamondLength: 10,
  linkEndpointDiamondHalfWidth: 4,
  linkEndpointNodeGap: 1.5,
` },
  { id: "endpoint-varyings-fragment", before: "flat in vec4 vEndpointColorB;\n", after: `flat in vec4 vEndpointColorB;\n${varying("in")}` },
  { id: "endpoint-varyings-vertex", before: "flat out vec4 vEndpointColorB;\n", after: `flat out vec4 vEndpointColorB;\n${varying("out")}${vertexFunctions}` },
  { id: "endpoint-vertex-sampler", before: "uniform sampler2D pointColorsTexture;\n", after: "uniform sampler2D pointColorsTexture;\nuniform sampler2D endpointFootprints;\n" },
  { id: "endpoint-uniform-block", before: "  float pixelRatio;\n} drawLine;", after: `  float pixelRatio;\n${fields.map(f=>`  float ${f};\n`).join("")}} drawLine;` },
  { id: "endpoint-uniform-defines", before: "#define pixelRatio drawLine.pixelRatio\n", after: `#define pixelRatio drawLine.pixelRatio\n${fields.map(f=>`#define ${f} drawLine.${f}\n`).join("")}` },
  { id: "endpoint-uniform-fallback", before: "uniform float pixelRatio;\n#endif\n\nout vec4 rgbaColor;", after: `uniform float pixelRatio;\n${fields.map(f=>`uniform float ${f};\n`).join("")}#endif\n\nout vec4 rgbaColor;` },
  { id: "endpoint-varying-defaults", before: "  vLinkStyle = linkStyle;\n", after: `  vLinkStyle = linkStyle;
  vEndpointAxes = vec4(0.0);
  vEndpointSpec = vec4(0.0);
  vEndpointStroke = vec2(0.0);
` },
  { id: "endpoint-disable-old-arrow", before: "  useArrow = arrow;\n", after: "  useArrow = linkEndpointMarkers > 0.5 ? 0.0 : arrow;\n" },
  { id: "endpoint-expanded-quad", before: "  // Quad width (stroke + ramp) in vLinkDashSpan's pattern-space units; the fragment takes the ramp back off.\n", after: `  if (linkEndpointMarkers > 0.5) {
    if (linkDistPx <= 0.0001) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
    vec2 direction = xBasis / linkDist;
    float sourceClip = endpointNodeRadius(texelFetch(endpointFootprints, pointTexelA, 0), direction);
    float targetClip = endpointNodeRadius(texelFetch(endpointFootprints, pointTexelB, 0), direction);
    float available = max(linkDistPx - sourceClip - targetClip, 0.0);
    float sourceMarkerLength = linkStyle == 2.0 ? linkEndpointDiamondLength : 0.0;
    float markerScale = min(1.0, available / max(linkEndpointArrowLength + sourceMarkerLength + 2.0, 1.0));
    vEndpointSpec = vec4(linkEndpointArrowLength, linkEndpointArrowHalfWidth,
      sourceMarkerLength, linkEndpointDiamondHalfWidth) * markerScale;
    vEndpointStroke = vec2(max((linkWidthPx - smoothingPx) * transformationMatrix[0][0], 0.1), 1.0);
    float markerWidth = 2.0 * max(linkEndpointArrowHalfWidth, linkEndpointDiamondHalfWidth) + vEndpointStroke.x;
    linkWidthPx = max(linkWidthPx, markerWidth / transformationMatrix[0][0] + smoothingPx);
    vEndpointAxes = vec4(linkDistPx, linkWidthPx * transformationMatrix[0][0], sourceClip, targetClip);
  }

  // Quad width (stroke + ramp) in vLinkDashSpan's pattern-space units; the fragment takes the ramp back off.
` },
  { id: "endpoint-fragment-functions", before: "void main() {\n  // Geometric coverage only (stroke / arrow / dash).", after: `${fragmentFunctions}\nvoid main() {\n  // Geometric coverage only (stroke / arrow / dash).` },
  { id: "endpoint-fragment-body", before: "  } else coverage = smoothstep(0.5, 0.5 - smoothing, abs(pos.y));\n", after: `  } else coverage = smoothstep(0.5, 0.5 - smoothing, abs(pos.y));

  if (vEndpointStroke.y > 0.5 && renderMode < 0.5) {
    float x = pos.x * vEndpointAxes.x;
    float y = pos.y * vEndpointAxes.y;
    float start = vEndpointAxes.z + vEndpointSpec.z;
    float end = vEndpointAxes.x - vEndpointAxes.w;
    coverage = endpointInk(abs(y), vEndpointStroke.x * 0.5);
    if (x < start || x > end) coverage = 0.0;
  }
` },
  { id: "endpoint-fragment-markers", before: "  float opacity = rgbaColor.a * coverage;\n", after: `  if (vEndpointStroke.y > 0.5) {
    vec2 point = vec2(pos.x * vEndpointAxes.x, pos.y * vEndpointAxes.y);
    float markerAa = max(max(fwidth(point.x), fwidth(point.y)), 0.0001) * 0.5;
    float start = vEndpointAxes.z;
    float end = vEndpointAxes.x - vEndpointAxes.w;
    if (point.x < start || point.x > end || end <= start) coverage = 0.0;
    else if (renderMode < 0.5) {
      // Most fragments are in the line body; only endpoint neighbourhoods
      // pay for segment distances, at every admitted scene size.
      float distance = 1000000.0;
      if (point.x >= end - vEndpointSpec.x - vEndpointStroke.x) {
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
      float halfStroke = vEndpointStroke.x * 0.5;
      coverage = max(coverage, 1.0 - smoothstep(halfStroke - markerAa, halfStroke + markerAa, distance));
    }
  }

  float opacity = rgbaColor.a * coverage;
` },
  { id: "endpoint-uniform-types", before: '          pixelRatio: "f32"\n        },\n        defaultUniforms: {\n          transformationMatrix: i.transformationMatrix4x4,', after: `          pixelRatio: "f32",\n${fields.map(f=>`          ${f}: "f32",\n`).join("")}        },\n        defaultUniforms: {\n          transformationMatrix: i.transformationMatrix4x4,` },
  { id: "endpoint-default-uniforms", before: "          pixelRatio: t.pixelRatio\n        }\n      },\n      drawLineFragmentUniforms:", after: `          pixelRatio: t.pixelRatio,\n${uniformLines("          ", "t")}        }\n      },\n      drawLineFragmentUniforms:` },
  { id: "endpoint-draw-uniforms", before: "        pixelRatio: t.pixelRatio\n      },\n      drawLineFragmentUniforms:", after: `        pixelRatio: t.pixelRatio,\n${uniformLines("        ", "t")}      },\n      drawLineFragmentUniforms:` },
  { id: "endpoint-pick-uniforms", before: "        pixelRatio: e.pixelRatio\n      },\n      drawLineFragmentUniforms:", after: `        pixelRatio: e.pixelRatio,\n${uniformLines("        ", "e")}      },\n      drawLineFragmentUniforms:` },
  { id: "endpoint-draw-footprints", before: "    this.updateLinkBlending();\n", after: "    this.updateLinkBlending();\n    if (t.linkEndpointMarkers && (i.isSizeTextureStale || !i.sizeTexture)) i.updateSizeTexture();\n" },
  { id: "endpoint-pick-footprints", before: "    const n = e.highlightedLinkIndices !== void 0;\n    this.drawLineUniformStore.setUniforms", after: "    if (e.linkEndpointMarkers && (t.isSizeTextureStale || !t.sizeTexture)) t.updateSizeTexture();\n    const n = e.highlightedLinkIndices !== void 0;\n    this.drawLineUniformStore.setUniforms" },
  { id: "endpoint-draw-binding", before: "      pointColorsTexture: i.pointColorsTexture ?? i.currentPositionTexture\n", after: "      pointColorsTexture: i.pointColorsTexture ?? i.currentPositionTexture,\n      endpointFootprints: i.sizeTexture ?? i.currentPositionTexture\n" },
  { id: "endpoint-pick-binding", before: "      pointColorsTexture: t.pointColorsTexture ?? t.currentPositionTexture\n", after: "      pointColorsTexture: t.pointColorsTexture ?? t.currentPositionTexture,\n      endpointFootprints: t.sizeTexture ?? t.currentPositionTexture\n" },
  { id: "endpoint-footprint-shape", before: "      n[o * 4] = i.getResolvedPointFootprint(o);\n", after: "      n[o * 4] = i.getResolvedPointFootprint(o), n[o * 4 + 1] = i.pointShapes?.[o] ?? 0;\n" },
  { id: "endpoint-stale-shape", before: "    const i = t.pointShapes, n = i.byteLength;\n", after: "    this.markFootprintsStale();\n    const i = t.pointShapes, n = i.byteLength;\n" },
  { id: "endpoint-stale-picking", before: "this.points && this.lines && (this.isPointPositionsUpdateNeeded &&",
    after: "this.points && this.lines && (this.config.linkEndpointMarkers && (this.isPointSizeUpdateNeeded || this.isPointShapeUpdateNeeded || this.isPointImageIndicesUpdateNeeded || this.isPointImageSizesUpdateNeeded) && (this.lines.isLinkIndexBufferStale = !0), this.isPointPositionsUpdateNeeded &&" },
].map(hunk => Object.freeze(hunk)));

export function patchCosmosLinkEndpoints(source, version) {
  assert.equal(version, COSMOS_VERSION, "Review endpoint shader and footprint uniforms before upgrading Cosmos");
  let original = source;
  for (const hunks of [COSMOS_POINT_IMAGE_SDF_PATCH_HUNKS, COSMOS_POINT_RING_PATCH_HUNKS,
    COSMOS_LINK_PATTERN_PATCH_HUNKS, COSMOS_POINT_PICKER_PATCH_HUNKS, COSMOS_COLLISION_RADII_PATCH_HUNKS]) {
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
  assert.equal(reversed, source, "Unexpected extra endpoint SDK change");
  return modified;
}

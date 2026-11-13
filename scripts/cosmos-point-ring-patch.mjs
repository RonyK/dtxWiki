import assert from "node:assert/strict";
import { COSMOS_COLLISION_RADII_PATCH_HUNKS } from "./cosmos-collision-radii-patch.mjs";
import { COSMOS_POINT_PICKER_PATCH_HUNKS } from "./cosmos-point-picker-patch.mjs";
import { COSMOS_SOURCE_SHA256, COSMOS_VERSION, sha256 } from "./cosmos-source.mjs";

// A ninth point shape, RING (9): a band `pointRingWidth` CSS px wide (× pixelRatio
// device px) at every size and zoom, over a disc of the background colour. A
// texture ring scales with the point and breaks up below a few pixels.
const band = "pointRingBandPx";
const bandValue = config => `${config}.pointRingWidth * ${config}.pixelRatio`;

/** Reversible ring and triangle presentation changes; force shaders stay intact. */
export const COSMOS_POINT_RING_PATCH_HUNKS = Object.freeze([
  { id: "triangle-normalized-upward",
    before: `float triangleDistance(vec2 p) {
    const float k = sqrt(3.0);   // ≈1.732; slope of 60° lines for an equilateral triangle
    const float r = 0.9;         // half the edge length; each edge is 2r
    p.x = abs(p.x) - r;          // fold the X axis and shift: the base corner is at the origin
    p.y += 0.55;                 // move the base (y = -0.55 in the core) to y = 0. The centred offset is
                                 // r/k = 0.52; 0.55 keeps the apex at 1.01 rather than 1.04

    // Reflect across the corner's bisector: the slanted edge maps onto the x axis,
    // corner at x = 0, apex at x = -2r
    if (p.x + k * p.y > 0.0)
        p = vec2(p.x - k * p.y,  -k * p.x - p.y) / 2.0;

    // Distance to the edge as the segment [-2r, 0] on the x axis
    p.x -= clamp(p.x, -2.0 * r, 0.0);

    // Return signed distance: negative = inside; positive = outside
    return -length(p) * sign(p.y);
}`,
    after: `float triangleDistance(vec2 p) {
    // Centred equilateral triangle, circumradius 1; gl_PointCoord grows downward.
    // The host applies the shared legend's 1.35 area compensation to its diameter.
    const float k = sqrt(3.0);
    const float r = sqrt(3.0) * 0.5;
    p.y = -p.y;
    p.x = abs(p.x) - r;
    p.y += 0.5;
    if (p.x + k * p.y > 0.0)
        p = vec2(p.x - k * p.y, -k * p.x - p.y) / 2.0;
    p.x -= clamp(p.x, -2.0 * r, 0.0);
    return -length(p) * sign(p.y);
}` },
  { id: "ring-config-default", before: "  pointOpacity: 1,\n  pointGreyoutOpacity: void 0,",
    after: "  pointOpacity: 1,\n  pointRingWidth: 2,\n  pointGreyoutOpacity: void 0," },
  { id: "ring-default-shape-range", before: "t >= 0 && t <= 8 ? t : E.pointDefaultShape",
    after: "t >= 0 && t <= 9 ? t : E.pointDefaultShape" },
  { id: "ring-shape-range", before: "(!Number.isInteger(s) || s < 0 || s > 8) && (n[o] = i);",
    after: "(!Number.isInteger(s) || s < 0 || s > 9) && (n[o] = i);" },
  { id: "ring-uniform-block", before: "  float outlineWidth;\n  float renderMode;\n} drawFragment;",
    after: `  float outlineWidth;\n  float ${band};\n  float renderMode;\n} drawFragment;` },
  { id: "ring-uniform-define", before: "#define outlineWidth drawFragment.outlineWidth\n",
    after: `#define outlineWidth drawFragment.outlineWidth\n#define ${band} drawFragment.${band}\n` },
  { id: "ring-uniform", before: "uniform float outlineWidth;\n",
    after: `uniform float outlineWidth;\nuniform float ${band};\n` },
  { id: "ring-shape-constant", before: "const float NONE = 8.0;\n",
    after: "const float NONE = 8.0;\nconst float RING = 9.0;\n" },
  { id: "ring-shape-coverage",
    before: "        float opacity;\n        if (pointShape == CIRCLE) {",
    after: `        float opacity;
        // dtxWiki RING: share of the shape colour; the rest is the background disc.
        float ringBand = 1.0;
        if (pointShape == RING) {
            float rPx = length(coreCoord) * coreDiameterPx * 0.5;
            float outerPx = coreDiameterPx * 0.5;
            float innerPx = outerPx - ${band};
            opacity = 1.0 - smoothstep(outerPx - halfRampPx, outerPx + halfRampPx, rPx);
            ringBand = smoothstep(innerPx - halfRampPx, innerPx + halfRampPx, rPx);
        } else if (pointShape == CIRCLE) {` },
  { id: "ring-shape-colour", before: "        finalShapeColor = vec4(shapeColor.rgb, opacity);\n",
    after: "        finalShapeColor = vec4(mix(backgroundColor.rgb, shapeColor.rgb, ringBand), opacity);\n" },
  { id: "ring-uniform-type", before: "          outlineWidth: \"f32\",\n          renderMode: \"f32\"",
    after: `          outlineWidth: "f32",\n          ${band}: "f32",\n          renderMode: "f32"` },
  { id: "ring-default-uniform", before: "          outlineWidth: 0.9,\n          renderMode: 0\n        }",
    after: `          outlineWidth: 0.9,\n          ${band}: ${bandValue("t")},\n          renderMode: 0\n        }` },
  { id: "ring-draw-uniform", before: "      outlineWidth: 0.9,\n      renderMode: 0\n    }, r = {",
    after: `      outlineWidth: 0.9,\n      ${band}: ${bandValue("i")},\n      renderMode: 0\n    }, r = {` },
].map(hunk => Object.freeze(hunk)));

/** Applied last, to the collision-radii, point-picker and link-pattern output. */
export function patchCosmosPointRing(source, version) {
  assert.equal(version, COSMOS_VERSION, "Review the ring and triangle geometry before upgrading Cosmos");
  let original = source;
  for (const hunks of [COSMOS_POINT_PICKER_PATCH_HUNKS, COSMOS_COLLISION_RADII_PATCH_HUNKS]) {
    for (const { id, before, after } of [...hunks].reverse()) {
      assert.equal(original.split(after).length, 2, `Expected exactly one reviewed ${id} seam`);
      original = original.replace(after, before);
    }
  }
  assert.equal(sha256(original), COSMOS_SOURCE_SHA256, "Cosmos source differs from the reviewed package");
  let modified = source;
  for (const { id, before, after } of COSMOS_POINT_RING_PATCH_HUNKS) {
    assert.equal(modified.split(before).length, 2, `Expected exactly one ${id} seam`);
    modified = modified.replace(before, after);
  }
  let reversed = modified;
  for (const { id, before, after } of [...COSMOS_POINT_RING_PATCH_HUNKS].reverse()) {
    assert.equal(reversed.split(after).length, 2, `Expected exactly one patched ${id} seam`);
    reversed = reversed.replace(after, before);
  }
  assert.equal(reversed, source, "Unexpected extra point ring SDK change");
  return modified;
}

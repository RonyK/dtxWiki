import assert from "node:assert/strict";
import { COSMOS_COLLISION_RADII_PATCH_HUNKS } from "./cosmos-collision-radii-patch.mjs";
import { COSMOS_POINT_PICKER_PATCH_HUNKS } from "./cosmos-point-picker-patch.mjs";
import { COSMOS_POINT_RING_PATCH_HUNKS } from "./cosmos-point-ring-patch.mjs";
import { COSMOS_SOURCE_SHA256, COSMOS_VERSION, sha256 } from "./cosmos-source.mjs";

// Optional `pointImageSdf`: every point image's alpha is a signed distance
// field (0.5 at the edge), decoded per pixel, and the atlas is sampled
// linearly. Text and outlines then stay crisp at any drawn size; upstream
// images are nearest-sampled bitmaps that break up when scaled.
const flag = "pointImageSdf";

/** Every change is reversible; with the option off, images render as upstream. */
export const COSMOS_POINT_IMAGE_SDF_PATCH_HUNKS = Object.freeze([
  { id: "sdf-config-default", before: "  pointRingWidth: 2,\n", after: `  pointRingWidth: 2,\n  ${flag}: !1,\n` },
  { id: "sdf-uniform-block", before: "  float pointRingBandPx;\n  float renderMode;\n} drawFragment;",
    after: `  float pointRingBandPx;\n  float ${flag};\n  float renderMode;\n} drawFragment;` },
  { id: "sdf-uniform-define", before: "#define pointRingBandPx drawFragment.pointRingBandPx\n",
    after: `#define pointRingBandPx drawFragment.pointRingBandPx\n#define ${flag} drawFragment.${flag}\n` },
  { id: "sdf-uniform", before: "uniform float pointRingBandPx;\n",
    after: `uniform float pointRingBandPx;\nuniform float ${flag};\n` },
  { id: "sdf-decode", before: "const float RING = 9.0;\n",
    after: `const float RING = 9.0;

// dtxWiki: an image whose alpha is a signed distance field. A sprite's fragments
// share its image, so the derivative is taken in coherent control flow.
vec4 decodePointImage(vec4 imageColor) {
    if (${flag} < 0.5) return imageColor;
    float aa = max(fwidth(imageColor.a), 1e-4) * 0.75;
    return vec4(imageColor.rgb, smoothstep(0.5 - aa, 0.5 + aa, imageColor.a));
}
` },
  { id: "sdf-centred-sample",
    before: "                vec4 imageColor = texture(imageAtlasTexture, atlasUV);\n",
    after: "                vec4 imageColor = decodePointImage(texture(imageAtlasTexture, atlasUV));\n" },
  { id: "sdf-full-sample",
    before: "            vec4 imageColor = texture(imageAtlasTexture, atlasUV);\n            finalImageColor",
    after: "            vec4 imageColor = decodePointImage(texture(imageAtlasTexture, atlasUV));\n            finalImageColor" },
  { id: "sdf-uniform-type", before: "          pointRingBandPx: \"f32\",\n",
    after: `          pointRingBandPx: "f32",\n          ${flag}: "f32",\n` },
  { id: "sdf-default-uniform", before: "          pointRingBandPx: t.pointRingWidth * t.pixelRatio,\n",
    after: `          pointRingBandPx: t.pointRingWidth * t.pixelRatio,\n          ${flag}: t.${flag} ? 1 : 0,\n` },
  { id: "sdf-draw-uniform", before: "      pointRingBandPx: i.pointRingWidth * i.pixelRatio,\n",
    after: `      pointRingBandPx: i.pointRingWidth * i.pixelRatio,\n      ${flag}: i.${flag} ? 1 : 0,\n` },
  { id: "sdf-atlas-sampler",
    before: "this.imageAtlasTexture = e.createTexture({\n      width: s,\n      height: s,\n      format: \"rgba8unorm\"\n    })",
    after: `this.imageAtlasTexture = e.createTexture({\n      width: s,\n      height: s,\n      format: "rgba8unorm",\n      ...this.config.${flag} ? { sampler: { minFilter: "linear", magFilter: "linear" } } : {}\n    })` },
].map(hunk => Object.freeze(hunk)));

/** Applied last, to the collision-radii, point-picker, link-pattern and point-ring output. */
export function patchCosmosPointImageSdf(source, version) {
  assert.equal(version, COSMOS_VERSION, "Review the SDF image decode before upgrading Cosmos");
  let original = source;
  for (const hunks of [COSMOS_POINT_RING_PATCH_HUNKS,
    COSMOS_POINT_PICKER_PATCH_HUNKS, COSMOS_COLLISION_RADII_PATCH_HUNKS]) {
    for (const { id, before, after } of [...hunks].reverse()) {
      assert.equal(original.split(after).length, 2, `Expected exactly one reviewed ${id} seam`);
      original = original.replace(after, before);
    }
  }
  assert.equal(sha256(original), COSMOS_SOURCE_SHA256, "Cosmos source differs from the reviewed package");
  let modified = source;
  for (const { id, before, after } of COSMOS_POINT_IMAGE_SDF_PATCH_HUNKS) {
    assert.equal(modified.split(before).length, 2, `Expected exactly one ${id} seam`);
    modified = modified.replace(before, after);
  }
  let reversed = modified;
  for (const { id, before, after } of [...COSMOS_POINT_IMAGE_SDF_PATCH_HUNKS].reverse()) {
    assert.equal(reversed.split(after).length, 2, `Expected exactly one patched ${id} seam`);
    reversed = reversed.replace(after, before);
  }
  assert.equal(reversed, source, "Unexpected extra point image SDF SDK change");
  return modified;
}

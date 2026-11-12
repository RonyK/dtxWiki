import assert from "node:assert/strict";
import { COSMOS_COLLISION_RADII_PATCH_HUNKS } from "./cosmos-collision-radii-patch.mjs";
import { COSMOS_POINT_PICKER_PATCH_HUNKS } from "./cosmos-point-picker-patch.mjs";
import { COSMOS_SOURCE_SHA256, COSMOS_VERSION, sha256 } from "./cosmos-source.mjs";

// The dotted link style may draw a second dash pattern (screen px, like the
// global dash). A length of 0 keeps the upstream round dots.
const fields = ["linkDottedDashLength", "linkDottedDashGap"];
const uniformLines = (indent, value) => fields.map(field => `${indent}${field}: ${value(field)},\n`).join("");

/** Every change is reversible; force shaders and the dashed/solid strokes stay intact. */
export const COSMOS_LINK_PATTERN_PATCH_HUNKS = Object.freeze([
  { id: "dotted-dash-defaults", before: "  linkDashGap: 4,\n",
    after: `  linkDashGap: 4,\n${uniformLines("  ", () => "0")}` },
  { id: "dotted-dash-block", before: "  float linkDashGap;\n  float linkColorInterpolateFromEndpoints;",
    after: `  float linkDashGap;\n${fields.map(field => `  float ${field};\n`).join("")}  float linkColorInterpolateFromEndpoints;` },
  { id: "dotted-dash-defines", before: "#define linkDashGap drawLineFrag.linkDashGap\n",
    after: `#define linkDashGap drawLineFrag.linkDashGap\n${fields.map(field => `#define ${field} drawLineFrag.${field}\n`).join("")}` },
  { id: "dotted-dash-uniforms", before: "uniform float linkDashGap;\n",
    after: `uniform float linkDashGap;\n${fields.map(field => `uniform float ${field};\n`).join("")}` },
  { id: "dotted-dash-stroke",
    before: "      } else {\n        // Dotted: round dots sized to the stroke,",
    after: `      } else if (linkDottedDashLength > 0.0) {
        // dtxWiki: the dotted style's own dash pattern, masked like the dashed one.
        float period = max(linkDottedDashLength + linkDottedDashGap, 0.001);
        float aa = max(fwidth(phase), 1e-4) * (EDGE_RAMP_PX * 0.5);
        coverage *= strokeMask(phase, linkDottedDashLength, period, aa);
      } else {
        // Dotted: round dots sized to the stroke,` },
  { id: "dotted-dash-uniform-types", before: "          linkDashGap: \"f32\",\n",
    after: `          linkDashGap: "f32",\n${uniformLines("          ", () => "\"f32\"")}` },
  { id: "dotted-dash-default-uniforms", before: "          linkDashGap: t.linkDashGap,\n",
    after: `          linkDashGap: t.linkDashGap,\n${uniformLines("          ", field => `t.${field}`)}` },
  { id: "dotted-dash-draw-uniforms", before: "        linkDashGap: t.linkDashGap,\n        linkColorInterpolateFromEndpoints",
    after: `        linkDashGap: t.linkDashGap,\n${uniformLines("        ", field => `t.${field}`)}        linkColorInterpolateFromEndpoints` },
  { id: "dotted-dash-picking-uniforms", before: "        linkDashGap: e.linkDashGap,\n",
    after: `        linkDashGap: e.linkDashGap,\n${uniformLines("        ", field => `e.${field}`)}` },
].map(hunk => Object.freeze(hunk)));

/** Applied last, to the collision-radii and point-picker output. */
export function patchCosmosLinkPattern(source, version) {
  assert.equal(version, COSMOS_VERSION, "Review link pattern uniforms before upgrading Cosmos");
  let original = source;
  for (const hunks of [COSMOS_POINT_PICKER_PATCH_HUNKS, COSMOS_COLLISION_RADII_PATCH_HUNKS]) {
    for (const { id, before, after } of [...hunks].reverse()) {
      assert.equal(original.split(after).length, 2, `Expected exactly one reviewed ${id} seam`);
      original = original.replace(after, before);
    }
  }
  assert.equal(sha256(original), COSMOS_SOURCE_SHA256, "Cosmos source differs from the reviewed package");
  let modified = source;
  for (const { id, before, after } of COSMOS_LINK_PATTERN_PATCH_HUNKS) {
    assert.equal(modified.split(before).length, 2, `Expected exactly one ${id} seam`);
    modified = modified.replace(before, after);
  }
  let reversed = modified;
  for (const { id, before, after } of [...COSMOS_LINK_PATTERN_PATCH_HUNKS].reverse()) {
    assert.equal(reversed.split(after).length, 2, `Expected exactly one patched ${id} seam`);
    reversed = reversed.replace(after, before);
  }
  assert.equal(reversed, source, "Unexpected extra link pattern SDK change");
  return modified;
}

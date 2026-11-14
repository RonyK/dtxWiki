import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile, mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { transform, version as esbuildVersion } from "esbuild";

import { COSMOS_VERSION, COSMOS_SOURCE_SHA256, sha256 } from "./cosmos-source.mjs";
import { patchCosmosCollisionRadii, COSMOS_COLLISION_RADII_PATCH_HUNKS } from "./cosmos-collision-radii-patch.mjs";
import { patchCosmosPointPicker, COSMOS_POINT_PICKER_PATCH_HUNKS } from "./cosmos-point-picker-patch.mjs";
import { patchCosmosPointRing, COSMOS_POINT_RING_PATCH_HUNKS } from "./cosmos-point-ring-patch.mjs";
import { patchCosmosPointImageSdf, COSMOS_POINT_IMAGE_SDF_PATCH_HUNKS } from "./cosmos-point-image-sdf-patch.mjs";
import { patchCosmosLinkEndpoints, COSMOS_LINK_ENDPOINT_PATCH_HUNKS } from "./cosmos-link-endpoint-patch.mjs";
// Typecheck/test hooks may regenerate while Vite is open. Identical outputs
// must not trigger HMR or replace a running fixture's document/renderer.
import { writeFileIfChanged as writeFile } from "./write-if-changed.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const sdkEntry = require.resolve("@cosmos.gl/graph");
const sdkRoot = path.resolve(path.dirname(sdkEntry), "..");
const source = await readFile(sdkEntry, "utf8");
const { version } = JSON.parse(await readFile(path.join(sdkRoot, "package.json"), "utf8"));
const modified = patchCosmosLinkEndpoints(patchCosmosPointImageSdf(patchCosmosPointRing(
  patchCosmosPointPicker(patchCosmosCollisionRadii(source, version), version), version), version), version);
const license = await readFile(path.join(sdkRoot, "LICENCE"), "utf8");
const generated = path.join(root, "frontend/src/features/graph/workspace/generated-cosmos-native");
const notices = path.join(root, "frontend/public/third-party/cosmos-native");

// Native forces remain upstream-owned. Only optional glyph collision radii,
// picking ownership, borrowed tracked-position inputs, the graph line table
// (patterns and endpoint markers from graphTokens.ts), an upward centred triangle,
// a constant-width ring point shape and signed-distance point images are extended.
// Never overwrite node_modules or reuse the now-stale upstream TypeScript map:
// this source map truthfully embeds the modified bundle as its source.
const result = await transform(modified.replace(/\/\/# sourceMappingURL=[^\n]*\n?$/u, ""), {
  format: "esm", target: "es2022", sourcemap: "external", sourcesContent: true,
  sourcefile: `cosmos-native-${COSMOS_VERSION}.source.js`,
  banner: "// dtxWiki glyph collision radii and point picking extensions for Cosmos 3.4.2 (MIT). Native forces unchanged. See third-party/cosmos-native/LICENSE.txt.",
  legalComments: "inline",
});
const code = `${result.code}\n//# sourceMappingURL=index.js.map\n`;
// native.7 draws every graph line from the one line table in graphTokens.ts.
const manifest = JSON.stringify({
  artifact: "dtxwiki-cosmos-native", artifactVersion: `${COSMOS_VERSION}-native.7`,
  upstream: { package: "@cosmos.gl/graph", version: COSMOS_VERSION, license: "MIT", sha256: COSMOS_SOURCE_SHA256 },
  modification: "Optional per-point native collision radius column, point-picker host extension and active pointer-drag index remapping and synchronous explicit camera transforms, GPU-only tracked-position framebuffer access, one generated line table (dash/dot patterns, clipped open-arrow, hollow-diamond and compact filled-triangle endpoints, emphasis) replacing the upstream stroke styles and arrowheads, a centred upward equilateral triangle (2) and a ring point shape (9) with a constant CSS-px band and optional signed-distance point images on a linearly sampled atlas. Default/null/0/false preserve existing point behavior; native force shaders, coefficients and integration are unchanged.",
  modifiedSourceSha256: sha256(modified),
  collisionRadiiSeamSha256: sha256(JSON.stringify(COSMOS_COLLISION_RADII_PATCH_HUNKS)),
  pointPickerSeamSha256: sha256(JSON.stringify(COSMOS_POINT_PICKER_PATCH_HUNKS)),
  pointRingSeamSha256: sha256(JSON.stringify(COSMOS_POINT_RING_PATCH_HUNKS)),
  pointImageSdfSeamSha256: sha256(JSON.stringify(COSMOS_POINT_IMAGE_SDF_PATCH_HUNKS)),
  linkEndpointSeamSha256: sha256(JSON.stringify(COSMOS_LINK_ENDPOINT_PATCH_HUNKS)),
  generatedModuleSha256: sha256(code), generatedMapSha256: sha256(result.map),
  compiler: { package: "esbuild", version: esbuildVersion, target: "es2022" },
  licenseSha256: sha256(license),
}, null, 2);
// Existing checkouts can retain ignored outputs from the retired generator.
// Remove only its generated trees so Vite cannot redistribute obsolete notices.
await Promise.all([
  mkdir(generated, { recursive: true }), mkdir(notices, { recursive: true }),
  rm(path.join(root, "frontend/src/features/graph/workspace/generated-cosmos-rest-log"), { recursive: true, force: true }),
  rm(path.join(root, "frontend/public/third-party/cosmos-rest-log"), { recursive: true, force: true }),
]);
await Promise.all([
  writeFile(path.join(generated, "index.js"), code),
  writeFile(path.join(generated, "index.js.map"), result.map),
  writeFile(path.join(generated, "index.d.ts"), 'export * from "@cosmos.gl/graph";\nimport { Graph as UpstreamGraph, type GraphConfig } from "@cosmos.gl/graph";\nimport type { CosmosPointPicker } from "../cosmosPointPickingTypes";\n/** Link styles select rows of the generated line table (graphTokens.ts GRAPH_LINE_STYLE_ROWS). */\nexport type NativeGraphConfig = GraphConfig & { /** CSS-px band of point shape 9, a ring over a background-coloured disc. */ pointRingWidth?: number; /** Point images carry a signed distance field in alpha (0.5 = edge). */ pointImageSdf?: boolean };\nexport declare class Graph extends UpstreamGraph {\n  constructor(div: HTMLDivElement, config?: NativeGraphConfig, devicePromise?: ConstructorParameters<typeof UpstreamGraph>[2]);\n  setConfig(config: NativeGraphConfig): void;\n  setConfigPartial(config: NativeGraphConfig): void;\n  setPointCollisionRadii(radii: Float32Array | null): void;\n  setZoomTransformByPointPositions(positions: Float32Array | readonly number[], duration?: number, scale?: number, padding?: number, enableSimulation?: boolean): void;\n  setCameraCenter(position: readonly number[], zoom: number): void;\n  remapDraggingPoint(index: number | null): void;\n  remapPointer(point: number | null, link: number | null): void;\n  attachPointPicker(picker: CosmosPointPicker | null): void;\n  getTrackedPointPositionsFrame(): { gl: WebGL2RenderingContext; framebuffer: WebGLFramebuffer; width: number; height: number; indices: readonly number[] } | null;\n}\n'),
  writeFile(path.join(notices, "LICENSE.txt"), license),
  writeFile(path.join(notices, "provenance.json"), `${manifest}\n`),
]);
assert.equal(await readFile(sdkEntry, "utf8"), source, "Installed Cosmos was modified");
console.log(`Generated dtxwiki-cosmos-native ${COSMOS_VERSION}-native.7; original native force laws retained`);

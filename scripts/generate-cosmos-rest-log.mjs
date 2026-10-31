import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { transform, version as esbuildVersion } from "esbuild";

import { COSMOS_VERSION, COSMOS_SOURCE_SHA256, patchCosmosRestLog, restLogTension, sha256 } from "./cosmos-rest-log-patch.mjs";
import { patchCosmosGpuSimulation, COSMOS_GPU_SIMULATION_PATCH_HUNKS } from "./cosmos-gpu-simulation-patch.mjs";
import { patchCosmosCollisionRadii, COSMOS_COLLISION_RADII_PATCH_HUNKS } from "./cosmos-collision-radii-patch.mjs";
import { patchCosmosPointPicker, COSMOS_POINT_PICKER_PATCH_HUNKS } from "./cosmos-point-picker-patch.mjs";
// Typecheck/test hooks may regenerate while Vite is open. Identical outputs
// must not trigger HMR or replace a running fixture's document/renderer.
import { writeFileIfChanged as writeFile } from "./write-if-changed.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const sdkEntry = require.resolve("@cosmos.gl/graph");
const sdkRoot = path.resolve(path.dirname(sdkEntry), "..");
const source = await readFile(sdkEntry, "utf8");
const { version } = JSON.parse(await readFile(path.join(sdkRoot, "package.json"), "utf8"));
const modified = patchCosmosGpuSimulation(patchCosmosRestLog(source, version), version);
const license = await readFile(path.join(sdkRoot, "LICENCE"), "utf8");
const fa2Root = path.dirname(require.resolve("graphology-layout-forceatlas2"));
const fa2License = await readFile(path.join(fa2Root, "LICENSE.txt"), "utf8");
const fa2Package = JSON.parse(await readFile(path.join(fa2Root, "package.json"), "utf8"));
assert.equal(fa2Package.version, "0.10.1", "Review the full FA2 driver before upgrading its upstream reference");
assert.equal(sha256(fa2License), "9d396b4882c329077f32861c0d6822dcee48f2d0ff6196d8459af70844196275", "ForceAtlas2 upstream license differs from the reviewed source");
const generated = path.join(root, "frontend/src/features/graph/workspace/generated-cosmos-rest-log");
const notices = path.join(root, "frontend/public/third-party/cosmos-rest-log");

// A distinct artifact: never overwrite node_modules or reuse the now-stale
// upstream TS map. This map truthfully embeds the modified bundle as source.
const result = await transform(modified.replace(/\/\/# sourceMappingURL=[^\n]*\n?$/u, ""), {
  format: "esm", target: "es2022", sourcemap: "external", sourcesContent: true,
  sourcefile: `cosmos-rest-log-${COSMOS_VERSION}.source.js`,
  banner: "// dtxWiki rest-log and optional GPU simulation seam for Cosmos 3.4.2 (MIT). See third-party/cosmos-rest-log/LICENSE.txt.",
  legalComments: "inline",
});
const code = `${result.code}\n//# sourceMappingURL=index.js.map\n`;
const manifest = JSON.stringify({
  artifact: "dtxwiki-cosmos-rest-log", artifactVersion: `${COSMOS_VERSION}-rest-log.2`,
  upstream: { package: "@cosmos.gl/graph", version: COSMOS_VERSION, license: "MIT", sha256: COSMOS_SOURCE_SHA256 },
  modification: "Rest-scaled logarithmic native link tension plus an optional, exclusive GPU simulation host seam. Without attachment native behavior is retained. The seam itself is not ForceAtlas2.",
  patchSha256: sha256(restLogTension), modifiedSourceSha256: sha256(modified),
  gpuSeamSha256: sha256(JSON.stringify(COSMOS_GPU_SIMULATION_PATCH_HUNKS)),
  generatedModuleSha256: sha256(code), generatedMapSha256: sha256(result.map),
  compiler: { package: "esbuild", version: esbuildVersion, target: "es2022" },
  licenseSha256: sha256(license),
  forceAtlas2Reference: { package: "graphology-layout-forceatlas2", version: fa2Package.version,
    license: "MIT", licenseSha256: sha256(fa2License), notice: "FORCEATLAS2-LICENSE.txt" },
}, null, 2);
await Promise.all([mkdir(generated, { recursive: true }), mkdir(notices, { recursive: true })]);
await Promise.all([
  writeFile(path.join(generated, "index.js"), code),
  writeFile(path.join(generated, "index.js.map"), result.map),
  writeFile(path.join(generated, "index.d.ts"), 'export * from "@cosmos.gl/graph";\nimport { Graph as UpstreamGraph } from "@cosmos.gl/graph";\nimport type { CosmosGpuSimulation, CosmosGpuSimulationFrame } from "../cosmosGpuTypes";\nexport declare class Graph extends UpstreamGraph {\n  attachGpuSimulation(simulation: CosmosGpuSimulation | null): void;\n  getGpuSimulationFrame(): CosmosGpuSimulationFrame | null;\n}\n'),
  writeFile(path.join(notices, "LICENSE.txt"), license),
  writeFile(path.join(notices, "FORCEATLAS2-LICENSE.txt"), fa2License),
  writeFile(path.join(notices, "provenance.json"), `${manifest}\n`),
]);

// Production native artifact: radius and nearest-point picking extensions
// activate only when explicitly attached. Default/null keeps upstream behavior.
// The separately generated rest-log diagnostic above stays unchanged.
const nativeModified = patchCosmosPointPicker(patchCosmosCollisionRadii(patchCosmosGpuSimulation(source, version), version), version);
const nativeGenerated = path.join(root, "frontend/src/features/graph/workspace/generated-cosmos-native");
const nativeNotices = path.join(root, "frontend/public/third-party/cosmos-native");
const nativeResult = await transform(nativeModified.replace(/\/\/# sourceMappingURL=[^\n]*\n?$/u, ""), {
  format: "esm", target: "es2022", sourcemap: "external", sourcesContent: true,
  sourcefile: `cosmos-native-${COSMOS_VERSION}.source.js`,
  banner: "// dtxWiki optional GPU simulation, glyph collision radii and point picking seams for Cosmos 3.4.2 (MIT). Native forces unchanged. See third-party/cosmos-native/LICENSE.txt.",
  legalComments: "inline",
});
const nativeCode = `${nativeResult.code}\n//# sourceMappingURL=index.js.map\n`;
const nativeManifest = JSON.stringify({
  artifact: "dtxwiki-cosmos-native", artifactVersion: `${COSMOS_VERSION}-native.1`,
  upstream: { package: "@cosmos.gl/graph", version: COSMOS_VERSION, license: "MIT", sha256: COSMOS_SOURCE_SHA256 },
  modification: "Optional exclusive GPU simulation host seam, per-point native collision radius column, point-picker host seam and GPU-only tracked-position framebuffer access. Default/null preserves upstream behavior; force shaders, coefficients and integration are unchanged. No rest-log or ForceAtlas2 force law is applied.",
  modifiedSourceSha256: sha256(nativeModified),
  gpuSeamSha256: sha256(JSON.stringify(COSMOS_GPU_SIMULATION_PATCH_HUNKS)),
  collisionRadiiSeamSha256: sha256(JSON.stringify(COSMOS_COLLISION_RADII_PATCH_HUNKS)),
  pointPickerSeamSha256: sha256(JSON.stringify(COSMOS_POINT_PICKER_PATCH_HUNKS)),
  generatedModuleSha256: sha256(nativeCode), generatedMapSha256: sha256(nativeResult.map),
  compiler: { package: "esbuild", version: esbuildVersion, target: "es2022" },
  licenseSha256: sha256(license),
}, null, 2);
await Promise.all([mkdir(nativeGenerated, { recursive: true }), mkdir(nativeNotices, { recursive: true })]);
await Promise.all([
  writeFile(path.join(nativeGenerated, "index.js"), nativeCode),
  writeFile(path.join(nativeGenerated, "index.js.map"), nativeResult.map),
  writeFile(path.join(nativeGenerated, "index.d.ts"), 'export * from "@cosmos.gl/graph";\nimport { Graph as UpstreamGraph } from "@cosmos.gl/graph";\nimport type { CosmosGpuSimulation, CosmosGpuSimulationFrame } from "../cosmosGpuTypes";\nimport type { CosmosPointPicker } from "../cosmosPointPickingTypes";\nexport declare class Graph extends UpstreamGraph {\n  attachGpuSimulation(simulation: CosmosGpuSimulation | null): void;\n  getGpuSimulationFrame(): CosmosGpuSimulationFrame | null;\n  setPointCollisionRadii(radii: Float32Array | null): void;\n  attachPointPicker(picker: CosmosPointPicker | null): void;\n  getTrackedPointPositionsFrame(): { gl: WebGL2RenderingContext; framebuffer: WebGLFramebuffer; width: number; height: number; indices: readonly number[] } | null;\n}\n'),
  writeFile(path.join(nativeNotices, "LICENSE.txt"), license),
  writeFile(path.join(nativeNotices, "provenance.json"), `${nativeManifest}\n`),
]);
assert.equal(await readFile(sdkEntry, "utf8"), source, "Installed Cosmos was modified");
console.log(`Generated dtxwiki-cosmos-rest-log ${COSMOS_VERSION}-rest-log.2; installed SDK unchanged`);
console.log(`Generated dtxwiki-cosmos-native ${COSMOS_VERSION}-native.1; original native force laws retained`);

import assert from "node:assert/strict";
import { createHash } from "node:crypto";

export const COSMOS_VERSION = "3.4.2";
export const COSMOS_SOURCE_SHA256 = "4ad56e0d6ddb6ad483baee7464b6ab0dd88b31c6af7f049408ad50574b8f1ff2";
export const sha256 = value => createHash("sha256").update(value).digest("hex");

const linearTension = "l = (l - randomMinLinkDist) / l;";
// Rest-scaled logarithmic tension, NOT the complete ForceAtlas2 algorithm.
// Keep compression, degree bias, occurrence strength and integration intact.
// The small-u branch only conditions log1p in GLSL Float32 arithmetic.
export const restLogTension = `float extension = l - randomMinLinkDist;
        if (extension > 0.0 && randomMinLinkDist > 0.0) {
          float u = extension / randomMinLinkDist;
          float logRatio = u < 0.001 ? u * (1.0 - u * 0.5 + u * u / 3.0) : log(1.0 + u);
          extension = randomMinLinkDist * logRatio;
        }
        l = extension / l;`;

export function patchCosmosRestLog(source, version) {
  assert.equal(version, COSMOS_VERSION, "Review the extension before upgrading Cosmos");
  assert.equal(sha256(source), COSMOS_SOURCE_SHA256, "Cosmos source differs from the reviewed package");
  assert.equal(source.split(linearTension).length, 2, "Expected exactly one link shader");
  const modified = source.replace(linearTension, restLogTension);
  assert.equal(modified.replace(restLogTension, linearTension), source, "Unexpected extra SDK change");
  return modified;
}

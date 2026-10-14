import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import { COSMOS_VERSION, COSMOS_SOURCE_SHA256, patchCosmosRestLog, restLogTension, sha256 } from "./cosmos-rest-log-patch.mjs";

const require = createRequire(import.meta.url);
const original = await readFile(require.resolve("@cosmos.gl/graph"), "utf8");

test("one reversible GPU-law patch retains the reviewed SDK", () => {
  assert.equal(sha256(original), COSMOS_SOURCE_SHA256);
  const modified = patchCosmosRestLog(original, COSMOS_VERSION);
  assert.equal(modified.replace(restLogTension, "l = (l - randomMinLinkDist) / l;"), original);
  assert.equal(sha256(restLogTension), "844453bdacd4212efda717b3e798045d996e80f71c238d065ee1653585ff3a68");
});

test("an upstream version change fails closed", () => {
  assert.throws(() => patchCosmosRestLog(original, "3.5.1"), /Review the extension/);
});

test("changed or already-patched SDK bytes fail closed", () => {
  assert.throws(() => patchCosmosRestLog(`${original}\n`, COSMOS_VERSION), /source differs/);
  assert.throws(() => patchCosmosRestLog(patchCosmosRestLog(original, COSMOS_VERSION), COSMOS_VERSION), /source differs/);
});

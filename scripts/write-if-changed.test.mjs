import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat, utimes, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { writeFileIfChanged } from "./write-if-changed.mjs";

async function artifact(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "dtxwiki-artifact-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return { directory, filename: path.join(directory, "generated.js") };
}

test("new generated artifacts are written", async t => {
  const { filename } = await artifact(t);
  assert.equal(await writeFileIfChanged(filename, "export const value = 1;\n"), true);
  assert.equal(await readFile(filename, "utf8"), "export const value = 1;\n");
});

test("identical regeneration preserves both bytes and mtime without clock sleeps", async t => {
  const { filename } = await artifact(t);
  const contents = "// 한글 artifact\nexport const value = 1;\n";
  await writeFile(filename, contents);
  await utimes(filename, 1, 1);
  const before = await stat(filename, { bigint: true });
  assert.equal(await writeFileIfChanged(filename, contents), false);
  assert.equal(await writeFileIfChanged(filename, Buffer.from(contents)), false);
  const after = await stat(filename, { bigint: true });
  assert.equal(after.mtimeNs, before.mtimeNs);
  assert.equal(after.ino, before.ino);
  assert.equal(await readFile(filename, "utf8"), contents);
});

test("changed source updates the existing artifact", async t => {
  const { filename } = await artifact(t);
  await writeFile(filename, "old");
  await utimes(filename, 1, 1);
  assert.equal(await writeFileIfChanged(filename, "new"), true);
  assert.equal(await readFile(filename, "utf8"), "new");
  assert.notEqual((await stat(filename)).mtimeMs, 1000);
});

test("byte equality never treats invalid UTF-8 as identical replacement text", async t => {
  const { filename } = await artifact(t);
  await writeFile(filename, Buffer.from([255]));
  assert.equal(await writeFileIfChanged(filename, "\ufffd"), true);
  assert.deepEqual(await readFile(filename), Buffer.from("\ufffd"));
});

test("read errors other than missing files are not swallowed", async t => {
  const { directory } = await artifact(t);
  await assert.rejects(writeFileIfChanged(directory, "not a file"), { code: "EISDIR" });
  assert.equal((await stat(directory)).isDirectory(), true);
});

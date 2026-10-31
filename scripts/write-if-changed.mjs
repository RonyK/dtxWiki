import { readFile, writeFile } from "node:fs/promises";

/** Preserve artifact identity/mtime when regeneration produces identical bytes. */
export async function writeFileIfChanged(filename, contents) {
  const bytes = Buffer.isBuffer(contents) ? contents : Buffer.from(contents, "utf8");
  try {
    if ((await readFile(filename)).equals(bytes)) return false;
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  await writeFile(filename, bytes);
  return true;
}

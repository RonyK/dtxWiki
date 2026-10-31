import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";
import type {} from "./native-point-picking-restore-entry";

let server: ViteDevServer, origin: string;
test.beforeAll(async () => {
  server = await createServer({ root: path.resolve("frontend"), configFile: false, logLevel: "error",
    server: { host: "127.0.0.1", port: 0, fs: { allow: [path.resolve(".")] } } });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("Native restore fixture did not bind");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

async function open(page: Page) {
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/native-point-picking-restore.html")}`);
  await page.waitForFunction(() => !!window.__nativeRestore);
  return Promise.all([0, 1, 2].map(index => page.evaluate(index => window.__nativeRestore.screenOf(index), index)));
}
const pick = (page: Page, [x, y]: readonly [number, number]) => page.evaluate(([x, y]) => window.__nativeRestore.pick(x, y), [x, y]);

test("PC native picker rebuilds, with float targets, when the same context object is lost and restored with a query in flight", async ({ page }) => {
  const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
  const nodes = await open(page);
  expect(await pick(page, nodes[0])).toBe(0);
  // A query is in flight when the context goes, and nothing calls the picker until the restore is over.
  expect(await page.evaluate(([x, y]) => window.__nativeRestore.request(x, y), nodes[0])).toBe(true);
  expect(await page.evaluate(() => window.__nativeRestore.lose())).toBe(true);
  expect(await page.evaluate(() => window.__nativeRestore.restore())).toBe(true);
  // The premise: the context object lives on, but nothing made before the loss is valid any more.
  expect(await page.evaluate(() => window.__nativeRestore.alive())).toEqual({ programs: [false], framebuffers: [false] });
  // The loss event alone retired the query's fence and buffer.
  expect(await page.evaluate(([x, y]) => window.__nativeRestore.take(x, y), nodes[0]))
    .toEqual({ outcome: "pending-or-obsolete", index: null, hasPending: false });
  // Extension state went with the lost context. The host gets its textures back but does not ask for EXT_color_buffer_float
  // again: the picker must, or its float framebuffer is incomplete.
  await page.evaluate(() => window.__nativeRestore.fresh(false));
  for (const [index, node] of nodes.entries()) expect(await pick(page, node)).toBe(index);
  expect(await pick(page, [20, 20])).toBeNull();
  expect(await page.evaluate(() => window.__nativeRestore.errors())).toEqual([]);
  expect(await page.evaluate(() => window.__nativeRestore.alive())).toEqual({ programs: [false, true], framebuffers: [false, true] });
  // The async path serves again too; an unsignaled fence keeps its query, so polling only waits longer on a slow machine.
  expect(await page.evaluate(([x, y]) => window.__nativeRestore.request(x, y), nodes[1])).toBe(true);
  await expect.poll(() => page.evaluate(([x, y]) => window.__nativeRestore.take(x, y), nodes[1]), { timeout: 20_000 })
    .toEqual({ outcome: "hit", index: 1, hasPending: false });
  // The dead objects were dropped rather than deleted, which would raise INVALID_OPERATION.
  expect(await page.evaluate(() => window.__nativeRestore.drain())).toEqual([]);
  expect(await page.evaluate(() => window.__nativeRestore.errors())).toEqual([]); expect(errors).toEqual([]);
});

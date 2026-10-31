import { expect, test } from "@playwright/test";
import { createServer, type ViteDevServer } from "vite";

import { fixtureServerConfig } from "./vite-fixture";

import type {} from "../../frontend/src/features/graph/workspace/tests/browserSmoke";

let server: ViteDevServer;
let origin: string;
test.beforeAll(async () => {
  server = await createServer({
    ...fixtureServerConfig,
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("Map fixture server did not bind TCP");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

test("2k Cosmos matching, no-match and cleared search preserve GPU coordinates and camera", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.setViewportSize({ width: 1100, height: 760 });
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=reduce&topology=links&search=stability`);
  await page.waitForFunction(() => window.__workspaceMapSmoke?.status !== "loading");
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  const canvas = page.locator("#stage canvas");
  await expect(canvas).toHaveCount(1);
  const before = await page.evaluate(async () => {
    window.__workspaceMapSearch!.zoom();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return window.__workspaceMapSearch!.snapshot();
  });
  const { matchingPointCount: initialMatches, ...initialGeometry } = before;
  expect(initialMatches).toBeNull();
  expect(before.positions).toHaveLength(4000);
  expect(before.nodeCount).toBe(2000);
  expect(before.edgeCount).toBe(4700);
  expect(before.viewport).not.toBeNull();
  expect(before.glError).toBe(0);
  const input = page.getByRole("searchbox", { name: "Find in visible map" });
  for (const text of ["node-1", "no matching document", ""]) {
    await input.fill(text);
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const after = await page.evaluate(() => window.__workspaceMapSearch!.snapshot());
    const { matchingPointCount, ...geometry } = after;
    expect(geometry).toEqual(initialGeometry);
    expect(matchingPointCount).toBe(text === "node-1" ? 1111 : text ? 0 : null);
    await expect(canvas).toHaveCount(1);
    if (text) await page.screenshot({ path: testInfo.outputPath(
      text === "node-1" ? "cosmos-2k-search-matching.png" : "cosmos-2k-search-no-match.png",
    ) });
  }
  await page.screenshot({ path: testInfo.outputPath("cosmos-2k-search-stable.png") });
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.glError)).toBe(0);
  expect(errors).toEqual([]);
  await testInfo.attach("stable-map-search", { contentType: "application/json", body: JSON.stringify({
    nodes: before.nodeCount, relations: before.edgeCount, coordinates: before.positions.length,
    viewport: before.viewport, geometryUploads: before.geometryUploads,
    workerRequests: before.workerRequests, errors,
    scope: "Synthetic actual Cosmos/WebGL2 fixture; not application API acceptance or GPU performance",
  }) });
});

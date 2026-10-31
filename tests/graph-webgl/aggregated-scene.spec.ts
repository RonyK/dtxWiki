import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";
import type {} from "./aggregated-scene-entry";

// An aggregated scene through the real adapter, native picker and native renderer. Four link clusters of 17,499 / 492 /
// 12 / 2 documents next to 2,000 ordinary documents (2,004 nodes, above the 2,000-node boundary where the picker is
// attached), laid out by hand and kept still. Headless SwiftShader: functional only, not GPU latency or a target-device claim.
let server: ViteDevServer, origin: string;
test.beforeAll(async () => {
  server = await createServer({ root: path.resolve("frontend"), configFile: false, logLevel: "error",
    server: { host: "127.0.0.1", port: 0, fs: { allow: [path.resolve(".")] } } });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("Aggregated scene fixture did not bind");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

/** Wait for the hover the page reports; a miss says which pointer position it was and what hover it saw instead. */
async function hoveredIs(page: Page, id: string | null, what: string) {
  try {
    await page.waitForFunction(expected => window.__aggregatedScene.snapshot().hovered === expected, id, { timeout: 5_000, polling: 50 });
  } catch {
    const seen = await page.evaluate(() => window.__aggregatedScene.snapshot());
    throw new Error(`${what}: expected hover ${id}, the page shows ${seen.hovered}`);
  }
}

test("PC native picker finds a cluster at its drawn size, past the 12 px target, and a click there selects its id", async ({ page }) => {
  const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/aggregated-scene.html")}`);
  await page.waitForFunction(() => !!window.__aggregatedScene, undefined, { timeout: 60_000 });
  const hub = "cluster-0";
  await page.evaluate(() => window.__aggregatedScene.zoomOn("cluster-0", 4));

  // Zoomed in, the emphasised glyph is drawn at the camera's point-size scale: its body reaches well past the 12 px target.
  const camera = await page.evaluate(() => window.__aggregatedScene.snapshot());
  const { draw } = await page.evaluate(() => window.__aggregatedScene.hubSizes());
  const radius = draw * camera.pointSizeScale / 2;
  expect(radius).toBeGreaterThan(16);
  const centre = await page.evaluate(id => window.__aggregatedScene.screen(id), hub);
  const inside = Math.floor(radius) - 4, outside = Math.ceil(radius) + 8;

  await page.mouse.move(centre[0] - inside, centre[1]);
  await hoveredIs(page, hub, `${inside} px from the centre: outside the 12 px target, inside the drawn body`);
  await page.mouse.click(centre[0] - inside, centre[1]);
  await page.waitForFunction(id => window.__aggregatedScene.snapshot().selected === id, hub);

  // Past the body and the target the picker finds nothing, and a click there selects nothing.
  await page.mouse.move(centre[0] - outside, centre[1]);
  await hoveredIs(page, null, `${outside} px from the centre: past the body and the target`);
  await page.mouse.click(centre[0] - outside, centre[1]);
  await page.waitForFunction(() => window.__aggregatedScene.snapshot().selected === null);

  const done = await page.evaluate(() => window.__aggregatedScene.snapshot());
  expect(done.events.filter(event => event.kind === "select").map(event => event.id)).toEqual([hub, null]);
  expect(done.glError).toBe(0); expect(done.runtimeError).toBeNull(); expect(errors).toEqual([]);
});

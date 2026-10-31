import { expect, test } from "@playwright/test";
import { readFile, writeFile } from "node:fs/promises";
import { createServer, type ViteDevServer } from "vite";

import { evidenceScreenshot } from "../e2e/evidence";
import { fixtureServerConfig } from "./vite-fixture";

import type {} from "../../frontend/src/features/graph/workspace/tests/browserSmoke";

let server: ViteDevServer;
let origin: string;

test.beforeAll(async () => {
  // Use the deployed application's actual CSP, not a dev-only blob exception.
  const source = await readFile("backend/api/middleware/security_headers.py", "utf8");
  const block = source.match(/"content-security-policy": \(([\s\S]*?)\n {4}\),/);
  if (!block) throw new Error("Application CSP was not found");
  const csp = [...block[1].matchAll(/"([^"]*)"/g)].map(match => match[1]).join("");
  expect(csp).toContain("worker-src 'self' blob:");
  expect(csp).toContain("script-src 'self';");
  expect(csp).not.toContain("unsafe-eval");
  server = await createServer({
    ...fixtureServerConfig,
    server: { host: "127.0.0.1", port: 0, headers: { "Content-Security-Policy": csp } },
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("CPU layout fixture did not bind TCP");
  origin = `http://127.0.0.1:${address.port}`;
});

test.afterAll(async () => { await server?.close(); });

test("official CPU layout preserves an actual pointer drag pin through a fresh Worker resume", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => Number(document.getElementById("stage")?.dataset.mapLayoutTicks) >= 3);
  await expect(page.locator("#stage")).toHaveAttribute("data-map-layout-phase", "contact", { timeout: 45_000 });
  const contactZoom = await page.locator("#stage").getAttribute("data-map-contact-reference-zoom");
  expect(Number(contactZoom)).toBeGreaterThan(0);
  await page.evaluate(() => { window.__workspaceMapInteraction!.stop(); window.__workspaceMapInteraction!.fit(); });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const target = await page.evaluate(() => window.__workspaceMapInteraction!.dragTarget());
  expect(target.clearance).toBeGreaterThan(6);
  const start = target.screen.map(Math.round);
  await page.mouse.move(start[0], start[1]);
  await page.waitForFunction(id => window.__workspaceMapSmoke?.hoveredId === id, target.id);
  await page.mouse.down();
  await page.waitForFunction(index => window.__workspaceMapInteraction!.snapshot().draggingIndex === index, target.index);
  await expect(page.locator("#stage")).toHaveAttribute("data-map-layout-running", "false");
  const destination = [start[0] + 48, start[1] + 24];
  await page.mouse.move(destination[0], destination[1], { steps: 4 });
  await page.mouse.up();
  await page.waitForFunction(index => window.__workspaceMapInteraction!.snapshot().pinned.includes(index), target.index);
  const pinned = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  const point = await page.evaluate(index => window.__workspaceMapInteraction!.point(index), target.index);
  expect(point[0]).toBeCloseTo(destination[0], 0);
  expect(point[1]).toBeCloseTo(destination[1], 0);
  expect(pinned.dragPins).toEqual([{ id: target.id,
    position: pinned.canonicalPositions.slice(target.index * 2, target.index * 2 + 2) }]);
  const ticks = await page.locator("#stage").getAttribute("data-map-layout-ticks");
  await page.evaluate(() => window.__workspaceMapInteraction!.resume());
  await page.waitForFunction(previous => Number(document.getElementById("stage")?.dataset.mapLayoutTicks) > Number(previous) + 2, ticks);
  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const resumed = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(resumed.canonicalPositions.slice(target.index * 2, target.index * 2 + 2))
    .toEqual(pinned.canonicalPositions.slice(target.index * 2, target.index * 2 + 2));
  expect(resumed.canonicalPositions).not.toEqual(pinned.canonicalPositions);
  expect(resumed.running).toBe(false);
  await expect(page.locator("#stage")).toHaveAttribute("data-map-contact-reference-zoom", contactZoom!);
  expect(resumed.glError).toBe(0);
  await evidenceScreenshot(page, testInfo, "cpu-2k-native-drag-pin.png");
});

// The CSP and Worker protocol are viewport-independent; desktop pointer drag is covered above.
test("official CPU Worker draws 2K with GPU physics off under production CSP at mobile viewport", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => Boolean(window.__workspaceMapSmoke && window.__workspaceMapSmoke.status !== "loading"));
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  await expect.poll(() => page.evaluate(() => ({ physics: document.getElementById("stage")?.dataset.mapPhysics,
    error: window.__workspaceMapSmoke?.runtimeError ?? null }))).toEqual({ physics: "force-atlas2-worker", error: null });
  await page.waitForFunction(() => Number(document.getElementById("stage")?.dataset.mapLayoutTicks) >= 3);
  await expect(page.locator("#stage")).toHaveAttribute("data-map-layout-phase", "contact", { timeout: 45_000 });
  const contactTicks = await page.locator("#stage").getAttribute("data-map-layout-ticks");
  // These later publications require the real module collision Worker to
  // return, and its projected XY to feed the official next matrix under CSP.
  await page.waitForFunction(ticks => Number(document.getElementById("stage")?.dataset.mapLayoutTicks) > Number(ticks) + 2, contactTicks);
  const contactZoom = await page.locator("#stage").getAttribute("data-map-contact-reference-zoom");
  expect(Number(contactZoom)).toBeGreaterThan(0);
  const live = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(live.running, "the native GPU force pipeline must stay off").toBe(false);
  expect(live.canonicalPositions).toHaveLength(4000);
  expect(live.canonicalPositions.every(Number.isFinite)).toBe(true);
  expect(live.glError).toBe(0);
  await expect(page.locator("#stage")).toHaveAttribute("data-map-layout-running", "true");

  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const paused = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  await page.waitForTimeout(250); // Let an in-flight old Worker result arrive.
  const held = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(held.canonicalPositions).toEqual(paused.canonicalPositions);
  expect(held.geometryUploads).toBe(paused.geometryUploads);
  await expect(page.locator("#stage")).toHaveAttribute("data-map-layout-running", "false");

  // Explicit user pin; this contract isolates fixed-state ownership, not
  // overlap quality. The actual pointer drag is covered separately.
  await page.evaluate(() => window.__workspaceMapInteraction!.isolate());
  const pinned = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(pinned.pinned).toContain(0);
  const beforeResumeTicks = await page.locator("#stage").getAttribute("data-map-layout-ticks");
  await page.evaluate(() => window.__workspaceMapInteraction!.resume());
  await page.waitForFunction(ticks => Number(document.getElementById("stage")?.dataset.mapLayoutTicks) > Number(ticks) + 2, beforeResumeTicks);
  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const resumed = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(resumed.canonicalPositions.slice(0, 2)).toEqual(pinned.canonicalPositions.slice(0, 2));
  expect(resumed.canonicalPositions.slice(2)).not.toEqual(pinned.canonicalPositions.slice(2));
  expect(resumed.running).toBe(false);
  expect(resumed.pinned).toContain(0);
  await expect(page.locator("#stage")).toHaveAttribute("data-map-contact-reference-zoom", contactZoom!);

  await page.evaluate(() => { window.__workspaceMapInteraction!.reduce(true); window.__workspaceMapInteraction!.resume(); });
  const reduced = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  await page.waitForTimeout(250);
  expect((await page.evaluate(() => window.__workspaceMapInteraction!.snapshot())).canonicalPositions).toEqual(reduced.canonicalPositions);
  await expect(page.locator("#stage")).toHaveAttribute("data-map-layout-running", "false");
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.runtimeError ?? null)).toBeNull();
  expect(errors).toEqual([]);
  await evidenceScreenshot(page, testInfo, "cpu-2k-paused.png");
  await writeFile(testInfo.outputPath("cpu-2k-contract.json"), JSON.stringify({ live, paused, pinned, resumed, reduced, errors }, null, 2));
});

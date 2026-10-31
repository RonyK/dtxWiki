import { expect, test } from "@playwright/test";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";
import type {} from "./native-point-picking-hover-entry";

let server: ViteDevServer, origin: string;
test.beforeAll(async () => {
  server = await createServer({ root: path.resolve("frontend"), configFile: false, logLevel: "error",
    server: { host: "127.0.0.1", port: 0, fs: { allow: [path.resolve(".")] } } });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("Native hover fixture did not bind");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

const open = (query: string) => `${origin}/@fs/${path.resolve("tests/graph-webgl/native-point-picking-hover.html")}?${query}`;

test("PC native hover follows a pointer swept in small steps instead of freezing, and a left target, jump or pan still discards", async ({ page }) => {
  const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
  await page.goto(open("dpr=2"));
  await page.waitForFunction(() => !!window.__nativeHover);
  const grid = await page.evaluate(() => window.__nativeHover.loadGrid(16));
  // At spacing 16 the midpoint between neighbours is 8 px from both, inside the 12 px target: along this row hover is never "no point".
  const nearest = (x: number) => grid.rowStart + grid.columnX.reduce((best, value, column) => Math.abs(value - x) < Math.abs(grid.columnX[best] - x) ? column : best, 0);
  const first = grid.columnX[0] - 7.5, edge = grid.columnX.at(-1)!;
  await page.mouse.move(first, grid.rowY);
  // A pointer at rest is served exactly, before and after the fix; the sweep below is what used to freeze.
  await page.waitForFunction(index => window.__nativeHover.snapshot().hovered === index, nearest(first));
  await page.evaluate(() => window.__nativeHover.reset());
  // Across the row and 30 px past its last node, 4 px per animation frame: the pointer moves in every frame, as in a real drag
  // of the mouse, yet never leaves the 12 px target between two frames.
  await page.evaluate(([from, to, y]) => window.__nativeHover.sweep(from, to, y, 4), [first + 4, edge + 30, grid.rowY]);
  const swept = await page.evaluate(() => window.__nativeHover.snapshot());
  // The frozen picker delivers nothing while the pointer moves; the following one shows nearly every node of the row.
  expect(new Set(swept.overs).size).toBeGreaterThanOrEqual(grid.columns - 10);
  // Whatever was served, its node was inside the 12 px target of the pointer it was served for.
  for (const take of swept.takes) {
    if (take.index === null) continue;
    expect(Math.hypot(grid.columnX[take.index - grid.rowStart] - take.pointer![0], grid.rowY - (400 - take.pointer![1]))).toBeLessThanOrEqual(12.001);
  }
  // Beyond the last node every answer is "no point". One may be applied only for the very pointer it was asked at.
  const movedSince = (take: (typeof swept.takes)[number]) => take.pointer![0] !== take.asked![0] || take.pointer![1] !== take.asked![1];
  expect(swept.takes.filter(take => take.index === null && movedSince(take))).toHaveLength(0);
  await page.waitForFunction(() => window.__nativeHover.snapshot().hovered === null);

  // An answer a frame old: served for a small move, discarded for a left target, a jump and a pan.
  await page.evaluate(() => window.__nativeHover.loadIsolated({ zoom: 1, center: [2048, 2048] }));
  const outcomes: Record<string, unknown> = {};
  for (const change of ["near", "left", "far", "pan"] as const) outcomes[change] = await page.evaluate(change => window.__nativeHover.aged(change), change);
  expect(outcomes).toEqual({
    near: { queued: true, pendingAfter: false, outcome: "index-0" },
    left: { queued: true, pendingAfter: false, outcome: "obsolete" },
    far: { queued: true, pendingAfter: false, outcome: "obsolete" },
    pan: { queued: true, pendingAfter: false, outcome: "obsolete" },
  });
  expect((await page.evaluate(() => window.__nativeHover.snapshot())).glError).toBe(0); expect(errors).toEqual([]);
});

// The CPU test the picker re-applies must say what the shader says. The GPU is the oracle: sweep the pointer
// outward from three isolated bodies and require the same hit/miss on both sides of every boundary. Two canvases
// that are not 1:1: DPR 1.5 with point sizes scaled by zoom on a tall one, DPR 2 with fixed point sizes on a wide one.
test("PC native CPU target test agrees with the GPU query at DPR 1.5 and 2 on non-square canvases, scaled and fixed point size", async ({ page }) => {
  for (const { dpr, scale, size } of [{ dpr: 1.5, scale: 1, size: "333x777" }, { dpr: 2, scale: 0, size: "900x300" }]) {
    await page.goto(open(`dpr=${dpr}&scale=${scale}&size=${size}`));
    await page.waitForFunction(() => !!window.__nativeHover);
    for (const zoom of [.5, 2.3]) {
      const where = `DPR ${dpr}, ${size}, zoom ${zoom}`;
      await page.evaluate(options => window.__nativeHover.loadIsolated(options), { zoom, center: [2048 + 37, 2048 - 21] as [number, number] });
      // 0.07 px off the 1 px lattice keeps every sample clear of a boundary at float precision.
      const samples = [];
      for (const node of [0, 1, 2]) for (const angle of [0, 45, 112, 270]) for (let radius = .07; radius < 64; radius += 1) {
        const theta = angle * Math.PI / 180;
        samples.push({ node, offset: [radius * Math.cos(theta), radius * Math.sin(theta)] as [number, number] });
      }
      const rows = await page.evaluate(samples => window.__nativeHover.agreement(samples), samples);
      expect(rows.filter(row => (row.gpu === row.node) !== row.cpu), where).toEqual([]);
      expect(rows.every(row => row.gpu === null || row.gpu === row.node), where).toBe(true);
      expect(Math.max(...rows.map(row => Math.hypot(row.projectedX - row.centerX, row.projectedY - row.centerY))), where).toBeLessThan(1e-3);
      // A meaningful sweep: every body is hit near its centre and missed far away.
      for (const node of [0, 1, 2]) {
        const own = rows.filter(row => row.node === node);
        expect(own.some(row => row.cpu) && own.some(row => !row.cpu), `${where}, node ${node}`).toBe(true);
      }
    }
    expect((await page.evaluate(() => window.__nativeHover.snapshot())).glError).toBe(0);
  }
});

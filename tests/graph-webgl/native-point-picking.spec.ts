import { expect, test } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";
import type {} from "./native-point-picking-entry";

let server: ViteDevServer, origin: string;
test.beforeAll(async () => {
  server = await createServer({ root: path.resolve("frontend"), configFile: false, logLevel: "error",
    server: { host: "127.0.0.1", port: 0, fs: { allow: [path.resolve(".")] } } });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("Native picking fixture did not bind");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

for (const dpr of [1, 2]) test(`PC native nearest picker selects an early Context under a later Library at DPR ${dpr}`, async ({ page }, info) => {
  const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/native-point-picking.html")}?dpr=${dpr}`);
  await page.waitForFunction(() => !!window.__nativePointPicking);
  // Independently reproduce the unchanged native default's paint-order capture.
  const legacy = await page.evaluate(() => window.__nativePointPicking.configure({ noProvider: true }));
  await page.mouse.click(legacy.context[0], legacy.context[1]);
  expect(await page.evaluate(() => window.__nativePointPicking.snapshot().selected)).toBe(legacy.libraryIndex);
  await page.mouse.move(20, 20);
  await page.waitForFunction(() => window.__nativePointPicking.snapshot().hovered === null);
  const checks = [];
  for (const zoom of [.5, 1, 3]) {
    const target = await page.evaluate(zoom => window.__nativePointPicking.configure({ zoom }), zoom);
    expect(target.context[0]).toBeCloseTo(300, 3); expect(target.context[1]).toBeCloseTo(200, 3);
    await page.mouse.move(target.context[0], target.context[1]);
    await page.waitForFunction(index => window.__nativePointPicking.snapshot().hovered === index, target.contextIndex);
    await page.mouse.click(target.context[0], target.context[1]);
    expect(await page.evaluate(() => window.__nativePointPicking.snapshot().selected)).toBe(target.contextIndex);
    await page.mouse.down();
    expect(await page.evaluate(() => window.__nativePointPicking.snapshot().dragging)).toBe(target.contextIndex);
    await page.mouse.up();
    checks.push({ zoom, ...await page.evaluate(() => window.__nativePointPicking.snapshot()) });
    // Move to a real canvas background so the next configure cannot inherit a hover.
    await page.mouse.move(20, 20);
    await page.waitForFunction(() => window.__nativePointPicking.snapshot().hovered === null);
  }
  expect(checks.every(check => check.readbacks.length > 0 && check.readbacks.every(read => read.width === 1 && read.height === 1
    && (read.floats === null || read.floats === 4)) && check.glError === 0)).toBe(true);
  expect(checks.every(check => check.queries.some(query => query.method === "take" && query.result !== null
    && typeof query.result === "object" && "index" in query.result && query.result.index === 0))).toBe(true);
  expect(errors).toEqual([]);
  await page.screenshot({ path: info.outputPath(`overlap-context-library-dpr-${dpr}.png`) });
  await writeFile(info.outputPath(`nearest-pick-dpr-${dpr}.json`), JSON.stringify({
    scope: "Actual native GPU nearest-center/callback regression, not 10K target-device performance or visual-overlap acceptance", checks, errors }, null, 2));
});

test("PC native nearest picker honors nearest center, large square shape, no-hit, stable ties and obsolete-query fencing", async ({ page }) => {
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/native-point-picking.html")}`);
  await page.waitForFunction(() => !!window.__nativePointPicking);
  const reverse = await page.evaluate(() => window.__nativePointPicking.configure({ reverse: true }));
  expect(await page.evaluate(point => window.__nativePointPicking.query(point), reverse.context)).toMatchObject({ index: reverse.contextIndex });
  const square = await page.evaluate(() => window.__nativePointPicking.configure({ diameter: 60 }));
  expect(await page.evaluate(point => window.__nativePointPicking.query([point[0] + 23, point[1] + 23]), square.library))
    .toMatchObject({ index: square.libraryIndex });
  expect(await page.evaluate(point => window.__nativePointPicking.query([point[0] + 26, point[1] + 26]), square.library)).toBeNull();
  const coincident = await page.evaluate(() => window.__nativePointPicking.configure({ coincident: true }));
  expect(await page.evaluate(point => window.__nativePointPicking.query(point), coincident.context)).toMatchObject({ index: 0 });
  // A pointer that only moved half a pixel keeps its query: the fence cannot resolve within this task, and the
  // answer is judged against the pointer once it does. Left targets and jumps are covered by the hover spec.
  await page.evaluate(() => window.__nativePointPicking.configure());
  expect(await page.evaluate(() => window.__nativePointPicking.stale("pointer"))).toEqual({ pendingBefore: true, pendingAfter: true });
  for (const kind of ["view", "scene"] as const) {
    await page.evaluate(() => window.__nativePointPicking.configure());
    expect(await page.evaluate(kind => window.__nativePointPicking.stale(kind), kind)).toEqual({ pendingBefore: true, pendingAfter: false });
  }
  await page.evaluate(() => window.__nativePointPicking.configure());
  const state = await page.evaluate(() => window.__nativePointPicking.state());
  expect(state.restored.every(Boolean)).toBe(true); expect(state.glError).toBe(0); expect(state.result?.index).toBe(0);
  expect(await page.evaluate(() => window.__nativePointPicking.snapshot().glError)).toBe(0);
});

test("PC native nearest picker retires an in-flight query on actual context loss", async ({ page }) => {
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/native-point-picking.html")}`);
  await page.waitForFunction(() => !!window.__nativePointPicking);
  expect(await page.evaluate(() => window.__nativePointPicking.lose())).toEqual({ pendingBefore: true, pendingAfter: false, result: null });
  await page.evaluate(() => window.__nativePointPicking.destroy());
});

test("PC native stationary click retains the pressed original node while GPU positions move before click", async ({ page }, info) => {
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/native-point-picking.html")}`);
  await page.waitForFunction(() => !!window.__nativePointPicking);
  const target = await page.evaluate(() => window.__nativePointPicking.configure());
  await page.mouse.move(target.context[0], target.context[1]);
  await page.waitForFunction(index => window.__nativePointPicking.snapshot().hovered === index, target.contextIndex);
  await page.evaluate(() => window.__nativePointPicking.armReleaseMotion());
  await page.mouse.down();
  const pressed = await page.evaluate(() => window.__nativePointPicking.snapshot());
  expect(pressed.hovered).toBe(target.contextIndex);
  expect(pressed.dragging).toBe(target.contextIndex);
  await page.mouse.up();
  const released = await page.evaluate(() => window.__nativePointPicking.snapshot());
  await writeFile(info.outputPath("stationary-press-motion.json"), JSON.stringify({ target, pressed, released,
    scope: "Controlled known-row GPU motion, not scene replacement, hardware performance, or additional GPU coordinate reads" }, null, 2));
  expect(released.glError).toBe(0);
  expect(released.releaseMotion.map(event => event.type)).toEqual([
    "after-native-pointerdown", "known-position-motion-after-native-pointerup", "after-native-click"]);
  expect(released.readbacks.every(read => read.width === 1 && read.height === 1 && (read.floats === null || read.floats === 4))).toBe(true);
  expect(released.queries.filter(query => query.method === "sync")).toHaveLength(1);
  expect(released.selected).toBe(target.contextIndex);
});

test("PC native stationary fractional pointer retains its precise pressed node through rounded click coordinates", async ({ page }, info) => {
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/native-point-picking.html")}`);
  await page.waitForFunction(() => !!window.__nativePointPicking);
  const target = await page.evaluate(() => window.__nativePointPicking.configure({ fractional: true }));
  await page.mouse.move(target.context[0], target.context[1]);
  await page.waitForFunction(index => window.__nativePointPicking.snapshot().hovered === index, target.contextIndex);
  await page.mouse.down();
  const pressed = await page.evaluate(() => window.__nativePointPicking.snapshot());
  expect(pressed.hovered).toBe(target.contextIndex);
  expect(pressed.dragging).toBe(target.contextIndex);
  await page.mouse.up();
  const released = await page.evaluate(() => window.__nativePointPicking.snapshot());
  await writeFile(info.outputPath("fractional-stationary-press.json"), JSON.stringify({ target, pressed, released,
    scope: "Static original GPU positions: real PointerEvent fractional coordinates versus browser click offset rounding; no position writes" }, null, 2));
  expect(released.glError).toBe(0);
  expect(released.queries.filter(query => query.method === "sync")).toHaveLength(1);
  expect(released.selected).toBe(target.contextIndex);
});

for (const delta of [.1, -.1]) test(`PC native subpixel mouse click retains its pressed node at delta ${delta}`, async ({ page }, info) => {
  await page.goto(`${origin}/@fs/${path.resolve("tests/graph-webgl/native-point-picking.html")}`);
  await page.waitForFunction(() => !!window.__nativePointPicking);
  const target = await page.evaluate(() => window.__nativePointPicking.configure({ fractional: true }));
  await page.mouse.move(target.context[0], target.context[1]);
  await page.waitForFunction(index => window.__nativePointPicking.snapshot().hovered === index, target.contextIndex);
  const events = await page.evaluateHandle(() => {
    const rows: Array<{ type: string; x: number; y: number; trusted: boolean }> = [];
    const canvas = document.querySelector("canvas")!;
    for (const type of ["pointerdown", "pointermove", "pointerup", "mousedown", "click"]) {
      canvas.addEventListener(type, event => {
        const mouse = event as MouseEvent;
        rows.push({ type, x: mouse.clientX, y: mouse.clientY, trusted: mouse.isTrusted });
      }, true);
    }
    return rows;
  });
  await page.evaluate(() => window.__nativePointPicking.armReleaseMotion());
  await page.mouse.down();
  const pressed = await page.evaluate(() => window.__nativePointPicking.snapshot());
  expect(pressed.hovered).toBe(target.contextIndex);
  expect(pressed.dragging).toBe(target.contextIndex);
  await page.mouse.move(target.context[0] + delta, target.context[1] + delta);
  await page.mouse.up();
  const released = await page.evaluate(() => window.__nativePointPicking.snapshot());
  const nativeEvents = await events.jsonValue();
  await events.dispose();
  await writeFile(info.outputPath(`fractional-subpixel-click-${delta}.json`), JSON.stringify({ target, delta, nativeEvents, pressed, released,
    scope: "Trusted browser/D3 click with fractional PointerEvent motion inside one compatibility MouseEvent cell; original GPU winner retained despite release-time position motion" }, null, 2));
  const down = nativeEvents.find(event => event.type === "pointerdown")!;
  const moved = nativeEvents.find(event => event.type === "pointermove")!;
  const click = nativeEvents.find(event => event.type === "click")!;
  expect(nativeEvents.every(event => event.trusted)).toBe(true);
  expect(Math.abs(moved.x - down.x)).toBeGreaterThan(0);
  expect(moved.x).toBeCloseTo(down.x + delta, 3);
  expect(moved.y).toBeCloseTo(down.y + delta, 3);
  expect(click).toBeDefined();
  expect([click.x, click.y]).toEqual([Math.trunc(down.x), Math.trunc(down.y)]);
  expect(released.releaseMotion.map(event => event.type)).toEqual([
    "after-native-pointerdown", "known-position-motion-after-native-pointerup", "after-native-click"]);
  expect(released.glError).toBe(0);
  expect(released.queries.filter(query => query.method === "sync")).toHaveLength(1);
  expect(released.selected).toBe(target.contextIndex);
});

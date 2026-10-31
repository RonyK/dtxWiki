import { expect, test, type CDPSession, type Page } from "@playwright/test";
import { build } from "esbuild";
import path from "node:path";

import type { GraphRenderScene } from "../../frontend/src/features/graph/shared/rendererTypes";

declare global {
  interface Window {
    __graphCanvasFixture: {
      selections: Array<string | null>;
      instances(): number;
      node(id: string): { x: number; y: number };
      lose(): void;
      restore(): void;
    };
    __graphLocalFixture: {
      events: Array<{ type: string; id: string | null }>;
      rawPicks: Array<{ kind: "node" | "edge" | "stage"; id: string | null }>;
      camera(): { x: number; y: number; ratio: number; angle: number };
      counts(): { nodes: number; relations: number };
      edges(): Array<{ id: string; color: string | undefined; hidden: boolean | undefined }>;
      labelPixels(bounds: { x: number; y: number; width: number; height: number }): { alpha: number; ink: number };
      labels(enabled: boolean): void;
      labelDraws(): Array<{ text: string; x: number; y: number }>;
      labelState(): { alpha: number; ink: number; displayed: string[] };
      detail(id: string): Promise<void>;
      kinds(): void;
      node(id: string): {
        point: { x: number; y: number };
        graphPoint: { x: number; y: number };
        color: string | undefined;
        hidden: boolean | undefined;
      };
      focus(id: string | null): void;
      pin(id: string, pinned?: boolean, position?: { x: number; y: number }): { x: number; y: number } | null;
      positions(): Record<string, { x: number; y: number }>;
      replaceScene(scene: { nodes: unknown[]; edges: unknown[] }): void;
      reset(): void;
      scene: { nodes: unknown[]; edges: unknown[] };
      select(id: string | null): void;
      stop(): void;
      stress(): void;
      crowdedLabels(): void;
      zoom(): void;
    };
  }
}

let bundle = "";
let canvasBundle = "";

test.beforeAll(async () => {
  const output = await build({
    entryPoints: [path.resolve("tests/graph-webgl/local-adapter-entry.ts")],
    bundle: true,
    platform: "browser",
    format: "iife",
    write: false,
  });
  bundle = output.outputFiles[0].text;
  const canvasOutput = await build({
    entryPoints: [path.resolve("tests/graph-webgl/local-canvas-entry.ts")],
    bundle: true, platform: "browser", format: "iife", write: false,
    define: { "process.env.NODE_ENV": '"production"' },
  });
  canvasBundle = canvasOutput.outputFiles[0].text;
});

test("Local pin state releases restored coordinates without reviving them on scene replacement", async ({ page }) => {
  const errors = await mountFixture(page);
  await page.evaluate(() => window.__graphLocalFixture.zoom());
  const before = await page.evaluate(() => ({
    camera: window.__graphLocalFixture.camera(),
    positions: window.__graphLocalFixture.positions(),
  }));
  await page.evaluate(() => {
    window.__graphLocalFixture.pin("context:linked", false);
    const scene = window.__graphLocalFixture.scene as GraphRenderScene;
    // Metadata-only replacements preserve live coordinates. A real count /
    // radius change must rebuild without reviving this released restore pin.
    window.__graphLocalFixture.replaceScene({
      ...scene,
      edges: scene.edges.map((edge, index) => index === 0
        ? { ...edge, count: (edge.count ?? 1) + 1 } : edge),
    });
  });
  const after = await page.evaluate(() => ({
    camera: window.__graphLocalFixture.camera(),
    positions: window.__graphLocalFixture.positions(),
    contextEvents: window.__graphLocalFixture.events.filter((event) => event.type.startsWith("context:")),
  }));
  expect(after.contextEvents, "pin/scene assertions require the native engine to remain available").toEqual([]);
  expect(after.positions["context_page:linked"]).not.toEqual(before.positions["context_page:linked"]);
  expect(after.positions["library_item_version:cited"]).toEqual(before.positions["library_item_version:cited"]);
  expect(after.positions["context_page:seed"]).toEqual({ x: 0, y: 0 });
  expect(after.camera).toEqual(before.camera);
  expect(errors).toEqual([]);
});

test("Local pin state reset stays released after replacement and keeps the seed fixed", async ({ page }) => {
  const errors = await mountFixture(page);
  const before = await page.evaluate(() => window.__graphLocalFixture.positions());
  await page.evaluate(() => {
    window.__graphLocalFixture.reset();
    const scene = window.__graphLocalFixture.scene as GraphRenderScene;
    // Exercise a genuine geometry rebuild, not a metadata-only refresh.
    window.__graphLocalFixture.replaceScene({
      ...scene,
      edges: scene.edges.map((edge, index) => index === 0
        ? { ...edge, count: (edge.count ?? 1) + 1 } : edge),
    });
  });
  const after = await page.evaluate(() => window.__graphLocalFixture.positions());
  expect(after["context_page:linked"]).not.toEqual(before["context_page:linked"]);
  expect(after["library_item_version:cited"]).not.toEqual(before["library_item_version:cited"]);
  expect(after["context_page:seed"]).toEqual({ x: 0, y: 0 });
  expect(errors).toEqual([]);
});

test("Local pin state preserves the latest explicit pin across scene replacement", async ({ page }) => {
  const errors = await mountFixture(page);
  await page.evaluate(() => {
    window.__graphLocalFixture.pin("context:linked", true, { x: 31, y: 37 });
    window.__graphLocalFixture.replaceScene(window.__graphLocalFixture.scene);
  });
  const positions = await page.evaluate(() => window.__graphLocalFixture.positions());
  expect(positions["context_page:linked"]).toEqual({ x: 31, y: 37 });
  expect(positions["context_page:seed"]).toEqual({ x: 0, y: 0 });
  expect(errors).toEqual([]);
});

test("Local pin state retains an actual pointer-drag endpoint across scene replacement", async ({ page }) => {
  const errors = await mountFixture(page);
  const point = await page.evaluate(() => window.__graphLocalFixture.node("context:linked").point);
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x + 40, point.y + 30, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => page.evaluate(() =>
    window.__graphLocalFixture.events.filter((event) => event.type === "pin").at(-1)?.id)).toBe("context:linked");
  const dragged = await page.evaluate(() => window.__graphLocalFixture.node("context:linked").graphPoint);
  await page.evaluate(() => window.__graphLocalFixture.replaceScene(window.__graphLocalFixture.scene));
  const positions = await page.evaluate(() => window.__graphLocalFixture.positions());
  expect(positions["context_page:linked"]).toEqual(dragged);
  expect(positions["context_page:seed"]).toEqual({ x: 0, y: 0 });
  expect(errors).toEqual([]);
});

test("Local tiny nodes have a 12px screen-space mouse target and win over relation corridors", async ({ page }, testInfo) => {
  const errors = await mountFixture(page);
  const node = await page.evaluate(() => window.__graphLocalFixture.node("context:linked").point);
  const seed = await page.evaluate(() => window.__graphLocalFixture.node("context:seed").point);
  const distance = Math.hypot(seed.x - node.x, seed.y - node.y);
  await page.mouse.click(node.x + (seed.x - node.x) * 9 / distance,
    node.y + (seed.y - node.y) * 9 / distance);
  expect(await page.evaluate(() => window.__graphLocalFixture.rawPicks.at(-1)))
    .toEqual({ kind: "edge", id: "edge:link" });
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "select").at(-1)?.id)).toBe("context:linked");
  await page.mouse.click(20, 600);
  for (const dx of [6, 11.5]) {
    await page.mouse.click(node.x + dx, node.y);
    await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "select").at(-1)?.id)).toBe("context:linked");
    await page.mouse.click(20, 600);
  }
  await page.mouse.move(node.x + 10, node.y);
  await page.mouse.down();
  await page.mouse.move(node.x + 30, node.y + 15, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "pin").at(-1)?.id)).toBe("context:linked");
  await testInfo.attach("local-12px-hit-and-drag.png", { body: await page.locator("#graph").screenshot(), contentType: "image/png" });
  expect(errors).toEqual([]);
});

for (const recovery of ["restore", "retry"] as const) {
  test(`Local GraphCanvas native WebGL loss exposes fallback and ${recovery} creates a fresh working engine`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.setViewportSize({ width: 860, height: 640 });
    await page.setContent('<style>body{margin:0}#graph,.graph-canvas{width:860px;height:640px;background:white;color:#0f172a}.graph-canvas-frame{position:relative}.graph-canvas-status{position:absolute;inset:12px auto auto 12px;background:white;padding:10px;border:1px solid #999}</style><div id="graph"></div>');
    await page.addScriptTag({ content: canvasBundle });
    const frame = page.locator(".graph-canvas-frame");
    await expect(frame).toHaveAttribute("data-renderer-status", "ready");
    expect(await page.evaluate(() => window.__graphCanvasFixture.instances())).toBe(1);
    await page.evaluate(() => window.__graphCanvasFixture.lose());
    await expect(frame).toHaveAttribute("data-renderer-status", "error");
    await expect(page.getByRole("button", { name: "Retry renderer" })).toBeVisible();
    await expect(page.getByRole("status")).toContainText("relationship list and static view remain available");
    await testInfo.attach(`local-webgl-${recovery}-lost.png`, { body: await page.locator("#graph").screenshot(), contentType: "image/png" });
    if (recovery === "restore") await page.evaluate(() => window.__graphCanvasFixture.restore());
    else await page.getByRole("button", { name: "Retry renderer" }).click();
    await expect(frame).toHaveAttribute("data-renderer-status", "ready");
    expect(await page.evaluate(() => window.__graphCanvasFixture.instances())).toBe(2);
    const point = await page.evaluate(() => window.__graphCanvasFixture.node("note"));
    await page.mouse.click(point.x + 11.5, point.y);
    await expect.poll(() => page.evaluate(() => window.__graphCanvasFixture.selections.at(-1))).toBe("note");
    await page.mouse.click(20, 600);
    await expect.poll(() => page.evaluate(() => window.__graphCanvasFixture.selections.at(-1))).toBeNull();
    const colors = await renderedColors(page);
    expect(colors.purple).toBeGreaterThan(5);
    expect(colors.teal).toBeGreaterThan(5);
    await testInfo.attach(`local-webgl-${recovery}-fresh.png`, { body: await page.locator("#graph").screenshot(), contentType: "image/png" });
    expect(errors).toEqual([]);
  });
}

async function mountFixture(page: Page, width = 860, viewportWidth = width): Promise<string[]> {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.setViewportSize({ width: viewportWidth, height: 640 });
  await page.setContent(`
    <style>
      html, body { margin: 0; }
      #graph { width: ${width}px; height: 640px; background: #fff; color: #0f172a;
        --graph-surface: #fff; --graph-node-context: #7c3aed;
        --graph-node-library: #0f9f8f; --graph-edge-link: #e11d48;
        --graph-edge-citation: #16a34a; --graph-edge-co-access: #2563eb;
        --graph-edge-membership: #db2777; }
    </style>
    <div id="graph" aria-label="Local Graph renderer fixture"></div>
  `);
  await page.addScriptTag({ content: bundle });
  await expect.poll(() => page.locator("#graph canvas").count()).toBeGreaterThan(2);
  await page.evaluate(() => window.__graphLocalFixture.stop());
  await expect.poll(async () => (await renderedColors(page)).nonwhite).toBeGreaterThan(100);
  // Text labels can arrive before native geometry/picking is ready. Require a
  // real Library glyph, using its ink rather than teal in forced colors.
  try {
    if (await page.evaluate(() => matchMedia("(forced-colors: active)").matches)) {
      await expect.poll(async () => (await nodePixels(page, "library:cited")).painted).toBeGreaterThan(10);
    } else {
      await expect.poll(async () => (await renderedColors(page)).teal).toBeGreaterThan(5);
    }
  } catch (error) {
    const diagnostic = await page.evaluate(() => ({
      events: window.__graphLocalFixture.events,
      camera: window.__graphLocalFixture.camera(),
      contexts: Array.from(document.querySelectorAll<HTMLCanvasElement>("#graph canvas")).map((canvas) => {
        const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
        return { className: canvas.className, width: canvas.width, height: canvas.height,
          native: Boolean(gl), lost: gl?.isContextLost(), error: gl?.getError() };
      }),
    }));
    await test.info().attach("native-geometry-readiness.json", {
      contentType: "application/json",
      body: JSON.stringify(diagnostic),
    });
    throw new Error(`Native glyph readiness failed: ${JSON.stringify(diagnostic)}`, { cause: error });
  }
  return errors;
}

async function nodePixels(page: Page, id: string): Promise<{ painted: number; brightness: number }> {
  const point = await page.evaluate((nodeId) => window.__graphLocalFixture.node(nodeId).point, id);
  const screenshot = await page.locator("#graph").screenshot();
  return page.evaluate(async ({ encoded, point: center }) => {
    const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${encoded}`)).blob());
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("2D screenshot decoder unavailable");
    context.drawImage(image, 0, 0);
    // The isolated point is a 3 px circle, away from edges/labels. Inspect its
    // actual WebGL pixels rather than treating fewer colored pixels as success.
    const pixels = context.getImageData(Math.round(center.x) - 3, Math.round(center.y) - 3, 7, 7).data;
    let painted = 0;
    let brightness = 0;
    for (let index = 0; index < pixels.length; index += 4) {
      const [red, green, blue] = [pixels[index], pixels[index + 1], pixels[index + 2]];
      if (red < 250 || green < 250 || blue < 250) {
        painted++;
        brightness += (red + green + blue) / 3;
      }
    }
    return { painted, brightness: painted ? brightness / painted : 255 };
  }, { encoded: screenshot.toString("base64"), point });
}

async function nodeShapePixels(page: Page, id: string): Promise<{
  center: number[]; ring: number[]; middle: number[]; corner: number[];
}> {
  await page.evaluate((nodeId) => window.__graphLocalFixture.detail(nodeId), id);
  const point = await page.evaluate((nodeId) => window.__graphLocalFixture.node(nodeId).point, id);
  expect(Number.isFinite(point.x) && Number.isFinite(point.y), "Reduced-motion centering must never emit NaN camera coordinates").toBe(true);
  expect(point.x).toBeCloseTo(430, 1);
  expect(point.y).toBeCloseTo(320, 1);
  const screenshot = await page.locator("#graph").screenshot();
  return page.evaluate(async ({ encoded, point: center }) => {
    const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${encoded}`)).blob());
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("2D screenshot decoder unavailable");
    context.drawImage(image, 0, 0);
    const pixel = (x: number, y: number) => Array.from(context.getImageData(Math.round(center.x) + x, Math.round(center.y) + y, 1, 1).data).slice(0, 3);
    // A 3 px point at camera ratio 1/6 is ~7.35 px in radius: the square's
    // (6,6) corner is outside a circle, and a folder's (0,0) is transparent.
    return { center: pixel(0, 0), ring: pixel(6, 0), middle: pixel(4, 2), corner: pixel(6, 6) };
  }, { encoded: screenshot.toString("base64"), point });
}

async function touchPan(client: CDPSession): Promise<void> {
  await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: 30, y: 570 }] });
  for (let step = 1; step <= 8; step++) {
    await client.send("Input.dispatchTouchEvent", {
      type: "touchMove", touchPoints: [{ x: 30 + step * 10, y: 570 - step * 6 }],
    });
  }
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
}

async function renderedColors(page: Page): Promise<{
  nonwhite: number;
  purple: number;
  teal: number;
  link: number;
  citation: number;
  coAccess: number;
  membership: number;
}> {
  const screenshot = await page.locator("#graph").screenshot();
  return page.evaluate(async (encoded) => {
    const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${encoded}`)).blob());
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("2D screenshot decoder unavailable");
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let nonwhite = 0;
    let purple = 0;
    let teal = 0;
    let link = 0;
    let citation = 0;
    let coAccess = 0;
    let membership = 0;
    for (let index = 0; index < pixels.length; index += 4) {
      const [red, green, blue] = [pixels[index], pixels[index + 1], pixels[index + 2]];
      if (red < 235 || green < 235 || blue < 235) nonwhite++;
      if (red > 75 && red < 190 && green < 125 && blue > 110 && blue > red) purple++;
      if (red < 90 && green > 100 && blue > 75 && blue < 200 && green > red) teal++;
      if (red > 150 && green < 85 && blue < 115) link++;
      if (red < 80 && green > 120 && blue < 130) citation++;
      if (red < 90 && green < 150 && blue > 170) coAccess++;
      if (red > 160 && green < 90 && blue > 90 && blue < 180) membership++;
    }
    return { nonwhite, purple, teal, link, citation, coAccess, membership };
  }, screenshot.toString("base64"));
}

test("Local Sigma adapter paints point nodes and all four visible relationship kinds without browser errors", async ({ page }, testInfo) => {
  const errors = await mountFixture(page);
  const colors = await renderedColors(page);
  expect(colors.nonwhite, "Local graph canvas should contain visible geometry and labels").toBeGreaterThan(100);
  expect(colors.purple, "Context point should be painted").toBeGreaterThan(5);
  expect(colors.teal, "Library square should be painted").toBeGreaterThan(5);
  expect(colors.link, "Link solid should be painted").toBeGreaterThan(5);
  expect(colors.citation, "Citation long dashes should be painted").toBeGreaterThan(5);
  expect(colors.coAccess, "Read-together dots should be painted").toBeGreaterThan(5);
  expect(colors.membership, "Membership thin dashes should be painted").toBeGreaterThan(5);
  expect(await page.evaluate(() => window.__graphLocalFixture.pin("context:seed"))).toEqual({ x: 0, y: 0 });
  const unrelated = await nodePixels(page, "context:unrelated");
  await page.evaluate(() => window.__graphLocalFixture.focus("context:seed"));
  const dimmed = await nodePixels(page, "context:unrelated");
  expect(dimmed.painted, "Direct-star focus must keep the unrelated point painted").toBeGreaterThan(5);
  expect(dimmed.brightness, "Non-neighbors should be dimmed into the background, not hidden").toBeGreaterThan(unrelated.brightness + 30);
  await page.evaluate(() => window.__graphLocalFixture.zoom());
  const screenshot = await page.locator("#graph").screenshot();
  await testInfo.attach("local-sigma-graph.png", { body: screenshot, contentType: "image/png" });
  expect(errors).toEqual([]);
});

test("Local canvas mouse selection dims non-neighbors and blank stage click restores them", async ({ page }, testInfo) => {
  const errors = await mountFixture(page);
  const initial = await nodePixels(page, "context:unrelated");
  const seed = await page.evaluate(() => window.__graphLocalFixture.node("context:seed").point);
  await page.mouse.click(seed.x, seed.y);
  await page.mouse.move(20, 600);
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "select").at(-1)?.id)).toBe("context:seed");
  const dimmed = await nodePixels(page, "context:unrelated");
  expect(dimmed.painted, "An unrelated node remains visible and pickable after selection").toBeGreaterThan(5);
  expect(dimmed.brightness).toBeGreaterThan(initial.brightness + 30);
  await testInfo.attach("local-selection-dimmed.png", { body: await page.locator("#graph").screenshot(), contentType: "image/png" });
  const unrelated = await page.evaluate(() => window.__graphLocalFixture.node("context:unrelated").point);
  await page.mouse.click(unrelated.x, unrelated.y);
  await page.mouse.move(20, 600);
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "select").at(-1)?.id)).toBe("context:unrelated");
  await page.mouse.click(20, 600);
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "select").at(-1)?.id)).toBeNull();
  const restored = await nodePixels(page, "context:unrelated");
  expect(restored.painted).toBeGreaterThan(5);
  expect(Math.abs(restored.brightness - initial.brightness)).toBeLessThan(5);
  expect(errors).toEqual([]);
});

test("Local selecting an unlinked document keeps every original relationship painted as dim context", async ({ page }, testInfo) => {
  const errors = await mountFixture(page);
  const before = await page.evaluate(() => ({ edges: window.__graphLocalFixture.edges(),
    positions: window.__graphLocalFixture.positions(), camera: window.__graphLocalFixture.camera() }));
  await page.evaluate(() => window.__graphLocalFixture.select("context:unrelated"));
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  const quiet = await page.evaluate(() => window.__graphLocalFixture.edges());
  expect(quiet).toHaveLength(4);
  expect(quiet.every((edge) => !edge.hidden)).toBe(true);
  expect(quiet.every((edge, index) => edge.color !== before.edges[index].color)).toBe(true);
  // Read actual native-frame pixels in a corridor away from the endpoint dots
  // and labels. Graphology counts/display flags alone cannot prove drawing.
  const endpoints = await page.evaluate(() => [window.__graphLocalFixture.node("context:seed").point,
    window.__graphLocalFixture.node("context:linked").point]);
  const screenshot = await page.locator("#graph").screenshot();
  const painted = await page.evaluate(async ({ png, endpoints }) => {
    const image = new Image();
    image.src = `data:image/png;base64,${png}`;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = image.width; canvas.height = image.height;
    const context = canvas.getContext("2d")!;
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(0, 0, image.width, image.height).data;
    const [source, target] = endpoints;
    let ink = 0;
    for (let step = 20; step <= 80; step += 1) {
      const x = source.x + (target.x - source.x) * step / 100;
      const y = source.y + (target.y - source.y) * step / 100;
      for (let dx = -2; dx <= 2; dx += 1) for (let dy = -2; dy <= 2; dy += 1) {
        const px = Math.round(x + dx), py = Math.round(y + dy);
        if (px < 0 || py < 0 || px >= image.width || py >= image.height) continue;
        const i = (py * image.width + px) * 4;
        if (pixels[i + 3] && Math.min(pixels[i], pixels[i + 1], pixels[i + 2]) < 250) ink += 1;
      }
    }
    return ink;
  }, { png: screenshot.toString("base64"), endpoints });
  expect(painted, "Nonincident original link must still reach the actual framebuffer").toBeGreaterThan(20);
  await testInfo.attach("local-nonincident-links-dimmed.png", { contentType: "image/png", body: screenshot });
  await page.evaluate(() => window.__graphLocalFixture.select(null));
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  const after = await page.evaluate(() => ({ edges: window.__graphLocalFixture.edges(),
    positions: window.__graphLocalFixture.positions(), camera: window.__graphLocalFixture.camera() }));
  expect(after).toEqual(before);
  expect(errors).toEqual([]);
});

test("Local DOM keyboard emphasis replaces stationary hover and preserves later pointer hover", async ({ page }, testInfo) => {
  const errors = await mountFixture(page);
  await testInfo.attach("local-native-initial-state.json", {
    body: JSON.stringify(await page.evaluate(() => ({
      events: window.__graphLocalFixture.events,
      nodes: ["context:seed", "library:cited", "context:unrelated"].map((id) => ({ id, ...window.__graphLocalFixture.node(id) })),
      contexts: ["nodes", "edges"].map((name) => {
        const canvas = document.querySelector<HTMLCanvasElement>(`#graph canvas.sigma-${name}`);
        const gl = canvas?.getContext("webgl2") ?? canvas?.getContext("webgl");
        return { name, width: canvas?.width, height: canvas?.height, lost: gl?.isContextLost(), error: gl?.getError() };
      }),
    }))), contentType: "application/json",
  });
  await page.evaluate(() => {
    for (const [id, target] of [["focus-node", "context:unrelated"], ["focus-edge", "edge:link"], ["outside-focus", null]] as const) {
      const button = document.createElement("button");
      button.id = id;
      button.textContent = id;
      button.style.position = "fixed";
      button.style.bottom = "0";
      button.style.left = `${document.querySelectorAll("button").length * 140}px`;
      button.addEventListener("focus", () => window.__graphLocalFixture.focus(target));
      button.addEventListener("blur", () => window.__graphLocalFixture.focus(null));
      document.body.appendChild(button);
    }
    window.__graphLocalFixture.select("context:seed");
  });
  const before = await page.evaluate(() => ({
    camera: window.__graphLocalFixture.camera(),
    positions: window.__graphLocalFixture.positions(),
    counts: window.__graphLocalFixture.counts(),
  }));
  const unrelated = await nodePixels(page, "context:unrelated");
  const seed = await page.evaluate(() => window.__graphLocalFixture.node("context:seed").point);
  await page.mouse.move(seed.x, seed.y);
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "hover").at(-1)?.id)).toBe("context:seed");
  await page.locator("#focus-node").focus();
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "hover").at(-1)?.id)).toBeNull();
  const nodeFocused = await nodePixels(page, "context:unrelated");
  expect(nodeFocused.painted).toBeGreaterThan(5);
  expect(nodeFocused.brightness).toBeLessThan(unrelated.brightness - 30);
  const focusScreenshot = testInfo.outputPath("local-dom-keyboard-node.png");
  await page.locator("#graph").screenshot({ path: focusScreenshot });
  await testInfo.attach("local-dom-keyboard-node.png", { path: focusScreenshot, contentType: "image/png" });
  await page.keyboard.press("Tab");
  await expect(page.locator("#focus-edge")).toBeFocused();
  const edgeFocused = await nodePixels(page, "context:unrelated");
  expect(edgeFocused.brightness).toBeGreaterThan(nodeFocused.brightness + 30);
  const point = await page.evaluate(() => window.__graphLocalFixture.node("context:unrelated").point);
  await page.mouse.move(point.x, point.y);
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "hover").at(-1)?.id)).toBe("context:unrelated");
  await page.keyboard.press("Tab");
  await expect(page.locator("#outside-focus")).toBeFocused();
  expect((await nodePixels(page, "context:unrelated")).brightness).toBeLessThan(edgeFocused.brightness - 30);
  await page.mouse.move(20, 500);
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "hover").at(-1)?.id)).toBeNull();
  expect(Math.abs((await nodePixels(page, "context:unrelated")).brightness - unrelated.brightness)).toBeLessThan(5);
  const after = await page.evaluate(() => ({
    camera: window.__graphLocalFixture.camera(),
    positions: window.__graphLocalFixture.positions(),
    counts: window.__graphLocalFixture.counts(),
    mutations: window.__graphLocalFixture.events.filter((event) => ["select", "pin", "context:lost"].includes(event.type)),
  }));
  expect(after.camera).toEqual(before.camera);
  expect(after.positions).toEqual(before.positions);
  expect(after.counts).toEqual(before.counts);
  expect(after.mutations).toEqual([]);
  expect(errors).toEqual([]);
});

test("Local canvas mouse drag commits one pin and release outside the canvas permits later stage pan", async ({ page }) => {
  // Release over a real page gutter, not outside the browser viewport where
  // Firefox cannot be expected to dispatch document/window mouseup at all.
  const errors = await mountFixture(page, 860, 960);
  const node = await page.evaluate(() => window.__graphLocalFixture.node("context:linked"));
  await page.mouse.move(node.point.x, node.point.y);
  await page.mouse.down();
  await page.mouse.move(900, 100, { steps: 10 });
  await page.mouse.up();
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "pin").map((event) => event.id))).toEqual(["context:linked"]);
  const pinned = await page.evaluate(() => window.__graphLocalFixture.node("context:linked").graphPoint);
  expect(pinned).not.toEqual(node.graphPoint);
  const cameraBefore = await page.evaluate(() => window.__graphLocalFixture.camera());
  await page.mouse.move(20, 600);
  await page.mouse.down();
  await page.mouse.move(160, 520, { steps: 8 });
  await page.mouse.up();
  await expect.poll(async () => {
    const cameraAfter = await page.evaluate(() => window.__graphLocalFixture.camera());
    return Math.hypot(cameraAfter.x - cameraBefore.x, cameraAfter.y - cameraBefore.y);
  }).toBeGreaterThan(0.05);
  expect(await page.evaluate(() => window.__graphLocalFixture.node("context:linked").graphPoint)).toEqual(pinned);
  expect(await page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "pin").length)).toBe(1);
  expect(errors).toEqual([]);
});

for (const end of ["touchEnd", "touchCancel"] as const) {
  test(`Local canvas touch ${end === "touchEnd" ? "tap" : "cancellation"} ends node interaction and permits stage pan at 390 px`, async ({ page }, testInfo) => {
    const errors = await mountFixture(page, 390);
    const client = await page.context().newCDPSession(page);
    await client.send("Emulation.setTouchEmulationEnabled", { enabled: true });
    const node = await page.evaluate(() => window.__graphLocalFixture.node("context:linked"));
    await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: node.point.x, y: node.point.y }] });
    await client.send("Input.dispatchTouchEvent", { type: end, touchPoints: [] });
    if (end === "touchEnd") {
      await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "select").at(-1)?.id)).toBe("context:linked");
      await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: 30, y: 570 }] });
      await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "select").at(-1)?.id), {
        message: "A blank-stage touch must clear the prior node selection",
      }).toBeNull();
    } else {
      expect(await page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "select")),
        "A cancelled touch must not be treated as a successful node tap").toEqual([]);
    }
    const cameraBefore = await page.evaluate(() => window.__graphLocalFixture.camera());
    await touchPan(client);
    await expect.poll(async () => {
      const cameraAfter = await page.evaluate(() => window.__graphLocalFixture.camera());
      return Math.hypot(cameraAfter.x - cameraBefore.x, cameraAfter.y - cameraBefore.y);
    }, { message: "Stage pan must move the camera after a completed/cancelled node touch" }).toBeGreaterThan(0.05);
    const nodeAfter = await page.evaluate(() => window.__graphLocalFixture.node("context:linked"));
    expect(nodeAfter.graphPoint, "A later stage gesture must not drag the previously tapped node").toEqual(node.graphPoint);
    expect(await page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "pin"))).toEqual([]);
    await testInfo.attach(`local-touch-${end}.png`, { body: await page.locator("#graph").screenshot(), contentType: "image/png" });
    await client.detach();
    expect(errors).toEqual([]);
  });
}

test("Local 390px touch taps the 12px padded node target and later stage input remains independent", async ({ page }) => {
  const errors = await mountFixture(page, 390);
  const client = await page.context().newCDPSession(page);
  await client.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  const node = await page.evaluate(() => window.__graphLocalFixture.node("context:linked"));
  await client.send("Input.dispatchTouchEvent", {
    type: "touchStart", touchPoints: [{ x: node.point.x + 11.5, y: node.point.y }],
  });
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "select").at(-1)?.id)).toBe("context:linked");
  await touchPan(client);
  expect(await page.evaluate(() => window.__graphLocalFixture.node("context:linked").graphPoint)).toEqual(node.graphPoint);
  expect(await page.evaluate(() => window.__graphLocalFixture.events.filter((event) => event.type === "pin"))).toEqual([]);
  await client.detach();
  expect(errors).toEqual([]);
});

test("Local reduced-motion reset at the 250-node/750-relation budget preserves coordinates and completes in one bounded batch", async ({ page }, testInfo) => {
  const errors = await mountFixture(page);
  await page.evaluate(() => {
    window.__graphLocalFixture.stress();
    for (let index = 1; index <= 12; index++) window.__graphLocalFixture.pin(`stress:${index}`);
  });
  const result = await page.evaluate(() => {
    const before = window.__graphLocalFixture.positions();
    const started = performance.now();
    window.__graphLocalFixture.reset();
    return { milliseconds: performance.now() - started, before, after: window.__graphLocalFixture.positions(), counts: window.__graphLocalFixture.counts() };
  });
  expect(result.counts).toEqual({ nodes: 250, relations: 750 });
  expect(Object.keys(result.before)).toHaveLength(250);
  expect(result.after, "Reduced-motion reset must not silently rerun the layout for each unpinned node").toEqual(result.before);
  expect(result.milliseconds, "Reset must not perform 250 separate 300-tick synchronous settles").toBeLessThan(1000);
  await testInfo.attach("local-reduced-motion-reset.json", {
    body: JSON.stringify({ ...result.counts, pinnedBeforeReset: 12, milliseconds: result.milliseconds }),
    contentType: "application/json",
  });
  await testInfo.attach("local-reduced-motion-250-750.png", { body: await page.locator("#graph").screenshot(), contentType: "image/png" });
  expect(errors).toEqual([]);
});

for (const forcedColors of [false, true]) {
  test(`Local actual API node kinds retain distinct ${forcedColors ? "forced-color shapes" : "colors and shapes"}`, async ({ page }, testInfo) => {
    await page.emulateMedia({ forcedColors: forcedColors ? "active" : "none" });
    const errors = await mountFixture(page);
    await page.evaluate(() => window.__graphLocalFixture.kinds());
    expect(await page.evaluate(() => window.__graphLocalFixture.counts())).toEqual({ nodes: 7, relations: 0 });
    await testInfo.attach(`local-actual-kinds-${forcedColors ? "forced" : "light"}.png`, {
      body: await page.locator("#graph").screenshot(), contentType: "image/png",
    });
    const context = await nodeShapePixels(page, "kind:0");
    const contextVersion = await nodeShapePixels(page, "kind:1");
    const libraryVersion = await nodeShapePixels(page, "kind:2");
    const libraryArtifact = await nodeShapePixels(page, "kind:3");
    const folder = await nodeShapePixels(page, "kind:4");
    const conversation = await nodeShapePixels(page, "kind:5");
    const seed = await nodeShapePixels(page, "kind:6");
    await testInfo.attach(`local-actual-kinds-${forcedColors ? "forced" : "light"}-pixels.json`, {
      body: JSON.stringify({ context, contextVersion, libraryVersion, libraryArtifact, folder, conversation, seed }),
      contentType: "application/json",
    });
    const isWhite = (pixel: number[]) => pixel.every((channel) => channel > 245);
    expect(isWhite(context.corner), "Context is a circle, not a square").toBe(true);
    expect(isWhite(contextVersion.corner), "Context versions retain the Context circle").toBe(true);
    expect(isWhite(libraryVersion.corner), "Real library_item_version must paint its square corner").toBe(false);
    expect(isWhite(libraryArtifact.corner), "Real library_artifact must paint its square corner").toBe(false);
    expect(isWhite(folder.center), "Folder ring center must remain unfilled").toBe(true);
    expect(isWhite(folder.ring), "Folder ring outline must still paint").toBe(false);
    if (forcedColors) {
      expect(isWhite(context.center), "Context remains a filled point in forced colors").toBe(false);
      expect(isWhite(conversation.center), "Conversation bullseye has a filled center").toBe(false);
      // The border program's 2 px antialias blends the narrow gap into gray at
      // this zoom. Require a strong actual gap, not a pure-white center sample.
      expect(Math.min(...conversation.middle), "Conversation bullseye has a contrasting inner gap").toBeGreaterThan(150);
      expect(Math.min(...conversation.middle) - Math.max(...context.middle), "The bullseye gap must differ visibly from a Context's filled circle").toBeGreaterThan(150);
      expect(isWhite(conversation.ring), "Conversation bullseye retains its outer ring").toBe(false);
      expect(isWhite(context.middle), "Conversation's gap is distinct from the Context filled circle").toBe(false);
    } else {
      const isPurple = ([red, green, blue]: number[]) => red > 75 && red < 190 && green < 125 && blue > 110 && blue > red;
      const isTeal = ([red, green, blue]: number[]) => red < 90 && green > 100 && blue > 75 && blue < 200 && green > red;
      const isOrange = ([red, green, blue]: number[]) => red > 180 && green > 45 && green < 150 && blue < 80;
      expect(isPurple(context.center)).toBe(true);
      expect(isPurple(contextVersion.center), "context_page_version must not fall into conversation orange").toBe(true);
      expect(isTeal(libraryVersion.center), "library_item_version must not fall into conversation orange").toBe(true);
      expect(isTeal(libraryArtifact.center), "library_artifact must not fall into conversation orange").toBe(true);
      expect(isOrange(conversation.center)).toBe(true);
      expect(isPurple(seed.center)).toBe(true);
      expect(seed.ring[2], "Seed outline must use contrast ink instead of a subpixel border").toBeLessThan(100);
    }
    expect(errors).toEqual([]);
  });
}

test("Local labels-off keeps actual seed and selected glyphs while suppressing ordinary labels", async ({ page }, testInfo) => {
  const errors = await mountFixture(page);
  await page.evaluate(() => window.__graphLocalFixture.labels(false));
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.labelState().displayed)).toEqual(["context:seed"]);
  const seedState = await page.evaluate(() => window.__graphLocalFixture.labelState());
  const seed = await page.evaluate(() => window.__graphLocalFixture.node("context:seed").point);
  const seedPixels = await page.evaluate((point) => window.__graphLocalFixture.labelPixels({
    x: point.x + 10, y: point.y - 10, width: 150, height: 20,
  }), seed);
  expect(seedPixels.ink, "The seed label must contain real text pixels, not only a forceLabel flag").toBeGreaterThan(20);
  const library = await page.evaluate(() => window.__graphLocalFixture.node("library:cited").point);
  const libraryRegion = { x: library.x - 150, y: library.y - 10, width: 300, height: 20 };
  expect(await page.evaluate((bounds) => window.__graphLocalFixture.labelPixels(bounds).alpha, libraryRegion)).toBe(0);
  await page.mouse.click(library.x, library.y);
  await page.mouse.move(20, 600);
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.labelState().displayed)).toEqual(["context:seed", "library:cited"]);
  const libraryPixels = await page.evaluate((bounds) => window.__graphLocalFixture.labelPixels(bounds), libraryRegion);
  const selectedState = await page.evaluate(() => window.__graphLocalFixture.labelState());
  expect(libraryPixels.ink,
    "Selecting a real Library square must paint its important label even with ordinary labels off").toBeGreaterThan(20);
  expect(selectedState.alpha).toBeGreaterThan(seedPixels.alpha);
  await testInfo.attach("local-important-label-pixels.json", {
    body: JSON.stringify({ seedState, selectedState, seedPixels, libraryPixels }), contentType: "application/json",
  });
  await testInfo.attach("local-important-labels-with-toggle-off.png", {
    body: await page.locator("#graph").screenshot(), contentType: "image/png",
  });
  expect(errors).toEqual([]);
});

test("Local Library square labels near the right boundary use the shared inward clamp", async ({ page }, testInfo) => {
  const errors = await mountFixture(page);
  await page.evaluate(() => window.__graphLocalFixture.kinds());
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.labelState().alpha)).toBeGreaterThan(100);
  const node = await page.evaluate(() => window.__graphLocalFixture.node("kind:2").point);
  expect(node.x, "The fixture square must be near the right boundary").toBeGreaterThan(800);
  const inward = await page.evaluate((point) => window.__graphLocalFixture.labelPixels({
    x: point.x - 150, y: point.y - 10, width: 140, height: 20,
  }), node);
  const outward = await page.evaluate((point) => window.__graphLocalFixture.labelPixels({
    x: point.x + 7, y: point.y - 10, width: 860 - point.x - 7, height: 20,
  }), node);
  await testInfo.attach("local-square-label-pixels.json", {
    body: JSON.stringify({ node, inward, outward }), contentType: "application/json",
  });
  expect(inward.ink, "The square's actual glyphs must move inside the canvas instead of clipping outward").toBeGreaterThan(20);
  expect(outward.alpha, "No truncated label glyph/stroke may be painted into the boundary corridor").toBe(0);
  await testInfo.attach("local-square-label-clamped.png", {
    body: await page.locator("#graph").screenshot(), contentType: "image/png",
  });
  expect(errors).toEqual([]);
});

test("crowded Local priority labels all paint in native Sigma without moving nodes or camera", async ({ page }, testInfo) => {
  const errors = await mountFixture(page);
  await page.evaluate(() => {
    window.__graphLocalFixture.crowdedLabels();
    window.__graphLocalFixture.labels(false);
  });
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.labelDraws().length)).toBe(10);
  const before = await page.evaluate(() => ({
    positions: window.__graphLocalFixture.positions(), camera: window.__graphLocalFixture.camera(),
  }));
  const draws = await page.evaluate(() => window.__graphLocalFixture.labelDraws());
  expect(draws.map((draw) => draw.text).sort()).toEqual(Array.from({ length: 10 }, (_, i) => `Priority title ${i}`).sort());
  expect(new Set(draws.map((draw) => draw.y)).size).toBe(10);
  for (const draw of draws) {
    expect(draw.x).toBeGreaterThanOrEqual(0); expect(draw.x).toBeLessThanOrEqual(860);
    expect(draw.y).toBeGreaterThanOrEqual(0); expect(draw.y).toBeLessThanOrEqual(640);
  }
  const pixels = await page.evaluate(() => window.__graphLocalFixture.labelState());
  expect(pixels.ink).toBeGreaterThan(200);
  await page.evaluate(() => window.__graphLocalFixture.labels(true));
  await expect.poll(() => page.evaluate(() => window.__graphLocalFixture.labelDraws().length)).toBe(10);
  expect(await page.evaluate(() => ({
    positions: window.__graphLocalFixture.positions(), camera: window.__graphLocalFixture.camera(),
  }))).toEqual(before);
  await testInfo.attach("crowded-priority-labels.json", { contentType: "application/json", body: JSON.stringify({
    draws, pixels, scope: "Actual pinned Sigma native paint in deliberately coincident ten-seed regression; not 2K map layout qualification",
  }) });
  await page.screenshot({ path: testInfo.outputPath("crowded-priority-labels.png") });
  expect(errors).toEqual([]);
});

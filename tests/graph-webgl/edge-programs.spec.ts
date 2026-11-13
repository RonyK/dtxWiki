import { expect, test, type Browser, type Page } from "@playwright/test";
import { build } from "esbuild";
import { writeFile } from "node:fs/promises";
import path from "node:path";

import { GRAPH_LINE_OPACITY, graphLineFade } from "../../frontend/src/features/graph/shared/graphTokens";
import type { ProbeResult } from "./browser-entry";

declare global {
  interface Window {
    __graphWebglProbe: (ratio: number, membershipCenterPhase?: 0 | 0.5) => ProbeResult;
    __graphWebglPick: (x: number, y: number) => string | null;
  }
}

const EXPECTED_DASH_PX = {
  citation: [8, 3, 1.5, 3],
  coAccess: [5, 3.5],
  membership: [10, 4],
  action: [1.5, 3.5],
} as const;

let bundle = "";

test.beforeAll(async () => {
  const output = await build({
    entryPoints: [path.resolve("tests/graph-webgl/browser-entry.ts")],
    bundle: true,
    platform: "browser",
    format: "iife",
    write: false,
  });
  bundle = output.outputFiles[0].text;
});

async function openWebgl1Fixture(browser: Browser, deviceScaleFactor: number, forcedColors: "active" | "none" = "none"): Promise<Page> {
  const context = await browser.newContext({
    viewport: { width: 720, height: 480 },
    deviceScaleFactor,
    forcedColors,
  });
  const page = await context.newPage();
  await page.setContent(`
    <style>
      html,body{margin:0;background:#000}
      #graph{width:720px;height:480px;background:#000;color:#fff;
        --graph-surface:#000;--graph-edge-link:#fff;
        --graph-edge-citation:#fff;--graph-edge-co-access:#fff;
        --graph-edge-membership:#fff;--graph-edge-action:#fff}
    </style>
    <div id="graph" aria-label="Graph shader fixture"></div>
  `);
  await page.evaluate(() => {
    const getContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type, ...args) {
      // Sigma prefers WebGL2. Force its documented WebGL1 fallback path.
      if (type === "webgl2") return null;
      return getContext.call(this, type, ...args);
    } as typeof getContext;

    const getExtension = WebGLRenderingContext.prototype.getExtension;
    WebGLRenderingContext.prototype.getExtension = function (name) {
      // The custom Local edge programs must render without this extension.
      if (name === "ANGLE_instanced_arrays") return null;
      return getExtension.call(this, name);
    };
  });
  await page.addScriptTag({ content: bundle });
  return page;
}

function runs(alpha: number[]): { on: number[]; off: number[] } {
  const values = alpha.map((value) => value >= 32);
  const segments: { lit: boolean; length: number }[] = [];
  for (const lit of values) {
    const last = segments.at(-1);
    if (last?.lit === lit) last.length++;
    else segments.push({ lit, length: 1 });
  }
  // The first and last strokes can be partial because the sample is clipped.
  const whole = segments.slice(1, -1);
  return {
    on: whole.filter((segment) => segment.lit).map((segment) => segment.length),
    off: whole.filter((segment) => !segment.lit).map((segment) => segment.length),
  };
}

function assertRepeatingRuns(actual: number[], expected: readonly number[], label: string): void {
  // A clipped sample can begin at either stroke of a dash-dot cycle. Prove
  // the complete alternating sequence, including the short dots.
  const matches = expected.some((_, phase) => actual.every((length, index) =>
    Math.abs(length - expected[(index + phase) % expected.length]) <= 1.5));
  expect(matches, `${label}: expected repeating ${expected}, measured ${actual}`).toBe(true);
}

function assertPatterns(result: ProbeResult): void {
  expect(result.webglVersion).toContain("WebGL 1.0");
  expect(result.angleAvailable).toBe(false);
  expect(result.edges.map((edge) => edge.kind)).toEqual([
    "link", "citation", "coAccess", "membership", "action",
  ]);

  const link = result.edges[0];
  expect(link.alpha.filter((value) => value >= 32).length).toBeGreaterThan(link.lengthCssPx * 0.95);

  for (const edge of result.edges.slice(1)) {
    const pattern = EXPECTED_DASH_PX[edge.kind as keyof typeof EXPECTED_DASH_PX];
    const expectedOn = pattern.filter((_, index) => index % 2 === 0);
    const expectedOff = pattern.filter((_, index) => index % 2 === 1);
    const measured = runs(edge.alpha);
    expect(measured.on.length, `${edge.kind} needs several visible dashes (max alpha ${Math.max(...edge.alpha)})`).toBeGreaterThan(5);
    expect(measured.off.length, `${edge.kind} needs several visible gaps`).toBeGreaterThan(5);
    assertRepeatingRuns(measured.on, expectedOn, `${edge.kind} on CSS-px lengths`);
    assertRepeatingRuns(measured.off, expectedOff, `${edge.kind} off CSS-px lengths`);
  }
  for (const edge of result.edges) {
    if (!edge.endpoints) continue; // Zoomed-in endpoints may be outside the viewport.
    const { source, target } = edge.endpoints;
    if (edge.kind === "coAccess") {
      expect(target.upper, "Undirected co-access has no target wings").toBeLessThan(8);
      expect(target.lower).toBeLessThan(8);
    } else {
      expect(target.tip, `${edge.kind} target tip follows the node boundary`).toBeGreaterThan(32);
      expect(target.upper, `${edge.kind} upper arrow edge keeps CSS size`).toBeGreaterThan(32);
      expect(target.lower, `${edge.kind} lower arrow edge keeps CSS size`).toBeGreaterThan(32);
      if (edge.kind === "action") {
        expect(target.hollow, "Action triangle interior is filled").toBeGreaterThan(60);
        expect(target.outsideSmallHead, "Action triangle is smaller than the open arrows").toBeLessThan(8);
      } else {
        expect(target.hollow, `${edge.kind} open arrow must stay hollow`)
          .toBeLessThan(Math.max(target.upper, target.lower) * 0.3);
      }
    }
    expect(target.insideNode, `${edge.kind} arrow must stop before the target glyph`).toBeLessThan(8);
    expect(source.insideNode, `${edge.kind} line must stop before the source glyph`).toBeLessThan(8);
    if (edge.kind === "membership") {
      expect(source.near, "Containment diamond begins at its container").toBeGreaterThan(32);
      expect(source.upper, "Containment diamond upper outline").toBeGreaterThan(32);
      expect(source.lower, "Containment diamond lower outline").toBeGreaterThan(32);
      expect(source.hollow, "Containment source diamond must stay hollow").toBeLessThan(8);
    } else {
      expect(source.upper, `${edge.kind} has no source diamond`).toBeLessThan(8);
      expect(source.lower).toBeLessThan(8);
    }
  }
}

test("all five Local edge shaders compile and keep dash periods in CSS pixels across zoom and DPR", async ({ browser }, testInfo) => {
  for (const dpr of [1, 2]) {
    const page = await openWebgl1Fixture(browser, dpr);
    try {
      for (const ratio of [0.5, 1, 2]) {
        let linkPeak = 0, linkExpected = 0;
        for (const phase of [0, 0.5] as const) {
          const result = await page.evaluate(
            ({ cameraRatio, centerPhase }) => window.__graphWebglProbe(cameraRatio, centerPhase),
            { cameraRatio: ratio, centerPhase: phase },
          );
          expect(result.pixelRatio).toBeCloseTo(dpr, 2);
          expect(result.forcedColorsActive).toBe(false);
          const actualPhase = result.edges[3].centerCssY % 1;
          const phaseError = Math.min(
            Math.abs(actualPhase - phase),
            Math.abs(actualPhase - phase - 1),
            Math.abs(actualPhase - phase + 1),
          );
          expect(phaseError).toBeLessThan(0.05);
          const link = result.edges[0];
          linkPeak = Math.max(linkPeak, ...link.alpha);
          linkExpected = 255 * GRAPH_LINE_OPACITY.line * graphLineFade(link.lengthCssPx * ratio, result.edges.length);
          try {
            assertPatterns(result);
          } catch (error) {
            const artifact = testInfo.outputPath("edge-pixel-failure.json");
            await writeFile(artifact, JSON.stringify({ dpr, ratio, phase, result }, null, 2));
            await testInfo.attach("edge-pixel-failure", { path: artifact, contentType: "application/json" });
            throw error;
          }
        }
        // GRAPH_LINE_OPACITY: the solid line's ink and long-line fade follow its fitted-view
        // (ratio 1) length whatever the zoom. One of the two pixel phases fully
        // covers a row; below ratio 1 the line leaves the viewport.
        if (ratio >= 1) expect(Math.abs(linkPeak - linkExpected), `ratio ${ratio} DPR ${dpr}`).toBeLessThanOrEqual(3);
      }
    } finally {
      await page.context().close();
    }
  }
});

test("the reduced-resolution picking buffer covers intentional citation and action gaps", async ({ browser }) => {
  const page = await openWebgl1Fixture(browser, 1);
  try {
    const result = await page.evaluate(() => window.__graphWebglProbe(1, 0));
    for (const kind of ["citation", "action"] as const) {
      const edge = result.edges.find((edge) => edge.kind === kind);
      if (!edge) throw new Error(`Missing ${kind} fixture edge`);
      const gap = edge.alpha.findIndex(
        (alpha, index, values) => index > 20 && index < values.length - 20
          && alpha < 8 && values[index - 1] < 8 && values[index + 1] < 8,
      );
      expect(gap, `fixture needs an intentional ${kind} gap`).toBeGreaterThan(0);
      const hit = await page.evaluate(
        ({ x, y }) => window.__graphWebglPick(x, y),
        { x: edge.startCssX + gap, y: edge.centerCssY },
      );
      expect(hit).toBe(kind);
      const empty = await page.evaluate(
        ({ x, y }) => window.__graphWebglPick(x, y),
        { x: edge.startCssX + gap, y: edge.centerCssY + 30 },
      );
      expect(empty).toBeNull();
    }
  } finally {
    await page.context().close();
  }
});

test("monochrome forced-colors retains relationship and distinct action dash topology", async ({ browser }) => {
  const page = await openWebgl1Fixture(browser, 1, "active");
  try {
    const result = await page.evaluate(() => window.__graphWebglProbe(1));
    expect(result.forcedColorsActive).toBe(true);
    expect(result.tokenInk).not.toBe(result.tokenSurface);
    // The fixture passes readGraphTokens(forced) ink to Graphology, so the
    // rendered WebGL stroke must actually use the resolved system color.
    const expectedRgb = await page.evaluate((ink) => {
      const color = document.createElement("span");
      color.style.color = ink;
      color.style.setProperty("forced-color-adjust", "none");
      document.body.append(color);
      const rgb = getComputedStyle(color).color.match(/\d+/g)?.slice(0, 3).map(Number);
      color.remove();
      return rgb;
    }, result.tokenInk);
    expect(result.edges[0].peakRgb).toEqual(expectedRgb);
    assertPatterns(result);
  } finally {
    await page.context().close();
  }
});

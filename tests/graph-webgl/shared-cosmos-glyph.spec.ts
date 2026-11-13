import { expect, test, type Browser, type Page } from "@playwright/test";
import { build } from "esbuild";
import { writeFile } from "node:fs/promises";
import path from "node:path";

import type { SharedCosmosProbe } from "./shared-cosmos-preparation-entry";

let cosmosBundle = "";

test.beforeAll(async () => {
  const output = await build({
    entryPoints: [path.resolve("tests/graph-webgl/shared-cosmos-preparation-entry.ts")],
    bundle: true, platform: "browser", format: "esm", write: false,
  });
  cosmosBundle = output.outputFiles[0].text;
});

async function openSharedCosmosFixture(browser: Browser, deviceScaleFactor: number): Promise<Page> {
  const context = await browser.newContext({ viewport: { width: 720, height: 600 }, deviceScaleFactor, forcedColors: "active" });
  const page = await context.newPage();
  await page.setContent(`<style>
    html,body{margin:0;background:#000;forced-color-adjust:none}
    #graph{position:relative;width:720px;height:600px;background:#000;color:#fff;forced-color-adjust:none}
    </style><div id="graph" aria-label="Shared Cosmos Context preparation"></div>`);
  await page.addScriptTag({ content: cosmosBundle, type: "module" });
  await page.waitForFunction(() => Boolean(window.__sharedCosmosPreparation));
  return page;
}

function assertSharedCosmosPresentation(result: SharedCosmosProbe): void {
  const glyph = Object.fromEntries(result.glyphs.map(node => [node.kind, node]));
  expect(glyph.context).toMatchObject({ centerInk: true, farCornerInk: false });
  expect(glyph.version).toMatchObject({ centerInk: true, topInk: true, bottomInk: false });
  expect(glyph.agent).toMatchObject({ centerInk: true, cutoutInk: false });
  expect(glyph.agentVersion).toMatchObject({ centerInk: true, cutoutInk: false, topInk: true, bottomInk: false });
  expect(glyph.library.cornerInk).toBe(true);
  expect(glyph.conversation).toMatchObject({ topInk: true, cornerInk: false });
  expect(glyph.folder).toMatchObject({ centerInk: false, bandInk: true });
  expect(glyph.cluster).toMatchObject({ centerInk: false, bodyInk: true });
  expect(glyph.version.diameter / glyph.context.diameter).toBeCloseTo(1.35, 6);
  expect(result.originalNodeIds).toHaveLength(10);
  expect(result.originalRelationIds).toEqual(["original:relation:link"]);
  expect(result.glError).toBe(0);
  expect(result.runtimeError).toBeNull();
}

// Independent failure path: native glyph rasterization and metadata repaint can
// fail while Sigma's Local dash shaders pass. A unit test cannot observe the
// GPU sprite, SDF atlas, real pointer picking or camera/pin retention together.
// One collected case: 2 DPRs × 2 zooms = 4 visual inputs, plus 2 metadata updates.
test("shared Cosmos glyphs keep forced-color cues and reader state through metadata repaint", async ({ browser }, testInfo) => {
  for (const dpr of [1, 2]) {
    // Exercise glyphs through the shared core at two DPRs and zoom levels.
    // Keep the pin and metadata repaint checks on the actual GPU path.
    // This fixture has no application server or new end-to-end journey.
    const shared = await openSharedCosmosFixture(browser, dpr);
    try {
      let last: SharedCosmosProbe | null = null;
      for (const zoom of [.75, 1.25]) {
        const result = await shared.evaluate(zoom => window.__sharedCosmosPreparation.probe(zoom), zoom);
        last = result;
        try { assertSharedCosmosPresentation(result); }
        catch (error) {
          const artifact = testInfo.outputPath(`shared-cosmos-dpr-${dpr}-zoom-${zoom}.json`);
          await writeFile(artifact, JSON.stringify(result, null, 2));
          await testInfo.attach("shared-cosmos-pixels", { path: artifact, contentType: "application/json" });
          throw error;
        }
      }
      const updated = await shared.evaluate(() => window.__sharedCosmosPreparation.updateMetadata());
      expect(updated.positionsAfter).toEqual(updated.positionsBefore);
      expect(updated.cameraAfter).toEqual(updated.cameraBefore);
      expect(updated.probe.repaintInk).toBeGreaterThanOrEqual(60);
      expect(updated.probe.repaintInk).toBeLessThanOrEqual(68);
      expect(updated.probe.glError).toBe(0);
      expect(updated.probe.runtimeError).toBeNull();
      const triangle = last!.glyphs.find(node => node.kind === "agentVersion")!;
      await shared.mouse.click(...triangle.center);
      await expect.poll(() => shared.evaluate(() => window.__sharedCosmosPreparation.selection().selected))
        .toBe("original:node:agentVersion");
      await expect.poll(() => shared.evaluate(() => window.__sharedCosmosPreparation.selection().ring)).toContain("L");
    } finally {
      await shared.evaluate(() => window.__sharedCosmosPreparation.destroy());
      await shared.context().close();
    }
  }
});

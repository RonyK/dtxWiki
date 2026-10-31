#!/usr/bin/env node
/* global process, window, console */
// Diagnostic-only local software-rendered browser smoke; not the physical V0 gate.
import { execFileSync } from 'node:child_process';
import { mkdtemp } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const current = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(current, '../../../../..');
const outputDir = await mkdtemp(path.join(os.tmpdir(), 'dtxwiki-workspace-map-smoke-'));
const variant = process.argv[2] || 'default';
if (!['default', 'default-fit', 'default-delayed', 'light-active-40', 'light-reduced-40',
  'light-active-2000', 'light-reduced-2000', 'light-links-2000'].includes(variant)) {
  throw new Error(`Unknown workspace Map smoke variant: ${variant}`);
}
const smallScene = variant.endsWith('-40');
const mediumScene = variant.endsWith('-2000');
const expectedNodes = smallScene ? 40 : mediumScene ? 2_000 : 20_000;
const expectedEdges = smallScene ? 40 : mediumScene ? 4_700 : 47_000;
const screenshotPath = path.join(outputDir, `workspace-map-${variant}.png`);
const server = await createServer({
  root: frontendRoot, configFile: false, logLevel: 'error',
  server: { host: '127.0.0.1', port: 0, strictPort: false },
});
let browser;
try {
  await server.listen();
  const port = server.httpServer.address().port;
  browser = await chromium.launch({ headless: true,
    executablePath: process.env.DTXWIKI_V0_HEADLESS_CHROME || undefined,
    args: ['--enable-webgl', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));
  const query = smallScene || mediumScene
    ? `?geometry=${expectedNodes}&theme=light&motion=${variant.includes('reduced') || variant.includes('links') ? 'reduce' : 'active'}${variant.includes('links') ? '&topology=links&size=app' : ''}${mediumScene && variant.includes('active') ? '&delay=5000' : ''}`
    : variant === 'default-fit' ? '?fit=manual' : variant === 'default-delayed' ? '?delay=5000' : '';
  await page.goto(`http://127.0.0.1:${port}/src/features/graph/workspace/tests/browserSmoke.html${query}`, { waitUntil: 'networkidle', timeout: 60_000 });
  await page.waitForFunction(() => window.__workspaceMapSmoke?.status !== 'loading', undefined, { timeout: 60_000 });
  const result = await page.evaluate(() => window.__workspaceMapSmoke);
  if (result.status !== 'ready' || result.nodeCount !== expectedNodes || result.edgeCount !== expectedEdges ||
      result.glError !== 0 || !result.canvas?.width || !result.canvas?.height ||
      result.labelCount > 40 || !result.togglesApplied || pageErrors.length ||
      result.workerStatus !== 'complete' || result.coordinateCount !== expectedNodes * 2 ||
      !Number.isFinite(result.minCoordinate) || result.minCoordinate < 248 ||
      !Number.isFinite(result.maxCoordinate) || result.maxCoordinate > 3848) {
    throw new Error(`Workspace Map smoke failed: ${JSON.stringify({ result, pageErrors })}`);
  }
  await page.screenshot({ path: screenshotPath });
  const pixels = execFileSync('ffmpeg', ['-v', 'error', '-i', screenshotPath,
    '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], { maxBuffer: 1440 * 900 * 3 + 8192 });
  let purplePixels = 0;
  for (let offset = 0; offset < pixels.length; offset += 3) {
    const red = pixels[offset], green = pixels[offset + 1], blue = pixels[offset + 2];
    if (red > 70 && red < 200 && green > 35 && green < 165 && blue > red && blue > green + 35) purplePixels += 1;
  }
  if (purplePixels < (smallScene ? 100 : 500)) throw new Error(`Canvas did not visibly paint context points: ${purplePixels} purple pixels`);
  console.log(JSON.stringify({ ...result, variant, purplePixels, pageErrors, screenshotPath,
    caveat: 'Synthetic SwiftShader scene; not app endpoint or physical-GPU performance gate' }, null, 2));
} finally {
  if (browser) await browser.close();
  await server.close();
}

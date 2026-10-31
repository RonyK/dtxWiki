import { expect, test } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";

import { evidenceScreenshot } from "../e2e/evidence";

import type {} from "../../frontend/src/features/graph/workspace/tests/browserSmoke";

let server: ViteDevServer;
let origin: string;
test.beforeAll(async () => {
  server = await createServer({
    root: path.resolve("frontend"), configFile: false, logLevel: "error",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("Map fixture server did not bind TCP");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

// Native capacity uses the maximum edge budget; the real-API motion case owns 30,895 edges.
test("native PC 10K preserves 100000 relations, real movement and finite camera ownership", async ({ page }, testInfo) => {
  // Sixteen natural GPU frames draw 100K relations on headless software WebGL.
  // This checks counted GPU work and camera lifecycle, not a device speed SLA.
  test.setTimeout(90_000);
  const relations = 100_000;
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  let bodyFailed = false;
  try {
    await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=10000&budget=pc10k&relations=${relations}&theme=light&motion=normal&topology=links&interaction=targets`);
    await page.waitForFunction(() => window.__workspaceMapSmoke?.status !== "loading" && Boolean(window.__workspaceMapSmoke));
    expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
    const before = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
    expect(before.positions).toHaveLength(20_000);
    expect(before.positions.every(Number.isFinite)).toBe(true);
    expect(before.uploadedRelationCount).toBe(relations);
    expect(before.uploadedTopologyMatches).toBe(true);
    expect(before.layoutUnits).toBeNull();
    expect(before.collisionRadius).toBe(0);
    expect(before.collisionFootprint?.radii).toBe(10_000);
    expect(before.running).toBe(true);
    expect(before.forceConfig).toEqual([.25, 1, 1, 10, 1, 1]);
    expect(before.glError).toBe(0);
    const checkpoints = await page.evaluate(before => window.__workspaceMapInteraction!.pcCheckpoints(before),
      { positions: before.positions, nativeTicks: before.nativeTicks });
    const { initialFits, geometry, paused, frozen, pausedFits } = checkpoints;
    expect(initialFits.length).toBeGreaterThan(0);
    expect(initialFits.every(fit => fit.points === 10_000)).toBe(true);
    expect(geometry).toHaveLength(3);
    for (const [index, delta] of [8, 16].entries()) expect(geometry[index + 1].tick).toBeGreaterThanOrEqual(before.nativeTicks + delta);
    expect(geometry.every(frame => frame.unpinnedBoundaryNodes === 0)).toBe(true);
    expect(checkpoints.initialMovementObserved).toBe(true);
    expect(paused.running).toBe(false);
    expect(frozen.positions).toEqual(paused.positions);
    expect(frozen.viewport).toEqual(paused.viewport);
    await evidenceScreenshot(page, testInfo, `pc-10k-${relations}-normal-motion-paused.png`);
    const continuation = await page.evaluate(positions => window.__workspaceMapInteraction!.pcResume(positions), paused.positions);
    const { resumed } = continuation;
    expect(resumed.positions.some((value, index) => Math.abs(value - paused.positions[index]) > .001)).toBe(true);
    expect(resumed.running).toBe(true);
    expect(resumed.topologyUploads).toBe(before.topologyUploads);
    expect(resumed.geometryUploads).toBe(before.geometryUploads);
    expect(resumed.viewport).toEqual(paused.viewport);
    expect(resumed.fits).toEqual(pausedFits);
    expect(resumed.glError).toBe(0);
    for (const diagnostics of [checkpoints.diagnostics, continuation.diagnostics]) {
      expect(diagnostics.frames).toBeGreaterThan(0);
      expect(diagnostics.positionReads).toBe(diagnostics.frames);
      expect(diagnostics.remountCaptures).toBe(0);
      expect(diagnostics.glErrorCodes).toEqual([0]);
    }
    expect(errors).toEqual([]);
    await writeFile(testInfo.outputPath(`pc-10k-${relations}-evidence.json`), JSON.stringify({
      nodeCount: before.positions.length / 2, relations: before.uploadedRelationCount,
      topologyMatches: before.uploadedTopologyMatches, initialFits,
      collisionFootprint: before.collisionFootprint,
      geometry,
      movementObserved: true, pausePreservedPositions: true, resumePreservedCamera: true,
      observation: { checkpoints: checkpoints.diagnostics, continuation: continuation.diagnostics },
      renderer: continuation.renderer, errors,
      scope: "Actual native GPU function and camera lifecycle fixture with fixed-tick body-disk/boundary diagnostics; no target-device performance or zero-overlap acceptance claim",
    }, null, 2));
  } catch (error) { bodyFailed = true; throw error; }
  finally {
    try {
      const cleanup = await page.evaluate(() => {
        window.__workspaceMapInteraction?.destroy();
        return { canvases: document.querySelectorAll("#stage canvas").length,
          labels: document.querySelectorAll("[data-workspace-map-labels]").length };
      });
      await writeFile(testInfo.outputPath(`pc-10k-${relations}-cleanup.json`), JSON.stringify(cleanup));
      expect(cleanup).toEqual({ canvases: 0, labels: 0 });
    } catch (error) { if (!bodyFailed) throw error; }
  }
});

test("native PC10K naturally ends after upstream cooling and only explicit Resume wakes it", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=10000&budget=pc10k&relations=30895&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => window.__workspaceMapSmoke?.status !== "loading" && Boolean(window.__workspaceMapSmoke));
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  const before = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  const cooling = await page.evaluate(() => window.__workspaceMapInteraction!.nativeCooling());
  const evidence = JSON.stringify({
    cooling, errors,
    scope: "One real GPU step from public start(.0010005), next-RAF stock natural end, Fit/zoom independence and explicit Resume; not elapsed-time convergence or target-device performance",
  }, null, 2);
  await writeFile(testInfo.outputPath("native-pc10k-natural-cooling.json"), evidence);
  await testInfo.attach("native-pc10k-natural-cooling", { contentType: "application/json", body: evidence });
  expect(cooling.afterStep.alpha).toBeGreaterThan(0);
  expect(cooling.afterStep.alpha).toBeLessThan(.001);
  expect(cooling.afterStep.running).toBe(true);
  expect(cooling.manualTicks).toBe(1);
  expect(cooling.afterEnd).toEqual({ alpha: cooling.afterStep.alpha, running: false });
  expect(cooling.endEvents).toBe(1);
  expect(cooling.afterCamera).toEqual(cooling.afterEnd);
  expect(cooling.cameraTicks).toBe(0);
  expect(cooling.afterResume).toEqual({ alpha: .15, running: true });
  expect(cooling.nodeCount).toBe(10_000);
  expect(cooling.relationCount).toBe(30_895);
  expect(cooling.geometryUploads).toBe(before.geometryUploads);
  expect(cooling.topologyUploads).toBe(before.topologyUploads);
  expect(cooling.glError).toBe(0);
  expect(errors).toEqual([]);
});

for (const action of ["unpin", "reset"] as const) {
  test(`native PC10K ${action} releases a cooled pin into real GPU motion`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    let bodyFailed = false;
    try {
      await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=10000&budget=pc10k&relations=30895&theme=light&motion=normal&topology=links&interaction=targets`);
      await page.waitForFunction(() => window.__workspaceMapSmoke?.status !== "loading" && Boolean(window.__workspaceMapSmoke));
      expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
      await page.waitForFunction(() => document.getElementById("stage")?.dataset.mapCommunityLayoutStatus === "applied");
      const { release, renderer } = await page.evaluate(async action => ({
        release: await window.__workspaceMapInteraction!.nativePinRelease(action),
        renderer: window.__workspaceMapSmoke?.renderer ?? null,
      }), action);
      const evidence = JSON.stringify({ release, renderer, errors,
        scope: "Original PC10K document moved through public pin API, one real GPU threshold-crossing step, native RAF end, release and fixed four-tick motion checkpoint; no real-pointer, target-device latency or convergence claim",
      }, null, 2);
      await writeFile(testInfo.outputPath(`native-pc10k-cooled-${action}.json`), evidence);
      await testInfo.attach(`native-pc10k-cooled-${action}`, { contentType: "application/json", body: evidence });
      expect(release.action).toBe(action); expect(release.id).toBe("node-0");
      expect(release.afterPin.canonicalPosition).toEqual(release.target);
      expect(release.afterPin.canonicalPosition).not.toEqual(release.beforePin.canonicalPosition);
      expect(release.afterPin.geometryUploads).toBe(release.beforePin.geometryUploads + 1);
      expect(release.manualTicks).toBe(1);
      expect(release.afterStep.alpha).toBeGreaterThan(0); expect(release.afterStep.alpha).toBeLessThan(.001);
      expect(release.afterStep.running).toBe(true);
      expect(release.afterEnd.alpha).toBe(release.afterStep.alpha);
      expect(release.afterEnd.running).toBe(false); expect(release.endEvents).toBe(1);
      expect(release.afterEnd.pinned).toEqual([0]);
      expect(release.afterEnd.canonicalPosition).toEqual(release.target);
      expect(release.afterRelease.alpha).toBe(.15); expect(release.afterRelease.running).toBe(true);
      expect(release.afterRelease.pinned).toEqual([]);
      expect(release.afterRelease.canonicalPosition).toEqual(release.target);
      expect(release.afterMovement.nativeTicks).toBeGreaterThanOrEqual(release.afterRelease.nativeTicks + 4);
      expect(release.afterMovement.running).toBe(true);
      expect(Math.hypot(...release.afterMovement.canonicalPosition.map((value, index) => value - release.target[index])))
        .toBeGreaterThan(.001);
      for (const frame of [release.afterPin, release.afterStep, release.afterEnd, release.afterRelease,
        release.afterMovement, release.paused, release.frozen]) {
        expect(frame.nodeCount).toBe(10_000); expect(frame.relationCount).toBe(30_895);
        expect(frame.topologyMatches).toBe(true); expect(frame.glError).toBe(0);
        expect(frame.geometryUploads).toBe(release.afterPin.geometryUploads);
        expect(frame.topologyUploads).toBe(release.beforePin.topologyUploads);
        expect(frame.forceCommits).toBe(release.beforePin.forceCommits);
        expect(frame.collisionCommits).toBe(release.beforePin.collisionCommits);
      }
      expect(release.paused.running).toBe(false); expect(release.frozen.running).toBe(false);
      expect(release.frozen.nativeTicks).toBe(release.paused.nativeTicks);
      expect(release.frozen.canonicalPosition).toEqual(release.paused.canonicalPosition);
      expect(errors).toEqual([]);
      await evidenceScreenshot(page, testInfo, `native-pc10k-cooled-${action}.png`);
    } catch (error) { bodyFailed = true; throw error; }
    finally {
      try {
        const cleanup = await page.evaluate(() => {
          window.__workspaceMapInteraction?.destroy();
          return { canvases: document.querySelectorAll("#stage canvas").length,
            labels: document.querySelectorAll("[data-workspace-map-labels]").length };
        });
        await writeFile(testInfo.outputPath(`native-pc10k-cooled-${action}-cleanup.json`), JSON.stringify(cleanup));
        expect(cleanup).toEqual({ canvases: 0, labels: 0 });
      } catch (error) { if (!bodyFailed) throw error; }
    }
  });
}

test("native 2k supports a 4096 texture limit without clamping canonical document coordinates", async ({ page }, testInfo) => {
  // Capability emulation on the real native WebGL pipeline, not a physical
  // mobile performance claim. Luma and the adapter observe the same limit.
  await page.addInitScript(() => {
    const original = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (parameter) {
      return parameter === this.MAX_TEXTURE_SIZE ? 4096 : original.call(this, parameter);
    };
  });
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => Boolean(window.__workspaceMapSmoke && window.__workspaceMapSmoke.status !== "loading"));
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  await page.waitForFunction(() => document.getElementById("stage")?.dataset.mapCommunityLayoutStatus === "applied"
    && window.__workspaceMapInteraction?.snapshot().layoutRunning
    && window.__workspaceMapInteraction.snapshot().layoutTicks >= 3);
  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const before = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  await evidenceScreenshot(page, testInfo, "map-2k-texture-limit-4096.png");
  await writeFile(testInfo.outputPath("map-2k-texture-limit-4096.json"), JSON.stringify(before, null, 2));
  expect(before.maxTextureSize).toBe(4096);
  expect(before.nativeSpaceSize).toBe(2048);
  expect(before.positions).toHaveLength(4000);
  // Official CPU coordinates are signed and unclamped; only the native GPU
  // coordinate conversion changes at this capability limit.
  expect(before.positions.every(Number.isFinite)).toBe(true);
  expect(before.canonicalPositions).toEqual(before.positions.map(value => value * 2));
  expect(before.glError).toBe(0);
  expect(before.running).toBe(false);
  expect(before.layoutRunning).toBe(false);
  await page.evaluate(() => { window.__workspaceMapInteraction!.zoom(2); window.__workspaceMapInteraction!.fit(); });
  await page.waitForTimeout(150);
  const after = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(after.positions).toEqual(before.positions);
  expect(after.nativeSpaceSize).toBe(before.nativeSpaceSize);
  expect(after.forceConfig).toEqual(before.forceConfig);
  expect(after.geometryUploads).toBe(before.geometryUploads);
  expect(after.running).toBe(false);
});

test("native 2k collision basis belongs to the scene, not Fit, zoom, focus, pin or resize", async ({ page }, testInfo) => {
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => Boolean(window.__workspaceMapSmoke && window.__workspaceMapSmoke.status !== "loading"));
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  await page.waitForFunction(() => document.getElementById("stage")?.dataset.mapCommunityLayoutStatus === "applied");
  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const before = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(await page.locator("#stage").getAttribute("data-map-physics")).toBe("force-atlas2-worker");
  await evidenceScreenshot(page, testInfo, "map-2k-cpu-native-drawing-desktop.png");
  const calibration = await page.evaluate(() => window.__workspaceMapInteraction!.calibration());
  // Independently compute the pinned SDK's initial Fit conversion from actual
  // native input columns and CSS dimensions, not an evolving camera/readback.
  const expectedBasis = (positions: number[]) => {
    const xs = positions.filter((_value, index) => index % 2 === 0);
    const ys = positions.filter((_value, index) => index % 2 === 1);
    const dx = Math.max(...xs) - Math.min(...xs);
    const dy = Math.max(...ys) - Math.min(...ys);
    const zoom = Math.max(.001, Math.min(calibration.width * .84 / (dx || 1),
      calibration.height * .84 / (dy || 1)));
    return { radius: Math.max(...calibration.pointSizes) / (2 * zoom), padding: 1 / zoom };
  };
  const fallbackBasis = expectedBasis(calibration.fallbackPositions);
  const workerBasis = expectedBasis(calibration.workerPositions);
  expect(before.collisionRadius).toBeGreaterThan(0);
  expect(before.collisionPadding).toBeGreaterThan(0);
  expect(before.collisionRadius).toBeCloseTo(workerBasis.radius, 8);
  expect(before.collisionPadding).toBeCloseTo(workerBasis.padding, 8);
  expect(before.collisionRadius).not.toBeCloseTo(fallbackBasis.radius, 3);
  expect(before.collisionCommits).toBe(2);
  expect(before.forceCommits).toBe(0);
  expect(before.running).toBe(false);
  const units = before.layoutUnits!;
  expect(units.nodeCount).toBe(2000);
  expect(units.referenceViewport).toEqual({ width: calibration.width, height: calibration.height });
  expect(units.referenceZoom).toBeCloseTo(1 / workerBasis.padding, 8);
  expect(units.rendererUnitsPerModelUnit).toBe(1);
  expect(units.collisionRadiusModel).toEqual(units.nominalRadiusCss.map(radius =>
    Math.fround((radius + units.pairGapCss / 2) / units.referenceZoom)));
  const basis = (value: typeof before) => [value.collisionRadius, value.collisionPadding,
    value.collisionCommits, value.forceCommits, ...value.forceConfig];
  for (const action of ["fit", "zoom", "focus", "pin", "resize", "fit"] as const) {
    await page.evaluate(async (action) => {
      const fixture = window.__workspaceMapInteraction!;
      if (action === "zoom") fixture.zoom(3);
      else if (action === "focus") fixture.focus("node-0");
      else if (action === "pin") fixture.isolate();
      else if (action === "resize") {
        const stage = document.getElementById("stage")!;
        stage.style.width = "640px";
        stage.style.height = "520px";
        fixture.resize();
      }
      else fixture[action]();
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }, action);
    const after = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
    expect(basis(after), `${action} must not recalibrate physics`).toEqual(basis(before));
    expect(after.layoutUnits, `${action} must preserve canonical CPU force units`).toEqual(units);
    expect(after.topologyUploads).toBe(before.topologyUploads);
    expect(after.running).toBe(false);
    expect(after.glError).toBe(0);
    if (action === "fit" || action === "zoom" || action === "focus") {
      // Before the explicit pin, camera/presentation actions keep all points.
      if (after.pinned.length === 0) expect(after.positions).toEqual(before.positions);
    }
  }
  const pinned = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(pinned.pinned).toContain(0);
  await page.evaluate(() => window.__workspaceMapInteraction!.resume());
  await page.waitForFunction(previousTicks => {
    const next = window.__workspaceMapInteraction!.snapshot();
    return next.layoutRunning && next.layoutTicks > previousTicks + 2;
  }, pinned.layoutTicks);
  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const resumed = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(basis(resumed)).toEqual(basis(before));
  expect(resumed.pinned).toContain(0);
  expect(resumed.positions.slice(0, 2)).toEqual(pinned.positions.slice(0, 2));
  expect(resumed.positions.slice(2)).not.toEqual(pinned.positions.slice(2));
  expect(resumed.running).toBe(false);
  expect(resumed.layoutUnits).toEqual(units);
  expect(resumed.topologyUploads).toBe(before.topologyUploads);
  await testInfo.attach("scene-collision-basis", { contentType: "application/json", body: JSON.stringify({
    radius: before.collisionRadius, padding: before.collisionPadding, commits: resumed.collisionCommits,
    forceCommits: resumed.forceCommits, forceConfig: resumed.forceConfig, fallbackBasis, workerBasis,
    calibrationViewport: { width: calibration.width, height: calibration.height },
    originalPoints: before.positions.length / 2, pinned: resumed.pinned,
    scope: "Actual official CPU Worker and Cosmos GPU drawing; scene-lifetime calibration, not target-device performance",
  }) });
});

for (const textureLimit of [null, 4096]) {
test(`native 2k Cosmos pointer drag saves its canonical position through Resume, pause and reduced motion${textureLimit ? " at texture limit 4096" : ""}`, async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  if (textureLimit) await page.addInitScript((limit) => {
    const original = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (parameter) {
      return parameter === this.MAX_TEXTURE_SIZE ? limit : original.call(this, parameter);
    };
  }, textureLimit);
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => Boolean(window.__workspaceMapSmoke && window.__workspaceMapSmoke.status !== "loading"));
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  await page.waitForFunction(() => document.getElementById("stage")?.dataset.mapCommunityLayoutStatus === "applied");
  expect(await page.locator("#stage").getAttribute("data-map-physics")).toBe("force-atlas2-worker");
  await page.evaluate(async () => {
    const fixture = window.__workspaceMapInteraction!;
    fixture.stop();
    fixture.fit();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  const before = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  const target = await page.evaluate(() => window.__workspaceMapInteraction!.dragTarget());
  expect(before.positions).toHaveLength(4000);
  expect(before.pinned).toEqual([]);
  expect(before.dragPins).toEqual([]);
  // Pick a real, unobscured native dot. No fixture pin(), isolate(), position
  // setter, synthetic drag callback or diagnostic GPU upload is involved.
  expect(target.clearance).toBeGreaterThan(6);
  // Chromium mouse/D3 input uses CSS pixel coordinates. A projected GPU
  // center can be fractional; define the actual pointer pixel explicitly,
  // instead of comparing an integer native event with that fractional center.
  const pointerStart: [number, number] = [Math.round(target.screen[0]), Math.round(target.screen[1])];
  await page.evaluate(() => {
    const stage = document.getElementById("stage")!;
    // Register before mouse-down: D3's drag installs a window capture handler
    // that stops later propagation, so a stage listener sees only the start.
    window.addEventListener("mousemove", (event) => {
      stage.dataset.fixtureMouseCoordinates = JSON.stringify([event.clientX, event.clientY, event.offsetX, event.offsetY]);
    }, { capture: true, passive: true });
  });
  await page.mouse.move(pointerStart[0], pointerStart[1]);
  await page.waitForFunction((id) => window.__workspaceMapSmoke?.hoveredId === id, target.id, { timeout: 3000 });
  await page.mouse.down();
  await page.waitForFunction((index) => window.__workspaceMapInteraction!.snapshot().draggingIndex === index,
    target.index, { timeout: 3000 });
  const destination: [number, number] = [pointerStart[0] + 48, pointerStart[1] + 24];
  await page.mouse.move(destination[0], destination[1], { steps: 4 });
  const deliveredMouseCoordinates: number[] = await page.evaluate(() => JSON.parse(
    document.getElementById("stage")!.dataset.fixtureMouseCoordinates ?? "[]") as number[]);
  expect(deliveredMouseCoordinates).toEqual([...destination, ...destination]);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const dragging = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(dragging.draggingIndex).toBe(target.index);
  expect(dragging.running).toBe(false);
  await page.mouse.up();
  await page.waitForFunction((index) => {
    const snapshot = window.__workspaceMapInteraction!.snapshot();
    return snapshot.dragPins.length === 1 && snapshot.pinned.includes(index) && snapshot.draggingIndex === null;
  }, target.index, { timeout: 3000 });
  const dragged = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  const movedScreen = await page.evaluate((index) => window.__workspaceMapInteraction!.point(index), target.index);
  const pinnedPosition = dragged.positions.slice(target.index * 2, target.index * 2 + 2);
  expect(movedScreen[0]).toBeCloseTo(destination[0], 0);
  expect(movedScreen[1]).toBeCloseTo(destination[1], 0);
  expect(pinnedPosition).not.toEqual(before.positions.slice(target.index * 2, target.index * 2 + 2));
  expect(dragged.dragPins).toEqual([{ id: target.id, position: dragged.canonicalPositions.slice(target.index * 2, target.index * 2 + 2) }]);
  if (textureLimit) {
    expect(dragged.nativeSpaceSize).toBe(textureLimit / 2);
    expect(dragged.dragPins[0].position).toEqual(pinnedPosition.map(value => value * 4096 / dragged.nativeSpaceSize));
  }
  expect(dragged.viewport).toEqual(before.viewport);
  expect(dragged.geometryUploads).toBeGreaterThan(before.geometryUploads);
  expect(dragged.topologyUploads).toBe(before.topologyUploads);
  expect(dragged.glError).toBe(0);

  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect(await page.evaluate(() => window.__workspaceMapInteraction!.snapshot().positions)).toEqual(dragged.positions);
  await page.evaluate(() => window.__workspaceMapInteraction!.resume());
  await page.waitForFunction(({ positions, index: pinnedIndex }) => {
    const next = window.__workspaceMapInteraction!.snapshot();
    return next.layoutRunning && next.layoutTicks > 0 && !next.running
      && next.positions.some((value, index) => Math.floor(index / 2) !== pinnedIndex
      && Math.abs(value - positions[index]) > 0.01);
  }, { positions: dragged.positions, index: target.index }, { timeout: 5000 });
  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const resumed = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(resumed.positions.slice(target.index * 2, target.index * 2 + 2)).toEqual(pinnedPosition);
  expect(resumed.pinned).toContain(target.index);
  expect(resumed.geometryUploads).toBeGreaterThan(dragged.geometryUploads);
  expect(resumed.topologyUploads).toBe(before.topologyUploads);
  expect(resumed.running).toBe(false);
  expect(resumed.layoutRunning).toBe(false);
  expect(resumed.dragPins).toEqual(dragged.dragPins);
  expect(resumed.glError).toBe(0);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect(await page.evaluate(() => window.__workspaceMapInteraction!.snapshot().positions)).toEqual(resumed.positions);

  await page.evaluate(() => { window.__workspaceMapInteraction!.reduce(true); window.__workspaceMapInteraction!.resume(); });
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const reduced = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(reduced.running).toBe(false);
  expect(reduced.positions).toEqual(resumed.positions);
  expect(reduced.pinned).toContain(target.index);
  expect(reduced.geometryUploads).toBe(resumed.geometryUploads);
  expect(reduced.topologyUploads).toBe(before.topologyUploads);
  expect(reduced.layoutRunning).toBe(false);
  expect(reduced.glError).toBe(0);
  expect(errors).toEqual([]);
  await testInfo.attach("native-cosmos-pointer-drag", { contentType: "application/json", body: JSON.stringify({
    target, pointerStart, destination, deliveredMouseCoordinates, pinnedPosition,
    pinCallback: dragged.dragPins[0], geometryUploads: resumed.geometryUploads, topologyUploads: resumed.topologyUploads,
    pinnedAfterResume: resumed.pinned, paused: !resumed.layoutRunning, reducedMotionRunning: reduced.layoutRunning,
    errors, scope: "Actual native 2K mouse drag with official CPU Worker layout and Cosmos GPU drawing; not target-device performance or real-API acceptance",
  }) });
});
}

test("native 2k hover names its direct neighbors without moving the graph", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => Boolean(window.__workspaceMapSmoke && window.__workspaceMapSmoke.status !== "loading"));
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  await page.waitForFunction(() => document.getElementById("stage")?.dataset.mapCommunityLayoutStatus === "applied");
  expect(await page.locator("#stage").getAttribute("data-map-physics")).toBe("force-atlas2-worker");
  await page.evaluate(async () => {
    const fixture = window.__workspaceMapInteraction!;
    fixture.stop();
    fixture.fit();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  const labels = page.locator("[data-workspace-map-labels] span");
  const candidateIds = () => labels.evaluateAll((elements) => elements
    .map((element) => (element as HTMLElement).dataset.nodeId ?? "").filter(Boolean));
  const before = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  const overviewCandidates = await candidateIds();
  const target = await page.evaluate(() => window.__workspaceMapInteraction!.dragTarget());
  // Derive the original fixture's incident endpoints independently from its
  // published topology, not from the adapter's highlight or label candidates.
  const neighbors = new Set<number>();
  const incidentLinks: number[] = [];
  for (let edge = 0; edge < 4700; edge += 1) {
    const source = edge % 2000;
    const destination = (source + 1 + Math.floor(edge / 2000) * 73) % 2000;
    if (source === target.index) neighbors.add(destination);
    if (destination === target.index) neighbors.add(source);
    if (source === target.index || destination === target.index) incidentLinks.push(edge);
  }
  const neighborIds = [...neighbors].sort((left, right) => left - right).map((index) => `node-${index}`);
  expect(neighborIds.length).toBeGreaterThan(0);
  expect(target.clearance).toBeGreaterThan(6);
  await evidenceScreenshot(page, testInfo, "map-2k-neighbor-labels-before.png");
  await page.mouse.move(Math.round(target.screen[0]), Math.round(target.screen[1]));
  await page.waitForFunction((id) => window.__workspaceMapSmoke?.hoveredId === id,
    target.id, { timeout: 3000 });
  // Labels share the existing bounded 200ms paint schedule. Let that schedule
  // run; do not inject graph positions or call a fixture renderer/engine hook.
  await page.waitForTimeout(200);
  const hovered = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  const hoverCandidates = await candidateIds();
  const visibleHoverCandidates = await labels.evaluateAll((elements) => elements.filter((element) => {
    const bounds = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && bounds.width > 0 && bounds.height > 0
      && bounds.left >= 0 && bounds.top >= 0 && bounds.right <= innerWidth && bounds.bottom <= innerHeight;
  }).map((element) => (element as HTMLElement).dataset.nodeId ?? "").filter(Boolean));
  const hoverLabelCount = await labels.count();
  await evidenceScreenshot(page, testInfo, "map-2k-neighbor-labels-hover.png");
  // Fit's margin leaves this real canvas pixel blank. Native picking/MouseOut,
  // rather than a synthetic callback, restores the ordinary label candidates.
  await page.mouse.move(0, 0);
  await page.waitForFunction(() => window.__workspaceMapSmoke?.hoveredId === null, null, { timeout: 3000 });
  await page.waitForTimeout(200);
  const cleared = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  const clearedCandidates = await candidateIds();
  await evidenceScreenshot(page, testInfo, "map-2k-neighbor-labels-clear.png");
  await testInfo.attach("native-neighbor-labels", { contentType: "application/json", body: JSON.stringify({
    target, neighborIds, incidentLinks, overviewCandidates, hoverCandidates, visibleHoverCandidates, clearedCandidates,
    nativeLinks: { before: before.highlightedLinks, hovered: hovered.highlightedLinks, cleared: cleared.highlightedLinks },
    nativeGreyoutOpacity: { before: before.linkGreyoutOpacity, hovered: hovered.linkGreyoutOpacity, cleared: cleared.linkGreyoutOpacity },
    hoverLabelCount, geometryUploads: before.geometryUploads,
    scope: "Real native 2K mouse hover and DOM labels on the original fixture topology; not API or target-device performance acceptance",
  }) });

  expect(hoverCandidates).toContain(target.id);
  expect(before.highlightedLinks).toEqual([]);
  // Native renderer state must contain exactly the original direct incident
  // edges. Greyout zero hides every nonincident stroke without deleting or
  // replacing GPU relation inputs; MouseOut restores the overview config.
  expect(hovered.highlightedLinks).toEqual(incidentLinks);
  expect(hovered.linkGreyoutOpacity).toBe(0);
  expect(cleared.highlightedLinks).toEqual(before.highlightedLinks);
  expect(cleared.linkGreyoutOpacity).toBe(before.linkGreyoutOpacity);
  expect(hoverCandidates).toEqual(expect.arrayContaining(neighborIds));
  expect(visibleHoverCandidates.some((id) => neighbors.has(Number(id.replace("node-", ""))))).toBe(true);
  expect(hoverLabelCount).toBeLessThanOrEqual(40);
  expect(hoverCandidates.length).toBeLessThanOrEqual(40);
  expect(clearedCandidates).toEqual(overviewCandidates);
  for (const snapshot of [hovered, cleared]) {
    expect(snapshot.positions).toEqual(before.positions);
    expect(snapshot.viewport).toEqual(before.viewport);
    expect(snapshot.geometryUploads).toBe(before.geometryUploads);
    expect(snapshot.running).toBe(false);
    expect(snapshot.pinned).toEqual(before.pinned);
    expect(snapshot.selectedId).toBe(before.selectedId);
    expect(snapshot.selections).toEqual(before.selections);
    expect(snapshot.glError).toBe(0);
  }
  expect(errors).toEqual([]);
});

for (const mobile of [false, true]) {
  test(`native 2k active full titles do not overprint adjacent selection and focus ${mobile ? "on mobile" : "on desktop"}`, async ({ page }, testInfo) => {
    await page.setViewportSize(mobile ? { width: 390, height: 600 } : { width: 900, height: 700 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=${mobile ? "dark" : "light"}&motion=normal&topology=links&interaction=targets&labels=boundary`);
    await page.waitForFunction(() => Boolean(window.__workspaceMapInteraction && window.__workspaceMapSmoke?.status === "ready"));
    await page.waitForFunction(() => document.getElementById("stage")?.dataset.mapCommunityLayoutStatus === "applied");
    const fit = await page.evaluate(async () => {
      const fixture = window.__workspaceMapInteraction!;
      const stage = document.getElementById("stage")!;
      fixture.stop();
      const fit = await fixture.fitAndWait();
      // Deliberate authored pins isolate label placement, not layout quality.
      // Use the acknowledged Fit's empty padding, not an unchecked dense area.
      // The same full native point/edge columns and three IDs remain mounted.
      fixture.pinAtScreen(0, [stage.clientWidth - 16, stage.clientHeight - 54]);
      fixture.pinAtScreen(1998, [stage.clientWidth - 16, stage.clientHeight - 39]);
      fixture.pinAtScreen(1999, [stage.clientWidth - 16, stage.clientHeight - 24]);
      return fit;
    });
    expect(fit.completed).toEqual(fit.requested);
    const viewport = page.viewportSize()!;
    const expectedCenters = new Map([[0, [viewport.width - 16, viewport.height - 54]],
      [1998, [viewport.width - 16, viewport.height - 39]], [1999, [viewport.width - 16, viewport.height - 24]]]);
    const pointerFrames: Awaited<ReturnType<NonNullable<Window["__workspaceMapInteraction"]>["boundaryPointFrame"]>>[] = [];
    const verifiedPointerFrame = async () => {
      const frame = await page.evaluate(() => window.__workspaceMapInteraction!.boundaryPointFrame([0, 1998, 1999]));
      pointerFrames.push(frame);
      expect(frame.nodeCount).toBe(2_000);
      expect(frame.targets.map(target => target.index)).toEqual([0, 1998, 1999]);
      for (const target of frame.targets) {
        const expected = expectedCenters.get(target.index)!;
        expect(target.screen.every(Number.isFinite)).toBe(true);
        expect(Math.abs(target.screen[0] - expected[0])).toBeLessThanOrEqual(.01);
        expect(Math.abs(target.screen[1] - expected[1])).toBeLessThanOrEqual(.01);
        expect(target.otherNodeClearance).toBeGreaterThanOrEqual(12);
      }
      return frame;
    };
    const selectedPoint = (await verifiedPointerFrame()).targets[0].screen;
    await page.mouse.click(selectedPoint[0], selectedPoint[1]);
    await expect.poll(() => page.evaluate(() => window.__workspaceMapInteraction!.snapshot().selectedId)).toBe("node-0");
    await page.evaluate(() => window.__workspaceMapInteraction!.focus("node-1999"));
    const baseline = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
    const hoveredPoint = (await verifiedPointerFrame()).targets[1].screen;
    await page.mouse.move(hoveredPoint[0], hoveredPoint[1]);
    await expect.poll(() => page.evaluate(() => window.__workspaceMapSmoke?.hoveredId)).toBe("node-1998");
    const labels = page.locator("[data-workspace-map-labels] span");
    await expect(labels.first()).toHaveAttribute("data-node-id", "node-1998");
    for (const id of ["node-1998", "node-1999", "node-0"]) {
      await expect(labels.locator(`xpath=self::*[@data-node-id='${id}']`)).toBeVisible();
    }
    const measured = await labels.evaluateAll(elements => elements.filter(element =>
      ["node-1998", "node-1999", "node-0"].includes((element as HTMLElement).dataset.nodeId ?? ""))
      .map(element => {
        const rectangle = element.getBoundingClientRect();
        const css = getComputedStyle(element);
        return { id: (element as HTMLElement).dataset.nodeId, title: element.textContent,
          full: (element as HTMLElement).dataset.fullTitle, x: rectangle.x, y: rectangle.y,
          right: rectangle.right, bottom: rectangle.bottom, width: rectangle.width, height: rectangle.height,
          background: css.backgroundColor, pointerEvents: css.pointerEvents };
      }));
    expect(measured).toHaveLength(3);
    for (let left = 0; left < measured.length; left++) {
      const label = measured[left];
      expect(label.full).toBe("true");
      expect(label.width).toBeGreaterThan(0);
      expect(label.height).toBeGreaterThan(0);
      expect(label.background).not.toBe("rgba(0, 0, 0, 0)");
      expect(label.pointerEvents).toBe("none");
      expect(label.x).toBeGreaterThanOrEqual(0);
      expect(label.y).toBeGreaterThanOrEqual(0);
      expect(label.right).toBeLessThanOrEqual(mobile ? 390 : 900);
      expect(label.bottom).toBeLessThanOrEqual(mobile ? 600 : 700);
      for (let right = left + 1; right < measured.length; right++) {
        const other = measured[right];
        expect(label.x < other.right && label.right > other.x
          && label.y < other.bottom && label.bottom > other.y).toBe(false);
      }
    }
    const after = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
    expect(after.canonicalPositions).toEqual(baseline.canonicalPositions);
    expect(after.viewport).toEqual(baseline.viewport);
    expect(after.geometryUploads).toBe(baseline.geometryUploads);
    expect(after.topologyUploads).toBe(baseline.topologyUploads);
    expect(after.pinned).toEqual(baseline.pinned);
    expect(after.forceConfig).toEqual(baseline.forceConfig);
    expect(after.glError).toBe(0);
    expect(await labels.count()).toBe(40);
    expect(errors).toEqual([]);
    const evidence = JSON.stringify({ measured, fit, pointerFrames, nodeCount: baseline.positions.length / 2,
      edgeCount: baseline.uploadedRelationCount,
      scope: "Real native pointer selection/hover with authored adjacent pins; label UX, not solver or real-device performance" }, null, 2);
    await writeFile(testInfo.outputPath("active-title-rectangles.json"), evidence);
    await testInfo.attach("active-title-rectangles", { contentType: "application/json", body: evidence });
    await evidenceScreenshot(page, testInfo, `map-2k-active-titles-${mobile ? "mobile-dark" : "desktop-light"}.png`);
  });
}

test("native 2k first boundary-label placement contains Korean and newly admitted multiline titles", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 600 });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets&labels=boundary`);
  await page.waitForFunction(() => Boolean(window.__workspaceMapSmoke && window.__workspaceMapSmoke.status !== "loading"));
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  await page.waitForFunction(() => document.getElementById("stage")?.dataset.mapCommunityLayoutStatus === "applied");
  expect(await page.locator("#stage").getAttribute("data-map-physics")).toBe("force-atlas2-worker");
  await page.mouse.move(0, 0);
  const result = await page.evaluate(async () => {
    const fixture = window.__workspaceMapInteraction!;
    const stage = document.getElementById("stage")!;
    const layer = stage.querySelector<HTMLElement>("[data-workspace-map-labels]")!;
    fixture.stop();
    fixture.fit();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const initialCandidates = Array.from(layer.querySelectorAll<HTMLElement>("span"))
      .map(label => label.dataset.nodeId).filter(Boolean);
    // Explicit user pins exercise viewport bounds, not seed quality or
    // density. All comparisons below start after those deliberate pin edits.
    fixture.pinAtScreen(1998, [stage.clientWidth - 20, stage.clientHeight - 12]);
    fixture.pinAtScreen(1999, [stage.clientWidth - 32, stage.clientHeight - 10]);
    const before = fixture.snapshot();
    const firstPlacements = [];
    for (const [id, title] of [
      ["node-1998", "지식관계분석문서제목"],
      ["node-1999", "A newly focused document outside the overview top forty must show its complete multiline title, including this final sentence at the bottom right boundary."],
    ]) {
      firstPlacements.push(await new Promise<{
        id: string; title: string; text: string | null; width: number; height: number;
        left: number; top: number; right: number; bottom: number;
        stageLeft: number; stageTop: number; stageRight: number; stageBottom: number;
        candidates: string[];
      }>((resolve, reject) => {
        const timeout = setTimeout(() => {
          observer.disconnect();
          reject(new Error(`No first full-title placement for ${id}`));
        }, 2000);
        const observer = new MutationObserver(() => {
          const label = layer.querySelector<HTMLElement>(`span[data-node-id="${id}"]`);
          if (!label || label.dataset.fullTitle !== "true" || getComputedStyle(label).display === "none"
            || getComputedStyle(label).visibility === "hidden") return;
          // Observe the first visible DOM commit, before the next 200ms
          // refresh can repair an initially hidden label's zero-height read.
          const rect = label.getBoundingClientRect(), bounds = stage.getBoundingClientRect();
          observer.disconnect();
          clearTimeout(timeout);
          resolve({ id, title, text: label.textContent, width: rect.width, height: rect.height,
            left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
            stageLeft: bounds.left, stageTop: bounds.top, stageRight: bounds.right, stageBottom: bounds.bottom,
            candidates: Array.from(layer.querySelectorAll<HTMLElement>("span"))
              .map(candidate => candidate.dataset.nodeId ?? "").filter(Boolean) });
        });
        observer.observe(layer, { subtree: true, attributes: true, childList: true, characterData: true });
        fixture.focus(id);
      }));
    }
    return { before, after: fixture.snapshot(), initialCandidates, firstPlacements };
  });
  await testInfo.attach("first-boundary-label-placement.json", { contentType: "application/json", body: JSON.stringify({
    ...result,
    scope: "First native 2K label DOM placement after explicit boundary pins; not force, density, real-API or device-performance acceptance",
  }, null, 2) });
  await evidenceScreenshot(page, testInfo, "map-2k-first-boundary-label-placement.png");
  expect(result.initialCandidates).toHaveLength(40);
  expect(result.initialCandidates).not.toContain("node-1998");
  expect(result.initialCandidates).not.toContain("node-1999");
  expect(result.firstPlacements).toHaveLength(2);
  // Ensure the fixture genuinely exercises CJK width beyond the former
  // seven-pixel estimate and a newly admitted title with multiple lines.
  expect(result.firstPlacements[0].width).toBeGreaterThan(result.firstPlacements[0].title.length * 7 + 4);
  expect(result.firstPlacements[1].height).toBeGreaterThan(18);
  for (const placement of result.firstPlacements) {
    expect(placement.text).toBe(placement.title);
    expect(placement.candidates[0]).toBe(placement.id);
    expect(placement.candidates).toHaveLength(40);
    expect(placement.left).toBeGreaterThanOrEqual(placement.stageLeft);
    expect(placement.top).toBeGreaterThanOrEqual(placement.stageTop);
    expect(placement.right).toBeLessThanOrEqual(placement.stageRight);
    expect(placement.bottom).toBeLessThanOrEqual(placement.stageBottom);
  }
  expect(result.before.positions).toHaveLength(4000);
  expect(result.before.pinned).toEqual([1998, 1999]);
  expect(result.after.positions).toEqual(result.before.positions);
  expect(result.after.canonicalPositions).toEqual(result.before.canonicalPositions);
  expect(result.after.viewport).toEqual(result.before.viewport);
  expect(result.after.geometryUploads).toBe(result.before.geometryUploads);
  expect(result.after.pinned).toEqual(result.before.pinned);
  expect(result.after.forceConfig).toEqual(result.before.forceConfig);
  expect(result.after.forceCommits).toBe(result.before.forceCommits);
  expect(result.after.collisionCommits).toBe(result.before.collisionCommits);
  expect(result.after.running).toBe(false);
  expect(result.after.selectedId).toBe(result.before.selectedId);
  expect(result.after.selections).toEqual(result.before.selections);
  expect(result.after.dragPins).toEqual(result.before.dragPins);
  expect(result.after.glError).toBe(0);
  expect(errors).toEqual([]);
});

test("2k Map touch targets, zoom direction/growth, full focused title and background clear preserve pins", async ({ browser }, testInfo) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true });
  const page = await context.newPage();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => Boolean(window.__workspaceMapSmoke && window.__workspaceMapSmoke.status !== "loading"));
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  // The relation-first 2K map is live by default. Pause it explicitly when
  // testing camera-only transforms; elapsed physics is not a zoom relayout.
  await page.waitForFunction(() => {
    const next = window.__workspaceMapInteraction!.snapshot();
    return next.layoutRunning && next.layoutTicks >= 3 && !next.running;
  });
  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const fit = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(fit.positions).toHaveLength(4000);
  expect(Number.isFinite(fit.viewport!.zoom)).toBe(true);
  expect(fit.viewport!.zoom).toBeGreaterThan(0);
  await page.evaluate(async () => {
    window.__workspaceMapInteraction!.zoom(1 / 1.2);
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  const out = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(out.viewport!.zoom).toBeLessThan(fit.viewport!.zoom);
  expect(out.pointSizeScale).toBeCloseTo(Math.sqrt(1 / 1.2));
  await page.evaluate(async () => {
    window.__workspaceMapInteraction!.fit();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    window.__workspaceMapInteraction!.zoom(4);
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  const zoom = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(zoom.pointSizeScale).toBeCloseTo(2);
  expect(zoom.positions).toEqual(fit.positions);
  await page.evaluate(() => window.__workspaceMapInteraction!.zoom(100));
  expect(await page.evaluate(() => window.__workspaceMapInteraction!.snapshot().pointSizeScale)).toBe(4);
  await page.evaluate(async () => {
    window.__workspaceMapInteraction!.fit();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    window.__workspaceMapInteraction!.isolate();
  });
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const target = await page.evaluate(() => window.__workspaceMapInteraction!.point(0));
  // Native dot is roughly 3px; tap 10px away exercises the >=12px fallback.
  await page.touchscreen.tap(target[0] + 10, target[1]);
  expect(await page.evaluate(() => window.__workspaceMapInteraction!.snapshot().selectedId)).toBe("node-0");
  const focused = page.locator('[data-workspace-map-labels] span[data-node-id="node-0"]');
  await expect(focused).toHaveText("A long document title whose complete ending must remain visible on focus");
  await expect(focused).toBeVisible();
  const titleBounds = await focused.boundingBox();
  expect(titleBounds!.x + titleBounds!.width).toBeLessThanOrEqual(390);
  const selected = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  await evidenceScreenshot(page, testInfo, "map-2k-mobile-touch-full-title.png");
  const touch = await context.newCDPSession(page);
  await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: 10, y: 760 }] });
  await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: 40, y: 780 }] });
  await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  const panned = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(panned.viewport!.pan).not.toEqual(selected.viewport!.pan);
  expect(panned.selections).toEqual(selected.selections);
  expect(panned.positions).toEqual(selected.positions);
  await page.touchscreen.tap(10, 820);
  const cleared = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(cleared.selectedId).toBeNull();
  expect(cleared.positions).toEqual(selected.positions);
  expect(cleared.viewport).toEqual(panned.viewport);
  expect(cleared.pinned).toContain(0);
  expect(cleared.glError).toBe(0);
  // Resume keeps the original pin while restarting real 2K relation physics.
  await page.evaluate(() => window.__workspaceMapInteraction!.resume());
  await page.waitForFunction((positions) => {
    const next = window.__workspaceMapInteraction!.snapshot();
    return next.layoutRunning && !next.running && next.positions.some((value, index) => index > 1
      && Math.abs(value - positions[index]) > 0.01);
  }, cleared.positions);
  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const resumed = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(resumed.positions[0]).toBeCloseTo(cleared.positions[0], 2);
  expect(resumed.positions[1]).toBeCloseTo(cleared.positions[1], 2);
  expect(resumed.pinned).toContain(0);
  expect(errors).toEqual([]);
  await testInfo.attach("map-interactions", { contentType: "application/json", body: JSON.stringify({
    fitZoom: fit.viewport!.zoom, outZoom: out.viewport!.zoom,
    growth: zoom.pointSizeScale, tappedOffset: 10, pinned: cleared.pinned,
    glError: cleared.glError, errors,
    scope: "Synthetic actual Cosmos/WebGL2 2K fixture, not real-API acceptance or GPU performance",
  }) });
  await context.close();
});

for (const count of [40, 2000]) {
  test(`native cooled ${count}-node Map resume moves the layout but never overrides reduced motion`, async ({ page }) => {
    await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=${count}&theme=light&motion=normal&topology=links&interaction=targets`);
    await page.waitForFunction(() => Boolean(window.__workspaceMapSmoke && window.__workspaceMapSmoke.status !== "loading"));
    expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
    const before = await page.evaluate(() => { window.__workspaceMapInteraction!.stop(); return window.__workspaceMapInteraction!.snapshot(); });
    expect(before.running).toBe(false);
    await page.evaluate(() => window.__workspaceMapInteraction!.resume());
    await page.waitForFunction((positions) => window.__workspaceMapInteraction!.snapshot().positions.some((position, index) =>
      Math.abs(position - positions[index]) > 0.01), before.positions);
    const live = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
    expect(live.layoutRunning).toBe(true);
    expect(live.layoutTicks).toBeGreaterThan(before.layoutTicks);
    expect(live.running).toBe(false);
    expect(live.topologyUploads).toBe(before.topologyUploads);
    await page.evaluate(() => { window.__workspaceMapInteraction!.reduce(true); window.__workspaceMapInteraction!.stop(); });
    const reduced = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
    await page.evaluate(() => window.__workspaceMapInteraction!.resume());
    const after = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
    expect(after.running).toBe(false);
    expect(after.layoutRunning).toBe(false);
    expect(after.positions).toEqual(reduced.positions);
    await page.waitForTimeout(250);
    expect(await page.evaluate(() => window.__workspaceMapInteraction!.snapshot().positions)).toEqual(reduced.positions);
    expect(after.glError).toBe(0);
  });
}

test("native 2k relation layout remains live after five seconds and explicit pause freezes every point", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => Boolean(window.__workspaceMapSmoke && window.__workspaceMapSmoke.status !== "loading"), { timeout: 30_000 });
  expect(await page.evaluate(() => window.__workspaceMapSmoke?.status)).toBe("ready");
  // This elapsed-time check is the original2K lifecycle contract, not a FPS
  // budget or target-GPU benchmark. Real CPU forces and GPU drawing run throughout.
  await page.waitForTimeout(5_100);
  const before = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(before.running).toBe(false);
  expect(before.layoutRunning).toBe(true);
  await page.waitForFunction((positions) => window.__workspaceMapInteraction!.snapshot().positions.some((value, index) =>
    Math.abs(value - positions[index]) > 0.01), before.positions, { timeout: 5_000 });
  await page.evaluate(() => window.__workspaceMapInteraction!.stop());
  const paused = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  await page.waitForTimeout(250);
  const after = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(after.running).toBe(false);
  expect(after.layoutRunning).toBe(false);
  expect(after.topologyUploads).toBe(before.topologyUploads);
  expect(after.positions).toHaveLength(4000);
  expect(after.positions).toEqual(paused.positions);
  expect(after.glError).toBe(0);
  expect(errors).toEqual([]);
  await testInfo.attach("native-2k-five-second-lifecycle", { contentType: "application/json", body: JSON.stringify({
    elapsedBeforeReadMs: 5100, liveAfterFiveSeconds: before.layoutRunning, nativeGpuRunning: before.running,
    layoutTicks: before.layoutTicks, pausedPoints: after.positions.length / 2,
    stablePausedCoordinates: true, glError: after.glError,
    scope: "Synthetic original2K corpus with official CPU Worker and actual Cosmos GPU drawing; not target-hardware performance acceptance",
  }) });
});

test("native 2k initial framing ends at the published contact boundary without changing physics or rearming on Resume", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets`);
  await page.waitForFunction(() => window.__workspaceMapInteraction?.initialCamera().fits.length);
  const first = await page.evaluate(() => window.__workspaceMapInteraction!.initialCamera().fits[0]);
  await page.waitForFunction(() => document.getElementById("stage")?.dataset.mapLayoutPhase === "contact", null, { timeout: 45_000 });
  // The final duration=0 canonical Fit is still a queued native D3 transition.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const expired = await page.evaluate(() => window.__workspaceMapInteraction!.initialCamera());
  expect(expired.fits.length).toBeGreaterThan(0);
  expect(expired.fits.length).toBeLessThanOrEqual(60);
  expect(expired.fits.every((fit) => fit.points === 2000 && fit.at - first.at < 30_000)).toBe(true);
  expect(expired.contactCanonicalFits).toHaveLength(1);
  expect(expired.contactCanonicalFits[0].points).toBe(2000);
  await expect(page.locator("#stage")).toHaveAttribute("data-map-layout-phase", "contact");
  expect(Number(await page.locator("#stage").getAttribute("data-map-layout-iterations"))).toBeGreaterThanOrEqual(32);
  await page.waitForTimeout(250);
  expect(await page.evaluate(() => window.__workspaceMapInteraction!.initialCamera().fits)).toEqual(expired.fits);
  expect(await page.evaluate(() => window.__workspaceMapInteraction!.initialCamera().contactCanonicalFits)).toEqual(expired.contactCanonicalFits);
  const before = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(before.running).toBe(false);
  expect(before.layoutRunning).toBe(true);
  expect(before.collisionCommits).toBe(2);
  expect(before.forceCommits).toBe(0);
  await evidenceScreenshot(page, testInfo, "initial-2k-framing.png");
  await page.evaluate(() => { window.__workspaceMapInteraction!.stop(); window.__workspaceMapInteraction!.resume(); });
  await page.waitForTimeout(250);
  expect(await page.evaluate(() => window.__workspaceMapInteraction!.initialCamera().fits)).toEqual(expired.fits);
  expect(await page.evaluate(() => window.__workspaceMapInteraction!.initialCamera().contactCanonicalFits)).toEqual(expired.contactCanonicalFits);
  expect(before.glError).toBe(0);
  expect(errors).toEqual([]);
  const report = testInfo.outputPath("bounded-initial-camera.json");
  await writeFile(report, JSON.stringify({
    fits: expired.fits, contactCanonicalFits: expired.contactCanonicalFits,
    viewportEvents: expired.viewportEvents, forceCommits: before.forceCommits,
    collisionCommits: before.collisionCommits, errors,
    scope: "Actual CPU structure/contact and native D3/Cosmos 2K fixture; bounded camera ownership, not convergence or target-device performance acceptance",
  }, null, 2));
  await testInfo.attach("bounded-initial-camera", { contentType: "application/json", path: report });
});

test("PC-native diagnostic focus cancels an already-submitted initial Fit without camera movement or persistence echoes", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/src/features/graph/workspace/tests/browserSmoke.html?geometry=2000&theme=light&motion=normal&topology=links&interaction=targets&camera=cancel-focus&budget=pc10k`);
  await page.waitForFunction(() => window.__workspaceMapInteraction?.initialCamera().cancellation);
  const observed = await page.evaluate(() => window.__workspaceMapInteraction!.initialCamera());
  const cancelled = observed.cancellation!;
  expect(cancelled.echoes).toBe(0);
  expect(cancelled.after).toEqual(cancelled.before);
  expect(cancelled.positionsAfter).toEqual(cancelled.positionsBefore);
  await page.waitForTimeout(300);
  const after = await page.evaluate(() => window.__workspaceMapInteraction!.snapshot());
  expect(after.viewport).toEqual(cancelled.before);
  expect(after.positions).toEqual(cancelled.positionsBefore);
  expect(await page.evaluate(() => window.__workspaceMapInteraction!.initialCamera().fits)).toEqual(observed.fits);
  expect(after.glError).toBe(0);
  expect(errors).toEqual([]);
  const report = testInfo.outputPath("submitted-fit-focus-cancellation.json");
  await writeFile(report, JSON.stringify({
    before: cancelled.before, after: after.viewport, pointsPreserved: after.positions.length / 2,
    echoes: cancelled.echoes, fits: observed.fits, glError: after.glError, errors,
    scope: "Explicit PC-native budget diagnostic with 2K input; real queued native D3 transition canceled through public Cosmos API. Not default CPU-layout acceptance or simulated camera mock",
  }, null, 2));
  await testInfo.attach("submitted-fit-focus-cancellation", { contentType: "application/json", path: report });
});

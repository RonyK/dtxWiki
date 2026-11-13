import { expect, test, type Locator, type Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";

import { graphFixture } from "../../frontend/src/features/graph/tests/graphFixture";
import { evidenceScreenshot } from "../e2e/evidence";
import { fixtureServerConfig } from "./vite-fixture";

const fixtureWorkspaceId = "00000000-0000-4000-8000-000000000001";
const fixtureNodes = graphFixture().nodes;
const seedEntityId = fixtureNodes.find((node) => node.id === "node:seed")!.entity_id;
const relatedEntityId = fixtureNodes.find((node) => node.id === "node:related")!.entity_id;

let server: ViteDevServer;
let origin: string;
test.beforeAll(async () => {
  server = await createServer({
    ...fixtureServerConfig,
    server: { host: "127.0.0.1", port: 0, strictPort: false, fs: { allow: [path.resolve(".")] } },
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("GraphView fixture server did not bind TCP");
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => { await server?.close(); });

const copy = {
  en: {
    folder: "Some folders are not shown.", folderChip: "Some folders omitted", coverage: "This graph has bounded or partial coverage.",
    history: "Timeline coverage is limited.", summary: "What was left out",
    folderDetail: "Folder grouping limit · Distance 1",
    quotaDetail: "Relationship display limit · Between distance 1 and 2",
    sourceDetail: "Expansion inspection limit · Distance 2",
    budget: "limited to the requested display budget",
    source: "The current graph inspection reached a source limit",
    historyDetail: "Timeline coverage is partial.",
    observationSource: "Observation inspection reached a source limit; current relationships may be missing.",
    usageDisabled: "Read-together relationships are turned off for this workspace.",
  },
  ko: {
    folder: "일부 폴더를 표시하지 않습니다.", folderChip: "일부 폴더 생략", coverage: "이 그래프의 범위가 제한되었거나 일부만 포함합니다.",
    history: "타임라인 범위가 제한되었습니다.", summary: "제외된 항목의 사유",
    folderDetail: "폴더 묶음 한도 · 거리 1",
    quotaDetail: "관계 표시 한도 · 거리 1과 2 사이",
    sourceDetail: "확장 범위 확인 한도 · 거리 2",
    budget: "요청한 표시 한도에 맞게 그래프 범위를 제한했습니다",
    source: "현재 그래프의 원본 확인 한도에 도달",
    historyDetail: "타임라인 범위가 일부만 포함됩니다.",
    observationSource: "관찰 기록 확인 한도에 도달하여 현재 관계가 누락될 수 있습니다.",
    usageDisabled: "이 워크스페이스에서 함께 읽음 관계가 꺼져 있습니다.",
  },
} as const;

// Layout, focus and native state have desktop and mobile paths. Palette and
// translated text do not need their full Cartesian product for each path.
const LOCAL_LAYOUT_CASES = [
  { locale: "en", mode: "light", viewport: { width: 1440, height: 900 } },
  { locale: "ko", mode: "dark", viewport: { width: 390, height: 844 } },
] as const;

// GraphView unit tests own every omission reason. Browser checks retain the
// three rendered forms: informational disclosure, warning disclosure and alert.
const COVERAGE_LAYOUT_CASES = [
  { locale: "en", scenario: "folder" },
  { locale: "ko", scenario: "quota" },
  { locale: "ko", scenario: "observation-source" },
] as const;

async function zoomAndPanLocalGraph(page: Page) {
  const initial = await page.evaluate(() => window.__localCoverageFixture.read());
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  const bounds = await page.getByTestId("graph-canvas").boundingBox();
  if (!bounds) throw new Error("The Local Graph canvas has no bounds");
  // Real stage input, away from the three document glyphs and below the seed
  // pill, a button over the stage's top-left corner. Observe native camera
  // animation completion instead of guessing a drag-inertia delay.
  await page.mouse.move(bounds.x + 35, bounds.y + 80);
  await page.mouse.down();
  await page.mouse.move(bounds.x + 73, bounds.y + 106, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => page.evaluate(() => window.__localCoverageFixture.cameraAnimated())).toBe(false);
  const moved = await page.evaluate(() => window.__localCoverageFixture.read());
  expect(moved.instances).toBe(initial.instances);
  expect(moved.camera.ratio).toBeCloseTo(initial.camera.ratio / 1.44, 12);
  expect([moved.camera.x, moved.camera.y]).not.toEqual([initial.camera.x, initial.camera.y]);
  return moved;
}

for (const rebuild of ["theme", "forced-colors", "reduced-motion", "context-restored"] as const) {
  test(`Local GraphView retains native zoom and pan through a ${rebuild} rebuild`, async ({ page }, testInfo) => {
    const errors = await mount(page, "camera-rebuild", "en", "light", { width: 1440, height: 900 });
    const before = await zoomAndPanLocalGraph(page);
    if (rebuild === "theme") {
      await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
    } else if (rebuild === "forced-colors") {
      await page.emulateMedia({ forcedColors: "active" });
    } else if (rebuild === "reduced-motion") {
      await page.emulateMedia({ reducedMotion: "no-preference" });
    } else {
      await page.evaluate(() => window.__localCoverageFixture.loseContext());
      await expect(page.locator(".graph-canvas-frame")).toHaveAttribute("data-renderer-status", "error");
      await page.evaluate(() => window.__localCoverageFixture.restoreContext());
    }
    await expect.poll(() => page.evaluate(() => window.__localCoverageFixture.read().instances)).toBe(before.instances + 1);
    await expect(page.locator(".graph-canvas-frame")).toHaveAttribute("data-renderer-status", "ready");
    const after = await page.evaluate(() => window.__localCoverageFixture.read());
    expect(after.camera).toEqual(before.camera);
    expect(errors).toEqual([]);
    await testInfo.attach("local-native-camera-rebuild", { contentType: "application/json",
      body: JSON.stringify({ rebuild, before, after }, null, 2) });
  });
}

test("Local GraphView keeps its native camera and engine while Dialog locks and unlocks body scrolling", async ({ page }, testInfo) => {
  let bodyFailed = false;
  try {
    const errors = await mount(page, "camera-rebuild", "en", "light", { width: 1440, height: 900 });
    const before = await zoomAndPanLocalGraph(page);
    const native = await page.locator("canvas.sigma-nodes").elementHandle();
    await page.getByRole("button", { name: "Open camera fixture dialog", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Camera fixture dialog", exact: true })).toBeVisible();
    await expect.poll(() => page.evaluate(() => document.body.style.overflow)).toBe("hidden");
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(before);
    await page.getByRole("button", { name: "Close camera fixture dialog", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => document.body.style.overflow)).toBe("");
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    const after = await page.evaluate(() => window.__localCoverageFixture.read());
    expect(after).toEqual(before);
    expect(await native!.evaluate((element) => element === document.querySelector("canvas.sigma-nodes"))).toBe(true);
    expect(errors).toEqual([]);
    await testInfo.attach("local-native-dialog-camera", { contentType: "application/json",
      body: JSON.stringify({ before, after }, null, 2) });
  } catch (error) { bodyFailed = true; throw error; }
  finally {
    try {
      const diagnostics = await page.evaluate(() => window.__localCoverageFixture?.cameraDiagnostics() ?? { unavailable: true });
      const diagnosticPath = testInfo.outputPath("local-dialog-timeline.json");
      await writeFile(diagnosticPath, JSON.stringify(diagnostics, null, 2));
      await testInfo.attach("local-dialog-timeline", { path: diagnosticPath, contentType: "application/json" });
    } catch (error) { if (!bodyFailed) throw error; }
  }
});

for (const { locale, scenario } of COVERAGE_LAYOUT_CASES) {
  test(`Local ${locale} ${scenario} coverage explanation stays readable at 390px with actual Sigma paint`, async ({ page }, testInfo) => {
    const errors = await mount(page, scenario, locale);
    const words = copy[locale];
    const heading = scenario === "folder" ? words.folder : words.coverage;
    const banner = page.getByRole(scenario === "folder" ? "status" : "alert");
    await expect(banner).toHaveAttribute("role", scenario === "folder" ? "status" : "alert");
    await expect(banner).toContainText(heading);
    if (scenario !== "folder") {
      await expect(banner).toBeVisible();
      await expect(page.locator(".graph-canvas-coverage .graph-local-coverage-info")).toHaveCount(1);
    }
    if (scenario !== "quota") {
      await expect(page.getByText(words.budget, { exact: false })).toHaveCount(0);
    }
    const summary = page.locator(".graph-canvas-coverage .graph-local-coverage-info > summary");
    const details = summary.locator("..");
    await expect(details).not.toHaveAttribute("open");
    await summary.click();
    await expect(details).toHaveAttribute("open");
    if (scenario === "observation-source") {
      await expect(banner).toContainText(words.observationSource);
      await expect(page.getByText(words.history, { exact: true })).toHaveCount(0);
    } else {
      await expect(details).toContainText(scenario === "folder" ? words.folderDetail : words.quotaDetail);
      await expect(details).not.toContainText(/folder_node|node_quota|frontier_cap|relation_source_cap|h12/);
    }
    const visibleNotice = page.locator(".graph-local-coverage-panel");
    await expectCoveragePanelFits(page, visibleNotice, { width: 390, height: 844 });
    const bounds = await visibleNotice.evaluate((element) => {
      const rectangle = element.getBoundingClientRect();
      return { left: rectangle.left, right: rectangle.right, width: window.innerWidth };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(0);
    expect(bounds.right).toBeLessThanOrEqual(bounds.width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    expect(await purpleNodePixels(page)).toBeGreaterThan(5);
    await page.evaluate(() => window.scrollTo(0, 0));
    await evidenceScreenshot(page, testInfo, `local-${locale}-${scenario}-390px.png`);
    expect(errors).toEqual([]);
  });
}

for (const { locale, mode, viewport } of LOCAL_LAYOUT_CASES) {
  test(`Local ${locale} ${mode} folder info at ${viewport.width}px preserves native selection, pins and camera`, async ({ page }, testInfo) => {
    const errors = await mount(page, "folder", locale, mode, viewport);
    const graph = page.getByTestId("graph-view");
    const summary = graph.getByText(copy[locale].folderChip, { exact: true });
    const disclosure = summary.locator("..");
    const panel = graph.locator(".graph-local-coverage-panel");
    await expect(graph.locator(".status-banner")).toHaveCount(0);
    await expect(graph.getByRole("status")).toContainText(copy[locale].folder);
    await expect(graph.locator(".graph-filter-bar")).toHaveCount(1);
    await expect(graph.locator(".graph-canvas-coverage .graph-local-coverage-info")).toHaveCount(1);
    const folderTop = await graph.evaluate((element) => element.querySelector(".graph-local-stage")!.getBoundingClientRect().top - element.getBoundingClientRect().top);
    // A DOM list selection (the seed pill), then back to the painted canvas
    // that owns the coverage chip, as before the list moved into the frame.
    await (await openList(page)).locator('button[data-local-graph-id="node:seed"]').click();
    const details = graph.locator(".graph-details");
    await expect(details.locator("h5.graph-details-title")).toHaveText("Decision Log");
    await (await localDetailsActions(details)).getByRole("button", { name: locale === "en" ? "Pin" : "고정", exact: true }).click();
    const unpin = details.getByRole("button", { name: locale === "en" ? "Unpin" : "고정 해제", exact: true, includeHidden: true });
    await expect(unpin).toHaveCount(1);
    await showGraph(page);
    const before = await page.evaluate(() => window.__localCoverageFixture.read());
    const nativeCanvas = await graph.locator("canvas.sigma-nodes").elementHandle();
    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(disclosure).toHaveAttribute("open");
    await expect(panel).toBeVisible();
    const bounds = await summary.boundingBox();
    expect(bounds!.height).toBeGreaterThanOrEqual(viewport.width <= 760 ? 44 : 22);
    await expectCoveragePanelFits(page, panel, viewport);
    await evidenceScreenshot(page, testInfo, `local-${locale}-${mode}-${viewport.width}-folder-info-open.png`);
    await page.keyboard.press("Escape");
    await expect(disclosure).not.toHaveAttribute("open");
    await expect(summary).toBeFocused();
    await expect(details.locator("h5.graph-details-title")).toHaveText("Decision Log");
    await expect(unpin).toHaveCount(1);
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(before);
    await page.keyboard.press("Space");
    await expect(disclosure).toHaveAttribute("open");
    await graph.locator(".graph-frame-header .graph-view-options > summary").click();
    const labels = graph.getByRole("checkbox", { name: locale === "en" ? "Show labels" : "레이블 표시", exact: true });
    await labels.uncheck();
    await expect(disclosure).not.toHaveAttribute("open");
    await expect(labels).toBeFocused();
    await expect(labels).not.toBeChecked();
    await expect(details.locator("h5.graph-details-title")).toHaveText("Decision Log");
    await expect(unpin).toHaveCount(1);
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(before);
    expect(await nativeCanvas!.evaluate((element) => element === document.querySelector("canvas.sigma-nodes"))).toBe(true);
    await graph.locator(".graph-frame-header .graph-view-options > summary").click();
    await summary.click();
    await page.evaluate(() => window.__localCoverageFixture.replacePayload());
    await expect(disclosure).not.toHaveAttribute("open");
    await summary.click();
    await page.evaluate(() => window.__localCoverageFixture.activate(false));
    await expect(disclosure).not.toHaveAttribute("open");
    await page.evaluate(() => window.__localCoverageFixture.activate(true));
    await summary.focus();
    await page.keyboard.press("Escape");
    await expect(details.locator("h5.graph-details-title")).toHaveCount(0);
    await expect(graph).toBeFocused();
    await evidenceScreenshot(page, testInfo, `local-${locale}-${mode}-${viewport.width}-folder-info.png`);
    await mount(page, "complete", locale, mode, viewport);
    const completeTop = await page.getByTestId("graph-view").evaluate((element) => element.querySelector(".graph-local-stage")!.getBoundingClientRect().top - element.getBoundingClientRect().top);
    // The only possible extra space is one wrapped 44px toolbar chip.
    expect(folderTop - completeTop).toBeLessThanOrEqual(50);
    expect(errors).toEqual([]);
  });
}

// Scrolling is independent of palette and locale; retain the longer Korean copy.
test("Local ko folder explanations scroll inside a 390x640 viewport", async ({ page }, testInfo) => {
  const locale = "ko" as const;
  const viewport = { width: 390, height: 640 };
  const errors = await mount(page, "folder-long", locale, "light", viewport);
  const summary = page.getByText(copy[locale].folderChip, { exact: true });
  await summary.click();
  const panel = page.locator(".graph-local-coverage-panel");
  await expectCoveragePanelFits(page, panel, viewport);
  await expect(panel).toContainText(copy[locale].usageDisabled);
  await expect(page.getByRole("status")).toContainText(copy[locale].usageDisabled);
  const sizes = await panel.evaluate((element) => ({ client: element.clientHeight, scroll: element.scrollHeight }));
  expect(sizes.scroll).toBeGreaterThan(sizes.client);
  await evidenceScreenshot(page, testInfo, `local-${locale}-folder-long-open-390x640.png`);
  await panel.focus();
  await page.keyboard.press("End");
  await expect.poll(() => panel.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  await page.keyboard.press("Escape");
  await expect(summary).toBeFocused();
  await expect(summary.locator("..")).not.toHaveAttribute("open");
  await evidenceScreenshot(page, testInfo, `local-${locale}-folder-long-390x640.png`);
  expect(errors).toEqual([]);
});

test("Local folder info stays painted inside a narrow reader on a wide viewport", async ({ page }, testInfo) => {
  const viewport = { width: 1440, height: 900 };
  const errors = await mount(page, "folder-long", "en", "light", viewport, 320);
  const summary = page.getByText(copy.en.folderChip, { exact: true });
  const panel = page.locator(".graph-local-coverage-panel");
  // Open and measure in one browser task, before the native queued toggle
  // event. The popup must already fit its reader on the first visible frame.
  const geometry = await summary.evaluate((element) => {
    (element as HTMLElement).click();
    const panel = element.parentElement!.querySelector(".graph-local-coverage-panel")!;
    const popup = panel.getBoundingClientRect();
    const owner = element.closest(".graph-view")!.getBoundingClientRect();
    const points = [[popup.left + 4, popup.top + 4], [popup.right - 4, popup.bottom - 4]];
    return { left: popup.left, right: popup.right, top: popup.top, bottom: popup.bottom,
      owner: { left: owner.left, right: owner.right, top: owner.top, bottom: owner.bottom },
      painted: points.every(([x, y]) => panel.contains(document.elementFromPoint(x, y))) };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(geometry.owner.left);
  expect(geometry.right).toBeLessThanOrEqual(geometry.owner.right);
  expect(geometry.top).toBeGreaterThanOrEqual(geometry.owner.top);
  expect(geometry.bottom).toBeLessThanOrEqual(geometry.owner.bottom);
  expect(geometry.painted).toBe(true);
  await expectCoveragePanelFits(page, panel, viewport);
  await evidenceScreenshot(page, testInfo, "local-folder-info-narrow-reader-open.png");
  expect(errors).toEqual([]);
});

for (const { locale, mode, viewport } of LOCAL_LAYOUT_CASES) {
  test(`Local ${locale} ${mode} details at ${viewport.width}px keep a stable full-width native canvas`, async ({ page }, testInfo) => {
    const errors = await mount(page, "complete", locale, mode, viewport);
    const graph = page.getByTestId("graph-view");
    const details = graph.locator(".graph-details");
    const idle = await localStageGeometry(graph);
    await expect(details).toBeHidden();
    await expect(graph.getByRole("complementary")).toHaveCount(0);
    expect(idle.frame).toEqual(idle.stage);
    const initial = await page.evaluate(() => window.__localCoverageFixture.read());
    const native = await graph.locator("canvas.sigma-nodes").elementHandle();
    await evidenceScreenshot(page, testInfo, `local-${locale}-${mode}-${viewport.width}-details-idle.png`);

    const seed = await page.evaluate(() => window.__localCoverageFixture.point("node:seed"));
    await page.mouse.click(seed.x, seed.y);
    await expect(details.locator("h5.graph-details-title")).toHaveText("Decision Log");
    await expect(graph).toBeFocused();
    await expectLocalDetailsBounds(graph, viewport.width > 760 ? "drawer" : "below");
    expect(await localStageGeometry(graph)).toEqual(idle);
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(initial);
    await expect(graph.getByRole("dialog")).toHaveCount(0);
    await expect(graph.locator("[aria-modal='true'], [inert]")).toHaveCount(0);
    const actions = details.locator(".graph-detail-actions");
    await expect(actions.getByRole("link", { name: locale === "en" ? "Open" : "열기", exact: true })).toHaveAttribute("href", `/workspaces/${fixtureWorkspaceId}/context/pages/${seedEntityId}`);
    if (viewport.width === 390) {
      const targets = await actions.locator(":scope > a,:scope > button").evaluateAll((elements) => elements.map((element) => {
        const bounds = element.getBoundingClientRect();
        return { width: bounds.width, height: bounds.height };
      }));
      // Open, center, pin, the Knowledge Map and expand share one row.
      expect(targets.length).toBeGreaterThanOrEqual(4);
      for (const bounds of targets) {
        expect(bounds.width).toBeGreaterThanOrEqual(44);
        expect(bounds.height).toBeGreaterThanOrEqual(44);
      }
    }
    await (await localDetailsActions(details)).getByRole("button", { name: locale === "en" ? "Pin" : "고정", exact: true }).click();
    await expect(actions.getByRole("button", { name: locale === "en" ? "Unpin" : "고정 해제", exact: true })).toBeVisible();
    await details.locator("h5.graph-details-title").click();
    await expect(details.locator("h5.graph-details-title")).toHaveText("Decision Log");
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(initial);
    await evidenceScreenshot(page, testInfo, `local-${locale}-${mode}-${viewport.width}-details-open.png`);

    // A normal input on uncovered native canvas clears selection without
    // touching pins; an input inside the non-modal details does not. The
    // action row follows the fields, so bring the canvas back into view first.
    await graph.locator(".graph-canvas-frame").scrollIntoViewIfNeeded();
    const canvasBounds = await graph.locator(".graph-canvas-frame").boundingBox();
    // Canvas chrome ends at 44px.
    const blank = { x: canvasBounds!.x + 8, y: canvasBounds!.y + 52 };
    expect(await page.evaluate(({ x, y }) => Boolean(document.elementFromPoint(x, y)?.closest(".graph-canvas")), blank)).toBe(true);
    await page.mouse.click(blank.x, blank.y);
    await expect(details).toBeHidden();
    await expect(graph).toBeFocused();
    expect(await localStageGeometry(graph)).toEqual(idle);
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(initial);
    const reselection = await page.evaluate(() => window.__localCoverageFixture.point("node:seed"));
    await page.mouse.click(reselection.x, reselection.y);
    await expect(details.getByRole("button", { name: locale === "en" ? "Unpin" : "고정 해제", exact: true, includeHidden: true })).toHaveCount(1);
    await details.getByRole("button", { name: locale === "en" ? "Clear selection" : "선택 해제", exact: true }).focus();
    await page.keyboard.press("Escape");
    await expect(details).toBeHidden();
    await expect(graph).toBeFocused();
    expect(await localStageGeometry(graph)).toEqual(idle);
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(initial);
    expect(await native!.evaluate((element) => element === document.querySelector("canvas.sigma-nodes"))).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
    await evidenceScreenshot(page, testInfo, `local-${locale}-${mode}-${viewport.width}-details-cleared.png`);
    await testInfo.attach("details-native-invariants.json", { body: Buffer.from(JSON.stringify({ idle, initial })), contentType: "application/json" });
    expect(errors).toEqual([]);
  });
}

// The 760/761 pair owns the container boundary. The narrower 320px layout in
// the same wide viewport is reached by the long-title 320px reader case.
for (const hostWidth of [784, 785]) {
  test(`Local details use the ${hostWidth - 24}px reader boundary inside a wide viewport`, async ({ page }, testInfo) => {
    const errors = await mount(page, "complete", "en", "light", { width: 1440, height: 900 }, hostWidth);
    const graph = page.getByTestId("graph-view");
    const before = await localStageGeometry(graph);
    // The graph frame adds a 1px border on each side; its reader container
    // still owns the 760/761px responsive boundary.
    expect(before.stage.width).toBe(hostWidth - 26);
    const seed = await page.evaluate(() => window.__localCoverageFixture.point("node:seed"));
    await page.mouse.click(seed.x, seed.y);
    await expect(graph.locator("h5.graph-details-title")).toHaveText("Decision Log");
    await expectLocalDetailsBounds(graph, hostWidth - 24 <= 760 ? "below" : "drawer");
    expect(await localStageGeometry(graph)).toEqual(before);
    await evidenceScreenshot(page, testInfo, `local-details-reader-${hostWidth - 24}.png`);
    expect(errors).toEqual([]);
  });
}

// Locale only selects button names here; translated action geometry is owned
// by the long-title cases, which cover Korean on the same 390x640 screen.
test("Local en short-screen details keep actions and full long evidence available", async ({ page }, testInfo) => {
  const locale = "en" as const;
  const errors = await mount(page, "details-long", locale, "light", { width: 390, height: 640 });
  const graph = page.getByTestId("graph-view");
  const details = graph.locator(".graph-details");
  const before = await localStageGeometry(graph);
  const initial = await page.evaluate(() => window.__localCoverageFixture.read());
  const seed = await page.evaluate(() => window.__localCoverageFixture.point("node:seed"));
  await page.mouse.click(seed.x, seed.y);
  await expect(details.locator("h5.graph-details-title")).toContainText("full title stays available");
  await expectLocalDetailsBounds(graph, "below");
  await expect(details).toContainText("end of evidence");
  // Below the canvas the page scrolls Details; its action row follows the fields.
  const actions = details.locator(".graph-detail-actions");
  await actions.scrollIntoViewIfNeeded();
  await expect(actions).toBeInViewport({ ratio: 1 });
  const dimensions = await details.evaluate((element) => ({ client: element.clientHeight, scroll: element.scrollHeight }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client + 1);
  await details.focus();
  const scrollY = await page.evaluate(() => window.scrollY);
  await page.keyboard.press("End");
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(scrollY);
  // Native key scrolling animates; read the position once it has settled.
  const { outerScrollY: scrolled } = await settleLocalAsideScroll(actions);
  await page.evaluate(() => window.__localCoverageFixture.replacePayload());
  expect(await page.evaluate(() => window.scrollY)).toBe(scrolled);
  expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(initial);
  await page.keyboard.press("Escape");
  await expect(details).toBeHidden();
  expect(await localStageGeometry(graph)).toEqual(before);
  await graph.locator(".graph-local-stage").scrollIntoViewIfNeeded();
  const reselection = await page.evaluate(() => window.__localCoverageFixture.point("node:seed"));
  await page.mouse.click(reselection.x, reselection.y);
  await expect(details).toBeVisible();
  await (await localDetailsActions(details)).getByRole("button", { name: "Center", exact: true }).click();
  await graph.locator(".graph-local-stage").scrollIntoViewIfNeeded();
  await expect.poll(async () => {
    const point = await page.evaluate(() => window.__localCoverageFixture.point("node:seed"));
    const frame = await graph.locator(".graph-canvas-frame").boundingBox();
    // Center targets the canvas under its 60px top chrome; Details covers none of it.
    return Math.abs(point.y - (frame!.y + (frame!.height + 60) / 2));
  }).toBeLessThanOrEqual(1);
  const centered = await page.evaluate(() => window.__localCoverageFixture.point("node:seed"));
  expect(await page.evaluate(({ x, y }) => Boolean(document.elementFromPoint(x, y)?.closest(".graph-canvas")), centered)).toBe(true);
  await evidenceScreenshot(page, testInfo, `local-${locale}-details-long-390x640.png`);
  expect(errors).toEqual([]);
});

async function localStageGeometry(graph: Locator) {
  return graph.evaluate((element) => {
    const box = (selector: string) => {
      const bounds = element.querySelector(selector)!.getBoundingClientRect();
      // Existing scoped Escape restores root focus and the browser can reveal
      // its 12px fixture padding. Compare document geometry, not that ordinary
      // focus-induced page scroll; input/hit tests use fresh viewport points.
      return { x: bounds.x + window.scrollX, y: bounds.y + window.scrollY, width: bounds.width, height: bounds.height };
    };
    const native = element.querySelector<HTMLCanvasElement>("canvas.sigma-nodes")!;
    return { stage: box(".graph-local-stage"), frame: box(".graph-canvas-frame"), backing: { width: native.width, height: native.height } };
  });
}

async function expectLocalDetailsBounds(graph: Locator, mode: "drawer" | "below") {
  const geometry = await graph.locator(".graph-details").evaluate((element) => {
    const panel = element.getBoundingClientRect();
    const stage = element.closest(".graph-frame")!.querySelector(".graph-local-stage")!;
    const owner = (element.closest(".graph-local-stage") ?? element.closest(".graph-frame"))!.getBoundingClientRect();
    const stageBottom = stage.getBoundingClientRect().bottom;
    // A panel below the canvas can run past the viewport; only its start is on screen.
    const corners = element.closest(".graph-local-stage") ? [[panel.left + 4, panel.top + 4], [panel.right - 4, panel.bottom - 4]] : [[panel.left + 4, panel.top + 4]];
    return { left: panel.left, right: panel.right, top: panel.top, bottom: panel.bottom, width: panel.width, height: panel.height,
      insideStage: !!element.closest(".graph-local-stage"), stageBottom,
      owner: { left: owner.left, right: owner.right, top: owner.top, bottom: owner.bottom, width: owner.width, height: owner.height },
      painted: corners.every(([x, y]) => element.contains(document.elementFromPoint(x, y))) };
  });
  expect(geometry.painted).toBe(true);
  expect(geometry.left).toBeGreaterThanOrEqual(geometry.owner.left);
  expect(geometry.right).toBeLessThanOrEqual(geometry.owner.right + 1);
  expect(geometry.bottom).toBeLessThanOrEqual(geometry.owner.bottom + 1);
  if (mode === "drawer") {
    expect(geometry.width).toBeLessThanOrEqual(300);
    expect(Math.abs(geometry.right - (geometry.owner.right - 12))).toBeLessThanOrEqual(1);
    expect(Math.abs(geometry.top - (geometry.owner.top + 60))).toBeLessThanOrEqual(1);
  } else {
    expect(geometry.insideStage).toBe(false);
    expect(geometry.top).toBeGreaterThanOrEqual(geometry.stageBottom - 1);
    expect(Math.abs(geometry.right - geometry.owner.right)).toBeLessThanOrEqual(1);
    expect(Math.abs(geometry.left - geometry.owner.left)).toBeLessThanOrEqual(1);
  }
}

const LOCAL_DETAILS_CONTROLS = ".graph-details-close,.graph-details-sheet-toggle,.graph-detail-actions a,.graph-detail-actions button";

async function expectAllLocalDetailsActions(details: Locator, artifactName?: string, controls = LOCAL_DETAILS_CONTROLS) {
  const geometry = await details.evaluate((element, controls) => {
    const panel = element.getBoundingClientRect();
    const readerWidth = element.closest(".graph-view")!.getBoundingClientRect().width;
    return { panel: { x: panel.x, y: panel.y, width: panel.width, height: panel.height },
      viewport: { width: window.innerWidth, height: window.innerHeight },
      minimumTarget: readerWidth <= 760 ? 44 : 36,
      actions: Array.from(element.querySelectorAll<HTMLElement>(controls)).filter((action) => action.getClientRects().length > 0
        && (action.matches("summary") || !action.closest("details:not([open])"))).map((action) => {
        const bounds = action.getBoundingClientRect();
        const x1 = bounds.left + 4; const x2 = bounds.right - 4;
        const y1 = bounds.top + 4; const y2 = bounds.bottom - 4;
        return { name: action.getAttribute("aria-label") ?? action.textContent,
          disabled: action instanceof HTMLButtonElement && action.disabled,
          x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height,
          hits: [[x1, y1], [x2, y1], [x1, y2], [x2, y2],
            [bounds.x + bounds.width / 2, bounds.y + bounds.height / 2]]
            .map(([x, y]) => action.contains(document.elementFromPoint(x, y))) };
      }) };
  }, controls);
  if (artifactName) {
    // Write before assertions so a clipped target retains exact geometry,
    // including the control the earlier first-action-only oracle missed.
    const artifactPath = test.info().outputPath(`${artifactName}.json`);
    await writeFile(artifactPath, JSON.stringify(geometry, null, 2));
    await test.info().attach(`${artifactName}.json`, { path: artifactPath, contentType: "application/json" });
  }
  expect(geometry.actions.length).toBeGreaterThan(0);
  // Disabled actions remain visible affordances; excluding them would miss
  // the original clipped folder Expand button. No scroll/focus before this.
  for (const action of geometry.actions) {
    expect(action.width, `${action.name} target width`).toBeGreaterThanOrEqual(geometry.minimumTarget);
    expect(action.height, `${action.name} target height`).toBeGreaterThanOrEqual(geometry.minimumTarget);
    expect(action.x, `${action.name} panel left`).toBeGreaterThanOrEqual(geometry.panel.x);
    expect(action.y, `${action.name} panel top`).toBeGreaterThanOrEqual(geometry.panel.y);
    expect(action.x + action.width, `${action.name} panel right`).toBeLessThanOrEqual(geometry.panel.x + geometry.panel.width);
    expect(action.y + action.height, `${action.name} panel bottom`).toBeLessThanOrEqual(geometry.panel.y + geometry.panel.height);
    expect(action.x).toBeGreaterThanOrEqual(0);
    expect(action.y).toBeGreaterThanOrEqual(0);
    expect(action.x + action.width).toBeLessThanOrEqual(geometry.viewport.width);
    expect(action.y + action.height).toBeLessThanOrEqual(geometry.viewport.height);
    expect(action.hits, `${action.name} complete painted/hit-testable target`).toEqual([true, true, true, true, true]);
  }
  return geometry;
}

/** The selected item's action row (open, center, pin, the Knowledge Map), scrolled into view. */
async function localDetailsActions(details: Locator) {
  const actions = details.locator(".graph-detail-actions");
  await actions.scrollIntoViewIfNeeded();
  return actions;
}

async function revealLocalCue(page: Page, cue: Locator) {
  for (let step = 0; step < 24; step += 1) {
    const observation = await cue.evaluate((element) => {
      const aside = element.closest<HTMLElement>(".graph-details")!;
      const bounds = aside.getBoundingClientRect();
      let top = bounds.top + aside.clientTop, bottom = top + aside.clientHeight;
      for (const control of Array.from(aside.querySelectorAll<HTMLElement>(".graph-details-title-row,.graph-details-sheet-toggle"))) {
        if (control.getClientRects().length && getComputedStyle(control).position === "sticky") {
          top = Math.max(top, control.getBoundingClientRect().bottom);
        }
      }
      const cueBounds = element.getBoundingClientRect();
      const owner = element.closest("button")!;
      const hits = [[cueBounds.left + 1, cueBounds.top + 1], [cueBounds.right - 1, cueBounds.bottom - 1]]
        .every(([x, y]) => owner.contains(document.elementFromPoint(x, y)));
      return { visible: cueBounds.top >= top && cueBounds.bottom <= bottom && hits,
        x: bounds.left + bounds.width / 2, y: (top + bottom) / 2,
        delta: (cueBounds.top + cueBounds.bottom - top - bottom) / 2 };
    });
    if (observation.visible) return;
    // Real wheel input makes the fine adjustment that a browser's minimal
    // scrollIntoView misses underneath sticky controls. No geometry writes.
    await page.mouse.move(observation.x, observation.y);
    await page.mouse.wheel(0, Math.max(-24, Math.min(24, observation.delta)));
    await settleLocalAsideScroll(cue);
  }
  throw new Error("Native scroll did not expose the complete Local node cue between sticky controls");
}

async function expectLocalTextTailReachable(page: Page, target: Locator, tail: string) {
  // Observe each wrapped fragment, not a union rectangle over blank space.
  // Native scroll is animated independently of prefers-reduced-motion: each
  // key must settle before the proof and its screenshot describe the same UI.
  const trace: Array<{ key: string; settled: Awaited<ReturnType<typeof settleLocalAsideScroll>>;
    observation: Awaited<ReturnType<typeof readLocalTextTail>> }> = [];
  let visible = false;
  let key = "settle preceding native key";
  for (let step = 0; step <= 24; step += 1) {
    const settled = await settleLocalAsideScroll(target);
    const observation = await readLocalTextTail(target, tail);
    trace.push({ key, settled, observation });
    visible = observation.visible;
    if (visible || !settled.stable || observation.fragments.length === 0 || step === 24) break;
    const top = Math.min(...observation.fragments.map((fragment) => fragment.top));
    const bottom = Math.max(...observation.fragments.map((fragment) => fragment.bottom));
    // Coarse movement is safe while the entire target is above the panel.
    // Near a wrapped tail use smaller native steps instead of skipping it.
    key = bottom <= observation.panel.top ? "PageUp"
      : top < observation.panel.top ? "ArrowUp" : "ArrowDown";
    await page.keyboard.press(key);
  }
  const tracePath = test.info().outputPath(`${tail.replaceAll(" ", "-")}-native-scroll-trace.json`);
  await writeFile(tracePath, JSON.stringify({ tail, maxKeys: 24, trace }, null, 2));
  await test.info().attach(path.basename(tracePath), { path: tracePath, contentType: "application/json" });
  expect(trace.at(-1)!.settled.stable, "Native aside scroll must settle before its visual proof").toBe(true);
  expect(visible, `Native aside scroll must expose the complete ${tail}`).toBe(true);
}

async function settleLocalAsideScroll(target: Locator) {
  return target.evaluate(async (element) => {
    const scroller = element.closest<HTMLElement>(".graph-details")!;
    let previous = { top: scroller.scrollTop, x: window.scrollX, y: window.scrollY };
    let stableFrames = 0;
    for (let frame = 0; frame < 30; frame += 1) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      const current = { top: scroller.scrollTop, x: window.scrollX, y: window.scrollY };
      stableFrames = current.top === previous.top && current.x === previous.x && current.y === previous.y
        ? stableFrames + 1 : 0;
      previous = current;
      if (frame >= 3 && stableFrames >= 3) {
        return { stable: true, frames: frame + 1, stableFrames, scrollTop: current.top,
          outerScrollX: current.x, outerScrollY: current.y };
      }
    }
    return { stable: false, frames: 30, stableFrames, scrollTop: previous.top,
      outerScrollX: previous.x, outerScrollY: previous.y };
  });
}

async function readLocalTextTail(target: Locator, tail: string) {
  return target.evaluate((element, text) => {
    const scroller = element.closest<HTMLElement>(".graph-details")!;
    const bounds = scroller.getBoundingClientRect();
    const panel = { top: bounds.top + scroller.clientTop, left: bounds.left + scroller.clientLeft,
      bottom: bounds.top + scroller.clientTop + scroller.clientHeight,
      right: bounds.left + scroller.clientLeft + scroller.clientWidth };
    // Sticky navigation and primary actions own part of the aside viewport.
    // Native keys must expose the text between them, retaining real hit tests.
    for (const control of Array.from(scroller.querySelectorAll<HTMLElement>(".graph-details-title-row,.graph-details-sheet-toggle"))) {
      if (!control.getClientRects().length || getComputedStyle(control).position !== "sticky") continue;
      const box = control.getBoundingClientRect();
      if (box.bottom > panel.top && box.top < panel.bottom) panel.top = Math.max(panel.top, box.bottom);
    }
    const fragments: Array<{ top: number; bottom: number; left: number; right: number;
      width: number; height: number; insidePanel: boolean; insideViewport: boolean; hit: boolean }> = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let node: Node | null;
    while ((node = walker.nextNode())) {
      const index = node.textContent?.lastIndexOf(text) ?? -1;
      if (index < 0) continue;
      const range = document.createRange();
      range.setStart(node, index); range.setEnd(node, index + text.length);
      for (const rectangle of Array.from(range.getClientRects())) {
        if (rectangle.width === 0 || rectangle.height === 0) continue;
        fragments.push({ top: rectangle.top, bottom: rectangle.bottom, left: rectangle.left, right: rectangle.right,
          width: rectangle.width, height: rectangle.height,
          insidePanel: rectangle.top >= panel.top && rectangle.bottom <= panel.bottom
            && rectangle.left >= panel.left && rectangle.right <= panel.right,
          insideViewport: rectangle.left >= 0 && rectangle.right <= window.innerWidth
            && rectangle.top >= 0 && rectangle.bottom <= window.innerHeight,
          hit: element.contains(document.elementFromPoint(rectangle.x + rectangle.width / 2, rectangle.y + rectangle.height / 2)) });
      }
      break;
    }
    return { scrollTop: scroller.scrollTop, scrollHeight: scroller.scrollHeight, clientHeight: scroller.clientHeight,
      outerScrollX: window.scrollX, outerScrollY: window.scrollY, panel, fragments,
      visible: fragments.length > 0 && fragments.every((fragment) => fragment.insidePanel && fragment.insideViewport && fragment.hit) };
  }, tail);
}

async function expectCoveragePanelFits(page: Page, panel: Locator, viewport: { width: number; height: number }) {
  await expect(panel).toBeVisible();
  // The native details toggle positions its popup asynchronously. Observe
  // the same viewport constraints before recording the final geometry.
  await expect.poll(async () => {
    const bounds = await panel.boundingBox();
    return !!bounds && bounds.x >= 0 && bounds.y >= 0
      && bounds.x + bounds.width <= viewport.width && bounds.y + bounds.height <= viewport.height;
  }).toBe(true);
  const bounds = await panel.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
}

test("Local filter exclusions are reversible information, not server coverage warnings", async ({ page }) => {
  const errors = await mount(page, "complete", "en");
  await page.getByTestId("graph-view").locator(".graph-frame-filters > summary").click();
  const linkFilter = page.getByRole("checkbox", { name: "Wiki link", exact: true });
  await linkFilter.uncheck();
  await expect(page.getByText("Some items are hidden by your active filters. Clear filters to show them again.")).toBeVisible();
  await expect(page.getByText(copy.en.coverage)).toHaveCount(0);
  await page.getByRole("button", { name: "Clear all", exact: true }).click();
  await expect(page.getByText(/Some items are hidden by your active filters/)).toHaveCount(0);
  await expect(linkFilter).toBeChecked();
  expect(errors).toEqual([]);
});

for (const { locale, viewport } of LOCAL_LAYOUT_CASES) {
  test(`Local ${locale} ${viewport.width}px 250-node/750-edge lists have one native keyboard entry and Details handoff`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    const errors = await mount(page, "keyboard-boundary", locale, "light", viewport);
    const graph = page.getByTestId("graph-view");
    const list = await openList(page);
    const words = NAVIGATION_COPY[locale];
    // The seed's relations open first (a bow-tie, or a flow when narrow):
    // incoming documents 246/247 and folder A, then eight outgoing documents.
    const seedGroup = list.locator('section.graph-list-group[data-list-group^="seed:"]');
    const incoming = seedGroup.getByRole("list", { name: words.incoming, exact: true });
    const outgoing = seedGroup.getByRole("list", { name: words.outgoing, exact: true });
    const details = graph.locator(".graph-details");
    const title = details.locator("h5.graph-details-title");
    const clear = details.getByRole("button", { name: locale === "en" ? "Clear selection" : "선택 해제", exact: true });
    // The list pages through the exact Local API ceiling the canvas renders.
    await expect(list.locator(".graph-list-counts")).toHaveText(words.counts);
    expect((await page.evaluate(() => window.__localCoverageFixture.read())).positions).toHaveLength(250);
    const native = await graph.locator("canvas.sigma-nodes").elementHandle();
    const idleGeometry = await localStageGeometry(graph);
    const ordinaryWitness = await page.evaluate(() => window.__localCoverageFixture.display("node:boundary:121"));

    // Enter begins on an actual Tab entry, not a programmatic selection: the
    // seed pill is the Tab stop after the incoming list.
    await tabIntoList(page, incoming);
    await page.keyboard.press("Tab");
    expect(await activeLocalGraphId(page)).toBe("node:seed");
    await page.keyboard.press("Enter");
    await expect(details).toBeFocused();
    await expect(title).toHaveText("Boundary document 000");
    await page.keyboard.press("Tab");
    await expect(clear).toBeFocused();
    await (await localDetailsActions(details)).getByRole("button", { name: locale === "en" ? "Pin" : "고정", exact: true }).click();
    await expect(details.getByRole("button", { name: locale === "en" ? "Unpin" : "고정 해제", exact: true, includeHidden: true })).toHaveCount(1);
    await clear.click();
    const idleState = await page.evaluate(() => window.__localCoverageFixture.read());

    await expectNativeRovingList(page, graph, incoming, 3, true);
    // The next Tab stop is the seed pill, not another row of the list.
    expect(await activeLocalGraphId(page)).toBe("node:seed");
    await expect(details).toBeHidden();
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(idleState);
    expect(await localStageGeometry(graph)).toEqual(idleGeometry);
    // Focusing folder A, the incoming list's last row, lights its even
    // members, not the odd witness; the point remains visible and GPU colors change.
    await page.keyboard.press("Shift+Tab");
    expect(await activeLocalGraphId(page)).toBe("node:folder:a");
    await expect.poll(() => page.evaluate(() => window.__localCoverageFixture.display("node:boundary:121").color))
      .not.toBe(ordinaryWitness.color);
    expect(await page.evaluate(() => window.__localCoverageFixture.display("node:boundary:121").hidden)).toBe(false);
    await page.keyboard.press("Space");
    await expect(details).toBeFocused();
    await expect(title).toHaveText("Boundary folder A");
    // The list scrolls inside the frame. A keyboard handoff must still expose
    // Details' actions immediately, not wait for the next Tab to reveal an
    // offscreen focused aside.
    const firstAction = details.locator(".graph-details-close").first();
    const handoffViewport = await details.evaluate((element) => {
      const box = (target: Element) => {
        const bounds = target.getBoundingClientRect();
        return { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height };
      };
      return { scroll: { x: window.scrollX, y: window.scrollY },
        viewport: { width: window.innerWidth, height: window.innerHeight },
        details: box(element), heading: box(element.querySelector(".graph-details-title-row")!),
        firstAction: box(element.querySelector(".graph-details-close")!),
        focused: document.activeElement === element };
    });
    const handoffPath = testInfo.outputPath("end-space-handoff-before-next-tab.json");
    await writeFile(handoffPath, JSON.stringify(handoffViewport, null, 2));
    await testInfo.attach("end-space-handoff-before-next-tab.json", {
      path: handoffPath, contentType: "application/json",
    });
    await evidenceScreenshot(page, testInfo, `local-${locale}-${viewport.width}-end-space-before-next-tab.png`);
    await expect(details.locator(".graph-details-title-row")).toBeInViewport({ ratio: 1 });
    await expect(firstAction).toBeInViewport({ ratio: 1 });
    await expectAllLocalDetailsActions(details, "end-space-all-actions-before-next-tab");
    await page.keyboard.press("Tab");
    await expect(details.locator(".graph-details-close").first()).toBeFocused();

    // Folder A's 124 members are its outgoing relations, five per page; a
    // revealed page joins the same single entry.
    const members = details.getByRole("list", { name: words.outgoing, exact: true });
    await expectNativeRovingList(page, graph, members, 5, true);
    await details.locator(".relation-list-more").click();
    await expectNativeRovingList(page, graph, members, 10, true);
    await expect(title).toHaveText("Boundary folder A");

    // The seed by keyboard; its folders are among its incoming relations,
    // a separate entry even though folder A is also a main-list row.
    await tabIntoList(page, incoming);
    await page.keyboard.press("Tab");
    expect(await activeLocalGraphId(page)).toBe("node:seed");
    await page.keyboard.press("Enter");
    await expect(details).toBeFocused();
    await expect(details.getByRole("button", { name: locale === "en" ? "Unpin" : "고정 해제", exact: true, includeHidden: true })).toHaveCount(1);
    const seedIncoming = details.getByRole("list", { name: words.incoming, exact: true });
    await expect(seedIncoming.locator('button[data-local-graph-id="node:folder:a"]')).toHaveCount(1);
    await expectNativeRovingList(page, graph, seedIncoming, 3, true);
    const seedOutgoing = details.getByRole("list", { name: words.outgoing, exact: true });
    await expectNativeRovingList(page, graph, seedOutgoing, 5, true);
    await focusLocalRowWithKeyboard(page, seedOutgoing, "node:boundary:007");
    await page.keyboard.press("Enter");
    await expect(details).toBeFocused();
    await expect(title).toHaveText("Boundary document 007");
    await expect(details.getByRole("button", { name: locale === "en" ? "Expand" : "연결 펼치기", exact: true })).toBeEnabled();
    await expectAllLocalDetailsActions(details, "eligible-enter-all-actions-before-next-tab");
    await page.keyboard.press("Tab");
    await expect(clear).toBeFocused();

    // A pointer preserves focus on its row. Space on that SAME selected ID
    // still hands off to Details rather than relying on an ID state change.
    const sameRow = outgoing.locator('button[data-local-graph-id="node:boundary:007"]');
    await sameRow.click();
    await expect(sameRow).toBeFocused();
    await expect(details).not.toBeFocused();
    const beforeSameId = await page.evaluate(() => window.__localCoverageFixture.read());
    await page.keyboard.press("Space");
    await expect(details).toBeFocused();
    await expectAllLocalDetailsActions(details, "same-id-space-all-actions-before-next-tab");
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(beforeSameId);
    await sameRow.click();
    await expect(sameRow).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(details).toBeFocused();
    await expectAllLocalDetailsActions(details, "same-id-enter-all-actions-before-next-tab");
    await clear.click();

    // Relation rows (the two-step ring) are a composite independent of node rows.
    await list.locator('section[data-list-group="ringRelations"] > .relation-list-fold-head').click();
    const relations = list.getByRole("list", { name: words.ringRelations, exact: true });
    const [firstEdgeId, secondEdgeId] = await relations.locator(":scope > li > button[data-local-graph-id]")
      .evaluateAll((elements) => elements.map((element) => element.getAttribute("data-local-graph-id")!));
    const ordinaryEdge = await page.evaluate((id) => window.__localCoverageFixture.display(id), firstEdgeId);
    await expectNativeRovingList(page, graph, relations, 10, false);
    await tabIntoList(page, relations);
    await page.keyboard.press("Home");
    expect(await activeLocalGraphId(page)).toBe(firstEdgeId);
    await expect.poll(() => page.evaluate((id) => window.__localCoverageFixture.display(id).color, firstEdgeId))
      .toBe(ordinaryEdge.color);
    await page.keyboard.press("ArrowDown");
    expect(await activeLocalGraphId(page)).toBe(secondEdgeId);
    await expect.poll(() => page.evaluate((id) => window.__localCoverageFixture.display(id).color, firstEdgeId))
      .not.toBe(ordinaryEdge.color);
    await expect(details).toBeHidden();
    await page.keyboard.press("Space");
    await expect(details).toBeFocused();
    const selectedEdgeRow = relations.locator(`button[data-local-graph-id="${secondEdgeId}"]`);
    await expect(selectedEdgeRow).toHaveAttribute("aria-pressed", "true");
    // Details names the relation the row draws: its kind, then both ends.
    await expect(title).toHaveText((await selectedEdgeRow.locator(".relation-list-map-kind .relation-list-chip").getAttribute("title"))!);
    await expect(details.locator(".relation-list-ends strong")).toHaveText(await selectedEdgeRow.locator(".relation-list-map-end").allTextContents());
    await page.keyboard.press("Tab");
    await expect(clear).toBeFocused();
    await graph.locator(".graph-local-stage").scrollIntoViewIfNeeded();
    await evidenceScreenshot(page, testInfo, `local-${locale}-${viewport.width}-keyboard-selected-relation.png`);
    expect(await native!.evaluate((element) => element === document.querySelector("canvas.sigma-nodes"))).toBe(true);
    // A narrow List grows in the page with its opened groups; the canvas under it does not.
    const { frame, backing } = await localStageGeometry(graph);
    expect({ frame, backing }).toEqual({ frame: idleGeometry.frame, backing: idleGeometry.backing });
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(idleState);

    // Text fallback preserves all original rows and does not attempt a
    // Mermaid layout above its 500-relation limit.
    const fallback = graph.locator("details.graph-static-fallback");
    await fallback.locator(":scope > summary").click();
    const textLists = fallback.locator(".graph-text-contents > ul");
    await expect(textLists).toHaveCount(2);
    await expect(textLists.nth(0).locator(":scope > li")).toHaveCount(250);
    await expect(textLists.nth(1).locator(":scope > li")).toHaveCount(750);
    await expect(fallback.locator(".mermaid")).toHaveCount(0);
    await testInfo.attach("local-keyboard-native-invariants.json", {
      body: JSON.stringify({ fixture: "synthetic exact250/750 Local wire; actual GraphView/Sigma", locale, viewport, idleGeometry, idleState }, null, 2),
      contentType: "application/json",
    });
    expect(errors).toEqual([]);
  });
}

test("Local bounded keyboard entries retire on disclosure, tab, scene and workspace changes without late focus", async ({ page }, testInfo) => {
  const errors = await mount(page, "keyboard-boundary", "en", "light", { width: 1440, height: 900 });
  const graph = page.getByTestId("graph-view");
  const list = await openList(page);
  const seedGroup = list.locator('section.graph-list-group[data-list-group^="seed:"]');
  const fold = seedGroup.locator(":scope > .relation-list-fold-head");
  const nodes = seedGroup.getByRole("list", { name: "Outgoing", exact: true });
  await focusLocalRowWithKeyboard(page, nodes, "node:boundary:001");
  expect(await activeLocalGraphId(page)).toBe("node:boundary:001");
  await fold.click();
  await expect(fold).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Tab");
  await expect(list.locator('section[data-list-group="neighbors"] > .relation-list-fold-head')).toBeFocused();
  expect(await seedGroup.evaluate((element) => element.contains(document.activeElement))).toBe(false);
  await fold.click();
  // Reopening remounts the rows with one entry; a real Tab from the control
  // before them must then focus the list itself.
  await expect(fold).toHaveAttribute("aria-expanded", "true");
  const restoredEntry = nodes.locator("button[tabindex='0']");
  await expect(restoredEntry).toHaveCount(1);
  await tabIntoList(page, nodes);
  await expect(restoredEntry).toBeFocused();

  await page.getByRole("button", { name: "Hide graph tab", exact: true }).click();
  await expect(graph).toBeHidden();
  await page.getByRole("button", { name: "Show graph tab", exact: true }).click();
  await expect(graph).toBeVisible();
  await expect(page.getByRole("button", { name: "Hide graph tab", exact: true })).toBeFocused();
  await focusLocalRowWithKeyboard(page, nodes, "node:boundary:001");
  expect(await activeLocalGraphId(page)).toBe("node:boundary:001");
  const remove = page.getByRole("button", { name: "Remove boundary row 001", exact: true });
  await remove.click();
  await expect(nodes.locator('button[data-local-graph-id="node:boundary:001"]')).toHaveCount(0);
  await expect(remove).toBeFocused();
  await expect(nodes.locator("button[tabindex='0']")).toHaveCount(1);
  const empty = page.getByRole("button", { name: "Empty graph scene", exact: true });
  await empty.click();
  await expect(graph.locator("button[data-local-graph-id]")).toHaveCount(0);
  await expect(empty).toBeFocused();
  await expect(graph.locator(".graph-details")).toBeHidden();
  await page.getByRole("button", { name: "Restore graph scene", exact: true }).click();
  await expect(graph.locator(".graph-canvas-frame")).toHaveAttribute("data-renderer-status", "ready");
  await tabIntoList(page, nodes);
  await page.keyboard.press("Home");
  await page.keyboard.press("Enter");
  await expect(graph.locator(".graph-details")).toBeFocused();
  const scope = page.getByRole("button", { name: "Change workspace scope", exact: true });
  await scope.click();
  await expect(graph.locator(".graph-details")).toBeHidden();
  await expect(scope).toBeFocused();
  await expect(graph.locator(".graph-canvas-frame")).toHaveAttribute("data-renderer-status", "ready");
  // The new scope starts in the Graph view; its reopened list carries no selection.
  await openList(page);
  await expect(graph.locator("button[data-local-graph-id]").first()).toBeVisible();
  await expect(graph.locator("button[data-local-graph-id][aria-pressed='true']")).toHaveCount(0);
  await graph.locator(".graph-local-stage").scrollIntoViewIfNeeded();
  await evidenceScreenshot(page, testInfo, "local-keyboard-boundary-scope-restored.png");
  expect(errors).toEqual([]);
});

test("Local revision-only refresh preserves an already pinned native drag until release", async ({ page }, testInfo) => {
  // Three synthetic public nodes, actual GraphView/Sigma and native mouse.
  // This is an interaction gate, not Local 250/750 or Map 2k performance proof.
  const errors = await mount(page, "complete", "en", "light", { width: 1440, height: 900 });
  const graph = page.getByTestId("graph-view");
  const details = graph.locator(".graph-details");
  // Details' title is the selection witness: the list and its pressed row
  // give way to the canvas for the native drag.
  const title = details.locator("h5.graph-details-title");
  const native = await graph.locator("canvas.sigma-nodes").elementHandle();
  const diagnosticPath = testInfo.outputPath("pinned-drag-native-gesture.json");
  const diagnostics: Array<{ phase: string; node: ReturnType<Window["__localCoverageFixture"]["nativeNode"]> }> = [];
  const observe = async (phase: string) => {
    const node = await page.evaluate(() => window.__localCoverageFixture.nativeNode("node:related"));
    diagnostics.push({ phase, node });
    // Persist before any gesture prerequisite can fail, including before the
    // metadata refresh. Raw and processed coordinates are different spaces.
    await writeFile(diagnosticPath, JSON.stringify(diagnostics, null, 2));
    return node;
  };
  await (await openList(page)).locator('button[data-local-graph-id="node:related"]').click();
  await expect(title).toHaveText("Architecture Notes");
  await (await localDetailsActions(details)).getByRole("button", { name: "Pin", exact: true }).click();
  const unpin = details.getByRole("button", { name: "Unpin", exact: true, includeHidden: true });
  await expect(unpin).toHaveCount(1);
  await showGraph(page);
  await observe("pinned-before-center");
  // Real Center keeps the selected node outside the overlaid Details drawer.
  await (await localDetailsActions(details)).getByRole("button", { name: "Center", exact: true }).click();
  // Camera state changes synchronously; Sigma's painted conversion matrix is
  // updated at render. Wait for paint, not an arbitrary timer or a canvas hit.
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect.poll(() => page.evaluate(() => {
    const { x, y } = window.__localCoverageFixture.nativeNode("node:related").paintedPoint;
    return Boolean(document.elementFromPoint(x, y)?.closest(".graph-canvas"));
  })).toBe(true);
  const before = await page.evaluate(() => window.__localCoverageFixture.read());
  expect(before.positions).toHaveLength(3);
  expect(before.instances).toBe(1);
  expect(before.graphActions).toBe(0);
  const revision = await page.evaluate(() => window.__localCoverageFixture.projectionRevision());
  const start = (await observe("center-painted")).paintedPoint;
  const destination = { x: start.x + 36, y: start.y + 24 };
  expect(await page.evaluate(({ x, y }) => Boolean(document.elementFromPoint(x, y)?.closest(".graph-canvas")), destination)).toBe(true);
  const geometry = await localStageGeometry(graph);
  const key = `context_page:${relatedEntityId}`;

  await page.mouse.move(start.x, start.y);
  await observe("native-move-before-hover-assertion");
  await expect.poll(() => page.evaluate(() => window.__localCoverageFixture.nativeNode("node:related").hoveredId)).toBe("node:related");
  await observe("native-related-hover-before-down");
  await page.mouse.down();
  try {
    for (let step = 1; step <= 4; step += 1) {
      await page.mouse.move(start.x + (destination.x - start.x) * step / 4,
        start.y + (destination.y - start.y) * step / 4);
      await observe(`held-move-${step}`);
    }
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await observe("held-destination-painted-before-assertion");
    await expect.poll(() => page.evaluate(({ x, y }) => {
      const point = window.__localCoverageFixture.nativeNode("node:related").paintedPoint;
      return Math.hypot(point.x - x, point.y - y);
    }, destination)).toBeLessThanOrEqual(1);
    const dragging = await page.evaluate(() => window.__localCoverageFixture.read());
    const originalPosition = before.positions.find((position) => position.key === key)!;
    const draggedPosition = dragging.positions.find((position) => position.key === key)!;
    expect(Math.hypot(draggedPosition.x - originalPosition.x, draggedPosition.y - originalPosition.y)).toBeGreaterThan(1);
    expect(dragging.camera).toEqual(before.camera);
    expect(dragging.instances).toBe(before.instances);
    expect(dragging.graphActions).toBe(before.graphActions);

    // The pointer stays held: replaying React's last committed pin here would
    // move the node back to the pre-drag point and must fail this assertion.
    await page.evaluate(() => window.__localCoverageFixture.replacePayload());
    await expect.poll(() => page.evaluate(() => window.__localCoverageFixture.projectionRevision())).toBe(revision + 1);
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await observe("held-revision-refreshed");
    const refreshed = await page.evaluate(() => window.__localCoverageFixture.read());
    expect(refreshed).toEqual(dragging);
    expect(await localStageGeometry(graph)).toEqual(geometry);
    expect(await native!.evaluate((element) => element === document.querySelector("canvas.sigma-nodes"))).toBe(true);
    await expect(title).toHaveText("Architecture Notes");
    await expect(unpin).toHaveCount(1);
    await evidenceScreenshot(page, testInfo, "local-pinned-drag-revision-held.png");

    await page.mouse.up();
    await expect.poll(() => page.evaluate((semanticKey) =>
      window.__localCoverageFixture.read().positions.find((position) => position.key === semanticKey), key)).toEqual(draggedPosition);
    // A subsequent move without a pressed button proves the drag released;
    // it must not change the newly committed pin after the metadata refresh.
    await page.mouse.move(destination.x + 15, destination.y - 15);
    const released = await page.evaluate(() => window.__localCoverageFixture.read());
    expect(released.positions.find((position) => position.key === key)).toEqual(draggedPosition);
    expect(released.camera).toEqual(before.camera);
    expect(released.instances).toBe(before.instances);
    expect(released.graphActions).toBe(before.graphActions);
    await expect(title).toHaveText("Architecture Notes");
    await expect(unpin).toHaveCount(1);
    const invariantPath = testInfo.outputPath("pinned-drag-metadata-native-invariants.json");
    await writeFile(invariantPath, JSON.stringify({ fixture: "synthetic three-node actual GraphView/Sigma",
      before, dragging, refreshed, released, revisionBefore: revision, revisionAfter: revision + 1,
      start, destination, geometry, pointerReleased: true }, null, 2));
    await testInfo.attach("pinned-drag-metadata-native-invariants.json", { path: invariantPath, contentType: "application/json" });
  } finally {
    await observe("cleanup-before-release");
    await page.mouse.up();
    await observe("cleanup-after-release");
    await testInfo.attach("pinned-drag-native-gesture.json", { path: diagnosticPath, contentType: "application/json" });
  }
  expect(errors).toEqual([]);
});

test("Local disappearing pointer member recovers scoped Escape without a keyboard Details handoff", async ({ page }, testInfo) => {
  const errors = await mount(page, "node-cues", "en", "light", { width: 390, height: 844 });
  const graph = page.getByTestId("graph-view");
  const nodes = (await openList(page)).locator('section.graph-list-group[data-list-group^="seed:"]');
  const seed = nodes.locator('button[data-local-graph-id="node:seed"]');
  const details = graph.locator(".graph-details");
  await seed.click();
  await expect(details.locator("h5.graph-details-title")).toHaveText("Decision Log");
  await (await localDetailsActions(details)).getByRole("button", { name: "Pin", exact: true }).click();
  await expect(details.getByRole("button", { name: "Unpin", exact: true, includeHidden: true })).toHaveCount(1);
  const before = await page.evaluate(() => window.__localCoverageFixture.read());
  const geometry = await localStageGeometry(graph);
  const native = await graph.locator("canvas.sigma-nodes").elementHandle();

  await nodes.locator('button[data-local-graph-id="node:folder"]').click();
  await expect(details.locator("h5.graph-details-title")).toHaveText("Visible Source Folder");
  // A folder's members are its outgoing relations (folder → member).
  const members = details.getByRole("list", { name: "Outgoing", exact: true });
  const member = members.locator('button[data-local-graph-id="node:seed"]');
  const origin = await member.elementHandle();
  expect(origin).not.toBeNull();
  // This is a native pointer click. Selecting the document replaces the
  // folder's relation flow and the exact focused originating button.
  await member.click();
  await expect(details.locator("h5.graph-details-title")).toHaveText("Decision Log");
  expect(await origin!.evaluate((element) => element.isConnected)).toBe(false);
  await expect(details).not.toBeFocused();
  await expect(graph).toBeFocused();
  await expect(details.getByRole("button", { name: "Unpin", exact: true, includeHidden: true })).toHaveCount(1);
  expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(before);
  expect(await localStageGeometry(graph)).toEqual(geometry);
  expect(await native!.evaluate((element) => element === document.querySelector("canvas.sigma-nodes"))).toBe(true);

  // No second click/focus call scopes Escape: the pointer recovery alone
  // must let the immediate native key clear selection without clearing pins.
  await page.keyboard.press("Escape");
  await expect(details).toBeHidden();
  await expect(graph).toBeFocused();
  await expect(graph.locator("button[data-local-graph-id][aria-pressed='true']")).toHaveCount(0);
  expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(before);
  expect(await localStageGeometry(graph)).toEqual(geometry);
  expect(await native!.evaluate((element) => element === document.querySelector("canvas.sigma-nodes"))).toBe(true);
  await seed.click();
  await expect(seed).toBeFocused();
  await expect(details).not.toBeFocused();
  await expect(details.getByRole("button", { name: "Unpin", exact: true, includeHidden: true })).toHaveCount(1);
  expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(before);
  expect(await localStageGeometry(graph)).toEqual(geometry);
  await evidenceScreenshot(page, testInfo, "local-pointer-member-scoped-escape-390px.png");
  const invariantPath = testInfo.outputPath("pointer-member-native-invariants.json");
  await writeFile(invariantPath, JSON.stringify({ fixture: "actual GraphView/Sigma node-cues",
    originDisconnected: true, pointerRecovery: "Graph root, not Details", pinsRetained: true,
    before, geometry, after: await page.evaluate(() => window.__localCoverageFixture.read()) }, null, 2));
  await testInfo.attach("pointer-member-native-invariants.json", { path: invariantPath, contentType: "application/json" });
  expect(errors).toEqual([]);
});

// Sizes own distinct layouts: a short viewport and a 320px container inside a
// wide viewport. Translated labels need each size once, not a cross product;
// the English labels measured wider and wrap the map link, so English takes
// the narrower 320px reader.
for (const { locale, size } of [
  { locale: "en", size: { name: "320px reader", viewport: { width: 1440, height: 900 }, hostWidth: 344 } },
  { locale: "ko", size: { name: "390px short screen", viewport: { width: 390, height: 640 }, hostWidth: 1100 } },
] as const) {
  test(`Local ${locale} ${size.name} keeps the long-title close control and every action whole, with the page as the Details scroller`, async ({ page }, testInfo) => {
    const errors = await mount(page, "details-actions-long", locale, "light", size.viewport, size.hostWidth);
    const graph = page.getByTestId("graph-view");
    const details = graph.locator(".graph-details");
    // Architecture Notes links to the seed: a row of the seed's incoming list.
    const nodes = (await openList(page)).locator('section.graph-list-group[data-list-group^="seed:"]')
      .getByRole("list", { name: locale === "en" ? "Incoming" : "들어오는 관계", exact: true });
    const native = await graph.locator("canvas.sigma-nodes").elementHandle();
    const geometry = await localStageGeometry(graph);
    const initial = await page.evaluate(() => window.__localCoverageFixture.read());
    const words = { open: locale === "en" ? "Open" : "열기", pin: locale === "en" ? "Pin" : "고정",
      unpin: locale === "en" ? "Unpin" : "고정 해제", expand: locale === "en" ? "Expand" : "연결 펼치기",
      map: locale === "en" ? "Show in Knowledge Map" : "지식 지도에서 보기" };
    await focusLocalRowWithKeyboard(page, nodes, "node:related");
    await page.keyboard.press("Enter");
    await expect(details).toBeFocused();
    await expect(nodes.locator('button[data-local-graph-id="node:related"]')).toHaveAttribute("aria-pressed", "true");
    await expect(details.locator(".graph-details-title-row")).toBeInViewport({ ratio: 1 });
    // The title row and its close control come first; the action row follows the fields.
    const firstActions = await expectAllLocalDetailsActions(details, "long-title-close-before-next-tab", ".graph-details-close,.graph-details-sheet-toggle");
    expect(firstActions.actions.length).toBeGreaterThanOrEqual(1);
    const expand = details.getByRole("button", { name: words.expand, exact: true });
    await expect(expand).toBeEnabled();
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(initial);
    expect(await localStageGeometry(graph)).toEqual(geometry);
    await evidenceScreenshot(page, testInfo, `local-${locale}-${size.hostWidth}-long-title-all-actions-before-tab.png`);

    // Actual next-Tab and pointer actions need no preliminary scroll to
    // expose the controls. Exactly one explicit Expand callback is allowed.
    await page.keyboard.press("Tab");
    await expect(details.getByRole("button", { name: locale === "en" ? "Clear selection" : "선택 해제", exact: true })).toBeFocused();
    const actions = await localDetailsActions(details);
    await expect(actions.getByRole("link", { name: words.map, exact: true }))
      .toHaveAttribute("href", `/workspaces/${fixtureWorkspaceId}/graph?focused_doc_id=${relatedEntityId}`);
    // Open, center, pin, the Knowledge Map and expand: one row, every target whole.
    const rowActions = await expectAllLocalDetailsActions(details, "long-title-revealed-actions", ".graph-detail-actions a,.graph-detail-actions button");
    expect(rowActions.actions).toHaveLength(5);
    await actions.getByRole("button", { name: words.pin, exact: true }).click();
    await expect(details.getByRole("button", { name: words.unpin, exact: true, includeHidden: true })).toHaveCount(1);
    await expectAllLocalDetailsActions(details, undefined, ".graph-detail-actions a,.graph-detail-actions button");
    await expand.click();
    await expect.poll(() => page.evaluate(() => window.__localCoverageFixture.read().graphActions)).toBe(initial.graphActions + 1);
    await expect(expand).toBeEnabled();
    const expanded = await page.evaluate(() => window.__localCoverageFixture.read());
    expect(expanded.expandedNodes).toEqual([{ id: "node:related", entityKind: "context_page", entityId: relatedEntityId }]);
    expect(expanded.instances).toBe(initial.instances);
    expect(expanded.camera).toEqual(initial.camera);
    expect(expanded.positions).toEqual(initial.positions);
    expect(await localStageGeometry(graph)).toEqual(geometry);

    const title = details.locator("h5.graph-details-title");
    const evidence = details.locator("dd").filter({ hasText: "end of evidence" });
    await expect(title).toContainText("end of full title");
    await expect(evidence).toContainText("end of evidence");
    // Below the canvas Details has no inner scroller; the page reaches its end.
    const dimensions = await details.evaluate((element) => ({ client: element.clientHeight, scroll: element.scrollHeight }));
    expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client + 1);
    await details.focus();
    await page.keyboard.press("End");
    await expect(evidence).toBeInViewport();
    // Native key scrolling animates; read the position once it has settled.
    const { outerScrollY: scrolled } = await settleLocalAsideScroll(evidence);
    await evidenceScreenshot(page, testInfo, `local-${locale}-${size.hostWidth}-long-evidence-page-scroll.png`);
    await page.evaluate(() => window.__localCoverageFixture.replacePayload());
    expect(await page.evaluate(() => window.scrollY)).toBe(scrolled);
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(expanded);
    expect(await localStageGeometry(graph)).toEqual(geometry);

    await page.keyboard.press("Escape");
    await expect(details).toBeHidden();
    await expect(graph).toBeFocused();
    await focusLocalRowWithKeyboard(page, nodes, "node:related");
    await page.keyboard.press("Enter");
    await expect(details).toBeFocused();
    await expect(details.getByRole("button", { name: words.unpin, exact: true, includeHidden: true })).toHaveCount(1);
    // Reselection reveals the title row first; the action row follows the fields.
    await expectAllLocalDetailsActions(details, undefined, ".graph-details-close,.graph-details-sheet-toggle");
    await localDetailsActions(details);
    await expectAllLocalDetailsActions(details, undefined, ".graph-detail-actions a,.graph-detail-actions button");
    // SAME-ID activation still hands off, with the original selected node
    // and retained pin rather than a new selection/query/renderer.
    await nodes.locator('button[data-local-graph-id="node:related"]').click();
    await page.keyboard.press("Space");
    await expect(details).toBeFocused();
    await expectAllLocalDetailsActions(details, undefined, ".graph-details-close,.graph-details-sheet-toggle");
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(expanded);
    expect(await localStageGeometry(graph)).toEqual(geometry);
    expect(await native!.evaluate((element) => element === document.querySelector("canvas.sigma-nodes"))).toBe(true);
    await expectLocalDetailsBounds(graph, "below");
    const invariantPath = testInfo.outputPath("long-title-native-invariants.json");
    await writeFile(invariantPath, JSON.stringify({ locale, size, firstActions, initial, expanded,
      geometry, sameIdMetadataScroll: scrolled }, null, 2));
    await testInfo.attach("long-title-native-invariants.json", { path: invariantPath, contentType: "application/json" });
    expect(errors).toEqual([]);
  });
}

async function activeLocalGraphId(page: Page) {
  return page.evaluate(() => document.activeElement?.getAttribute("data-local-graph-id") ?? null);
}

/**
 * Enter a list as a keyboard user does: a real Tab from the control before
 * it in Tab order (document order; no positive tabindex) must reach its entry.
 */
async function tabIntoList(page: Page, list: Locator) {
  await expect(list.locator("button[tabindex='0']")).toHaveCount(1);
  await list.evaluate((element) => {
    const stops = Array.from(document.querySelectorAll<HTMLElement>("a[href],button,input,select,textarea,summary,[tabindex]"))
      .filter((candidate) => candidate.tabIndex >= 0 && !candidate.matches(":disabled")
        && candidate.getClientRects().length > 0 && !candidate.closest("[hidden],[inert]"));
    const index = stops.indexOf(element.querySelector<HTMLElement>("button[tabindex='0']")!);
    if (index < 1) throw new Error("The list entry has no preceding Tab stop");
    stops[index - 1].focus();
  });
  await page.keyboard.press("Tab");
  expect(await list.evaluate((element) => element.contains(document.activeElement))).toBe(true);
}

async function focusLocalRowWithKeyboard(page: Page, list: Locator, id: string) {
  const ids = await list.locator(":scope > li > button[data-local-graph-id]")
    .evaluateAll((elements) => elements.map((element) => element.getAttribute("data-local-graph-id")));
  const index = ids.indexOf(id);
  expect(index, `Original wire row ${id} must exist`).toBeGreaterThanOrEqual(0);
  await tabIntoList(page, list);
  await page.keyboard.press("Home");
  for (let cursor = 0; cursor < index; cursor += 1) await page.keyboard.press("ArrowDown");
  expect(await activeLocalGraphId(page)).toBe(id);
}

/** `count` is the rows shown: lists page their rows (ten in the list view, five in Details). */
async function expectNativeRovingList(page: Page, graph: Locator, list: Locator, count: number, nodes: boolean) {
  const buttons = list.locator(":scope > li > button[data-local-graph-id]");
  await expect(buttons).toHaveCount(count);
  const ids = await buttons.evaluateAll((elements) => elements.map((element) => element.getAttribute("data-local-graph-id")));
  expect(new Set(ids).size).toBe(count);
  await expect(list.locator("button[tabindex='0']")).toHaveCount(1);
  // Exactly one Tab stop: row open-document links stay out of the Tab order.
  await expect(list.locator("a:not([tabindex='-1']),button:not([tabindex='-1'])")).toHaveCount(1);
  const before = await page.evaluate(() => window.__localCoverageFixture.read());
  const geometry = await localStageGeometry(graph);
  const selection = await graph.locator("button[data-local-graph-id][aria-pressed='true']")
    .evaluateAll((elements) => elements.map((element) => element.getAttribute("data-local-graph-id")));
  await tabIntoList(page, list);
  expect(ids).toContain(await activeLocalGraphId(page));
  for (const [key, index] of [["Home", 0], ["ArrowDown", Math.min(1, count - 1)], ["ArrowUp", 0], ["End", count - 1], ["Home", 0], ["End", count - 1]] as const) {
    await page.keyboard.press(key);
    expect(await activeLocalGraphId(page)).toBe(ids[index]);
    if (nodes) {
      await expect.poll(() => page.evaluate((id) => window.__localCoverageFixture.display(id!).forceLabel, ids[index])).toBe(true);
    }
    expect(await page.evaluate(() => window.__localCoverageFixture.read())).toEqual(before);
    expect(await localStageGeometry(graph)).toEqual(geometry);
    expect(await graph.locator("button[data-local-graph-id][aria-pressed='true']")
      .evaluateAll((elements) => elements.map((element) => element.getAttribute("data-local-graph-id")))).toEqual(selection);
    await expect(list.locator("button[tabindex='0']")).toHaveCount(1);
  }
  await page.keyboard.press("Tab");
  expect(await list.evaluate((element) => element.contains(document.activeElement))).toBe(false);
  await page.keyboard.press("Shift+Tab");
  expect(await activeLocalGraphId(page)).toBe(ids.at(-1));
  await page.keyboard.press("Tab");
}

// "seed": the current document's rows show the legend's "This document" glyph.
type CueKind = "seed" | "context" | "version" | "library" | "conversation" | "folder";
type CueMode = "light" | "dark" | "forced";
// `chip` names the row's kind chip: canvas kind, or the family for conversations.
type CueRow = { label: string; kind: CueKind; chip: "context" | "version" | "library" | "ask" | "chat" | "folder" };
const NODE_CUE_ROWS: CueRow[] = [
  { label: "Decision Log", kind: "seed", chip: "context" },
  { label: "Architecture Notes", kind: "context", chip: "context" },
  { label: "Why was this chosen?", kind: "conversation", chip: "ask" },
  { label: "Shared source title", kind: "version", chip: "version" },
  { label: "Shared source title", kind: "library", chip: "library" },
  { label: "Source Artifact", kind: "library", chip: "library" },
  { label: "MCP Conversation", kind: "conversation", chip: "chat" },
  { label: "Visible Source Folder", kind: "folder", chip: "folder" },
];
const CUE_COLORS = {
  light: { seed: "rgb(124, 58, 237)", context: "rgb(124, 58, 237)", version: "rgb(124, 58, 237)", library: "rgb(15, 159, 143)",
    conversation: "rgb(2, 132, 199)", folder: "rgb(100, 116, 139)" },
  dark: { seed: "rgb(139, 92, 246)", context: "rgb(139, 92, 246)", version: "rgb(139, 92, 246)", library: "rgb(13, 148, 136)",
    conversation: "rgb(56, 189, 248)", folder: "rgb(148, 163, 184)" },
} as const;
const NAVIGATION_COPY = {
  en: { incoming: "Incoming", outgoing: "Outgoing", ringRelations: "Relationships further out", counts: "250 nodes · 750 relationships",
    chips: { context: "Context", version: "Version", library: "Library", ask: "Ask turn", chat: "Conversation", folder: "Folder" } },
  ko: { incoming: "들어오는 관계", outgoing: "나가는 관계", ringRelations: "2단계 이상 관계", counts: "노드 250개 · 관계 750개",
    chips: { context: "컨텍스트", version: "버전", library: "라이브러리", ask: "질문 턴", chat: "대화", folder: "폴더" } },
} as const;

// Palette assertions do not depend on locale and translated family names do
// not depend on palette: each palette once, with both locales represented.
for (const { locale, mode } of [
  { locale: "en", mode: "light" },
  { locale: "ko", mode: "dark" },
  { locale: "ko", mode: "forced" },
] as const) {
  test(`Local ${locale} ${mode} canonical node cues retain visible silhouettes at 390px with actual Sigma`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    // Synthetic public wire data, actual GraphView/Sigma/styles/translations.
    // This gate does not claim live API, authentication or visibility proof.
    const errors = await mount(page, "node-cues", locale, mode);
    const words = NAVIGATION_COPY[locale];
    const panel = page.getByTestId("graph-view");
    // The seed's relations reach each of the seven other nodes once.
    const seedGroup = (await openList(page)).locator('section.graph-list-group[data-list-group^="seed:"]');
    const rows = seedGroup.locator("ul > li");
    const mainButtons = rows.locator(":scope > button.relation-list-row-select");
    const neighbors = NODE_CUE_ROWS.filter((node) => node.label !== "Decision Log");
    await expectNodeCueRows(mainButtons, neighbors, locale, mode);

    // Check the composited row SVG screenshots, not only attributes: a square
    // has filled corners, a folder has a hole, and a chat/activity diamond
    // is inset along diagonals where a disc is filled in every colour mode.
    const silhouettes: Array<{ region: string; kind: CueKind } & Awaited<ReturnType<typeof inspectCuePixels>>> = [];
    const proveSilhouettes = async (region: "main" | "neighbors", buttons: Locator) => {
      for (const kind of ["context", "library", "conversation", "folder"] as const) {
        const cue = buttons.locator(`:scope > .relation-list-flow-name > svg[data-node-visual-kind="${kind}"]`).first();
        await cue.scrollIntoViewIfNeeded();
        // Sticky rows can cover a minimally revealed row. Center its real SVG
        // within the scrollport before pixel proof.
        if (region === "main") {
          await cue.evaluate((element) => element.scrollIntoView({ block: "center", inline: "nearest" }));
        } else {
          await cue.evaluate((element) => element.scrollIntoView({ block: "center", inline: "nearest" }));
          await settleLocalAsideScroll(cue);
          await revealLocalCue(page, cue);
        }
        const screenshot = await cue.screenshot({ path: testInfo.outputPath(`local-${locale}-${mode}-${region}-${kind}-cue.png`) });
        const pixels = await inspectCuePixels(page, screenshot);
        expect(pixels.center, `${region} ${kind} visible center`).toBe(kind !== "folder");
        if (kind === "library") expect(pixels.corner, `${region} Library must paint square corners`).toBe(true);
        if (kind === "context" || kind === "conversation") expect(pixels.corner, `${region} ${kind} must not paint square corners`).toBe(false);
        if (kind === "folder") expect(pixels.rim, `${region} folder must have a visible rim`).toBe(true);
        if (kind === "context") expect(pixels.diagonal, `${region} Context must remain a disc`).toBe(true);
        if (kind === "conversation") expect(pixels.diagonal, `${region} chat/activity must be a diamond`).toBe(false);
        silhouettes.push({ region, kind, ...pixels });
        await testInfo.attach(`local-${locale}-${mode}-${region}-${kind}-cue.png`, { body: screenshot, contentType: "image/png" });
      }
    };
    // List rows first, before Details opens.
    await proveSilhouettes("main", mainButtons);

    const members = NODE_CUE_ROWS.filter((node) => node.kind !== "folder");
    const folderRow = rows.filter({ has: page.locator(':scope > button[data-local-graph-id="node:folder"]') });
    const details = panel.locator(".graph-details");
    await folderRow.locator(":scope > button").click();
    await expect(details.locator("h5.graph-details-title")).toHaveText("Visible Source Folder");
    // UX-12 gives actions/membership one selected-Details owner instead of
    // duplicating interactive member lists in every main node row.
    await expect(folderRow.locator("button, details, ul")).toHaveCount(1);
    // A folder's members are its outgoing relations, five per page.
    await expect(details.locator(".relation-list-fold-head", { hasText: words.outgoing })
      .locator(".relation-list-fold-count")).toHaveText("7");
    await details.locator(".relation-list-more").click();
    const memberButtons = details.getByRole("list", { name: words.outgoing, exact: true }).locator(":scope > li > button");
    await expectNodeCueRows(memberButtons, members, locale, mode);
    await seedGroup.locator('button[data-local-graph-id="node:seed"]').click();
    await expect(details.locator("h5.graph-details-title")).toHaveText("Decision Log");
    const neighborButtons = details.locator(".relation-list-flow-row > button");
    await expectNodeCueRows(neighborButtons, neighbors, locale, mode);
    // A document's folders are among its incoming relations.
    await expectNodeCueRows(details.getByRole("list", { name: words.incoming, exact: true })
      .locator(':scope > li > button[data-local-graph-id="node:folder"]'),
    NODE_CUE_ROWS.filter((node) => node.kind === "folder"), locale, mode);
    await proveSilhouettes("neighbors", neighborButtons);

    // Canvas paint and GPU state are read from the Graph view, not under the list.
    await showGraph(page);
    const nativeNodes = panel.locator("canvas.sigma-nodes");
    await expect(nativeNodes).toBeVisible();
    const gpu = await nativeNodes.evaluate((element) => {
      const canvas = element as HTMLCanvasElement;
      const context = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
      return { width: canvas.width, height: canvas.height, live: Boolean(context) && !context!.isContextLost(), error: context?.getError() };
    });
    expect(gpu.live).toBe(true);
    expect(gpu.error).toBe(0);
    expect(gpu.width).toBeGreaterThan(0);
    expect(gpu.height).toBeGreaterThan(0);
    if (mode !== "forced") expect(await purpleNodePixels(page)).toBeGreaterThan(5);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await panel.locator(".graph-local-stage").scrollIntoViewIfNeeded();
    await evidenceScreenshot(page, testInfo, `local-${locale}-${mode}-node-cues-390px.png`);
    await testInfo.attach(`local-${locale}-${mode}-node-cues.json`, {
      body: JSON.stringify({ fixture: "synthetic wire data, native GraphView/Sigma", locale, mode, renderedNodes: 8, directMembers: 7, directNeighbors: 7, folders: 1, silhouettes, gpu }, null, 2),
      contentType: "application/json",
    });
    expect(errors).toEqual([]);
  });
}

async function expectNodeCueRows(buttons: Locator, expected: CueRow[], locale: keyof typeof NAVIGATION_COPY, mode: CueMode) {
  await expect(buttons).toHaveCount(expected.length);
  const actual = await buttons.evaluateAll((elements) => elements.map((element) => {
    const cue = element.querySelector(":scope > .relation-list-flow-name > svg[data-node-visual-kind]");
    const bounds = cue?.getBoundingClientRect();
    let surface = "";
    for (let ancestor: Element | null = cue ?? element; ancestor; ancestor = ancestor.parentElement) {
      const background = getComputedStyle(ancestor).backgroundColor;
      if (background !== "transparent" && !/^rgba\([^)]*,\s*0(?:\.0+)?\)$/.test(background)) { surface = background; break; }
    }
    return {
      label: element.querySelector("strong")?.textContent ?? "",
      // The kind chip follows the relation chip and names the node kind in text.
      semantic: Array.from(element.querySelectorAll(".relation-list-row-meta > .relation-list-chip")).at(-1)?.textContent ?? "",
      kind: cue?.getAttribute("data-node-visual-kind") ?? "missing",
      hidden: cue?.getAttribute("aria-hidden"),
      width: bounds?.width ?? 0, height: bounds?.height ?? 0,
      // The SVG opts out of native recoloring so its inherited CSS color is
      // not a system-ink oracle. GraphView's auto-adjusted read surface is.
      foreground: getComputedStyle(element.closest(".graph-view") ?? element).color, surface,
      legacy: Boolean(element.querySelector(".graph-node-swatch")),
      shapes: cue ? Array.from(cue.querySelectorAll("path,circle")).map((shape) => ({
        name: shape.tagName.toLowerCase(), fill: getComputedStyle(shape).fill, stroke: getComputedStyle(shape).stroke,
        radius: Number(shape.getAttribute("r")),
      })) : [],
    };
  }));
  // Multiset comparison preserves both meanings of the duplicate title.
  expect(actual.map((row) => JSON.stringify([row.label, row.kind])).sort())
    .toEqual(expected.map((row) => JSON.stringify([row.label, row.kind])).sort());
  const semantics = NAVIGATION_COPY[locale].chips;
  expect(actual.map((row) => JSON.stringify([row.label, row.kind, row.semantic])).sort())
    .toEqual(expected.map((row) => JSON.stringify([row.label, row.kind, semantics[row.chip]])).sort());
  for (const row of actual) {
    const kind = row.kind as CueKind;
    const color = mode === "forced" ? row.foreground : CUE_COLORS[mode][kind];
    expect(row.hidden).toBe("true");
    expect(row.legacy).toBe(false);
    expect(row.width).toBe(16);
    expect(row.height).toBe(16);
    // One outline per glyph from the shared shape geometry; a folder is a stroked
    // ring and the current document a filled disc in an ink ring.
    expect(row.shapes.map((shape) => shape.name)).toEqual(kind === "folder" || kind === "seed" ? ["circle"] : ["path"]);
    expect(row.shapes[0].fill).toBe(kind === "folder" ? "none" : color);
    if (kind === "folder") expect(row.shapes[0].stroke).toBe(color);
  }
}

async function inspectCuePixels(page: Page, screenshot: Buffer) {
  return page.evaluate(async (encoded) => {
    const bitmap = await createImageBitmap(await (await fetch(`data:image/png;base64,${encoded}`)).blob());
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width; canvas.height = bitmap.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Row SVG screenshot decoder unavailable");
    context.drawImage(bitmap, 0, 0);
    const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
    const pixel = (x: number, y: number) => {
      const index = (Math.min(canvas.height - 1, Math.floor(y / 16 * canvas.height)) * canvas.width
        + Math.min(canvas.width - 1, Math.floor(x / 16 * canvas.width))) * 4;
      return [data[index], data[index + 1], data[index + 2]];
    };
    const background = pixel(0, 0);
    const contrast = (x: number, y: number) => Math.max(...pixel(x, y).map((value, index) => Math.abs(value - background[index])));
    const painted = (x: number, y: number) => contrast(x, y) > 25;
    // The corner probe stays inside the balanced square, outside the disc;
    // the diagonal lies inside the disc but beyond the balanced diamond.
    // Fractional SVG crops add an antialiased fringe: require majority ink
    // coverage there, relative to the solid centre, in every palette.
    return { center: painted(8, 8), corner: painted(3, 4), rim: painted(2, 8),
      diagonal: contrast(11, 11) > contrast(8, 8) * 0.5 };
  }, screenshot.toString("base64"));
}

async function mount(page: Page, scenario: string, locale: string, mode: CueMode = "light", viewport = { width: 390, height: 844 }, hostWidth = 1100) {
  const errors: string[] = [];
  const startup: Array<{ kind: string; url?: string; status?: number; message?: string }> = [];
  const pending = new Set<string>();
  const record = (entry: typeof startup[number]) => { if (startup.length < 256) startup.push(entry); };
  const localPath = (url: string) => url.slice(origin.length);
  const diagnosticMessage = (message: string) => message.slice(0, 2048)
    .replace(/https?:\/\/[^\s"'<>]+/g, (url) => url.startsWith(`${origin}/`) ? localPath(url) : "[external URL redacted]");
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("request", (request) => {
    if (request.url().startsWith(`${origin}/`) && pending.size < 256) pending.add(request.url());
  });
  page.on("requestfinished", (request) => { pending.delete(request.url()); });
  page.on("requestfailed", (request) => {
    pending.delete(request.url());
    if (request.url().startsWith(`${origin}/`)) {
      record({ kind: "requestfailed", url: localPath(request.url()), message: diagnosticMessage(request.failure()?.errorText ?? "") });
    }
  });
  page.on("response", (response) => {
    if (response.url().startsWith(`${origin}/`)
      && (response.status() >= 400 || response.request().resourceType() === "script")) {
      record({ kind: "response", url: localPath(response.url()), status: response.status() });
    }
  });
  await page.emulateMedia({ reducedMotion: "reduce", forcedColors: mode === "forced" ? "active" : "none" });
  await page.setViewportSize(viewport);
  await page.route(`${origin}/favicon.ico`, (route) => route.fulfill({ status: 204 }));
  // Load the real styles before the renderer snapshots its inherited palette.
  // A late CSS update must not masquerade as a Dialog-triggered engine rebuild.
  await page.route(`${origin}/fixture**`, (route) => route.fulfill({ contentType: "text/html", body: `<!doctype html>
    <html data-theme="${mode === "dark" ? "dark" : "light"}"><head><meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="/src/styles/index.css"></head>
    <body><main id="fixture" style="padding:12px;max-width:${hostWidth}px;margin:auto"></main>
    <script type="module" src="/@fs${path.resolve("tests/graph-webgl/local-coverage-entry.ts")}"></script></body></html>` }));
  await page.goto(`${origin}/fixture?scenario=${scenario}&locale=${locale}`);
  try {
    await expect(page.getByTestId("graph-view")).toBeVisible();
    await expect(page.locator(".graph-canvas-frame")).toHaveAttribute("data-renderer-status", "ready");
    await expect.poll(() => page.locator(".graph-canvas canvas").count()).toBeGreaterThan(2);
    // The Graph view stays the default. The List view covers the canvas, so
    // only cases that use its rows open it (openList) through the same switch.
    await page.getByTestId("graph-view").locator(".graph-local-stage").scrollIntoViewIfNeeded();
  } catch (failure) {
    // Preserve early module/network/React failures that used to disappear
    // before this helper returned errors. No retry or timeout relaxation.
    try {
      const diagnosticPath = test.info().outputPath("local-fixture-startup-failure.json");
      const documentState = await page.evaluate(() => ({
        path: window.location.pathname + window.location.search, readyState: document.readyState,
        fixtureChildCount: document.getElementById("fixture")?.childElementCount ?? null,
        graphCount: document.querySelectorAll('[data-testid="graph-view"]').length,
        scripts: Array.from(document.scripts).slice(0, 256).map((script) => ({
          src: script.src.startsWith(window.location.origin + "/")
            ? script.src.slice(window.location.origin.length) : "[external URL redacted]", type: script.type,
        })),
      }));
      await writeFile(diagnosticPath, JSON.stringify({ scenario, locale, mode, viewport, hostWidth,
        errors: errors.slice(0, 256).map(diagnosticMessage), startup, pending: [...pending].map(localPath), documentState }, null, 2));
      await test.info().attach("local-fixture-startup-failure.json", { path: diagnosticPath, contentType: "application/json" });
    } catch {
      // A closed/crashed page or artifact-write failure must not replace the
      // original assertion with an instrumentation failure.
      test.info().annotations.push({ type: "startup-diagnostic-unavailable", description: "Original mount assertion retained." });
    }
    throw failure;
  }
  return errors;
}

/** Choose List in the status-bar switch; the list replaces the canvas in its frame. */
async function openList(page: Page) {
  const graph = page.getByTestId("graph-view");
  await graph.locator(".graph-view-switch").getByRole("button", { name: /^(List|목록)$/ }).click();
  const list = graph.getByTestId("graph-list-view");
  await expect(list).toBeVisible();
  return list;
}

/** Return to the Graph view before native canvas input or canvas pixels. */
async function showGraph(page: Page) {
  const graph = page.getByTestId("graph-view");
  await graph.locator(".graph-view-switch").getByRole("button", { name: /^(Graph|그래프)$/ }).click();
  await expect(graph.getByTestId("graph-list-view")).toHaveCount(0);
}

async function purpleNodePixels(page: Page) {
  // Decode the actual composited canvas screenshot, not a renderer mock or
  // callback; the three-node fixture has real purple Context glyphs.
  const screenshot = await page.getByTestId("graph-canvas").screenshot();
  return page.evaluate(async (encoded) => {
    const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${encoded}`)).blob());
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Screenshot decoder unavailable");
    context.drawImage(image, 0, 0);
    const data = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let pixels = 0;
    for (let index = 0; index < data.length; index += 4) {
      const [red, green, blue] = [data[index], data[index + 1], data[index + 2]];
      if (red > 75 && red < 190 && green < 125 && blue > 110 && blue > red) pixels++;
    }
    return pixels;
  }, screenshot.toString("base64"));
}

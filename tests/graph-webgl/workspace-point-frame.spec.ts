import { expect, test, type Locator, type Page } from "@playwright/test";
import { movedPointCount, readWorkspacePointFrame, readWorkspacePointFrameOnPage, visibleOrdinaryTitleCount,
  visibleOrdinaryTitleCountOnPage, WORKSPACE_GRAPH_HOST_SELECTOR } from "../e2e/workspace_graph_point_frame";

// DOM oracle counterexamples only: these do not claim a synthetic DOM is a GPU
// motion test. The real API spec imports this exact reader for native evidence.
async function fixture(page: Page, count = 40, visible = 2) {
  await page.setContent(`<div id="map"><canvas></canvas><div data-workspace-map-labels>${Array.from({ length: count }, (_, index) =>
    `<span data-node-id="document:${index}" data-point-x="${100 + index}" data-point-y="200" data-full-title="false" style="display:${index < visible ? "block" : "none"}">Document ${index}</span>`).join("")}</div></div>`);
  await page.locator("canvas").evaluate(canvas => {
    (canvas as HTMLCanvasElement & { __zoom: { x: number; y: number; k: number } }).__zoom = { x: 10, y: 20, k: 1 };
  });
  return page.locator("#map");
}

for (const transport of ["element", "selector"] as const) {
const frame = (page: Page, map: Locator) => transport === "element"
  ? map.evaluate(readWorkspacePointFrame) : readWorkspacePointFrameOnPage(page, "#map");
const titles = (page: Page, map: Locator) => transport === "element"
  ? map.evaluate(visibleOrdinaryTitleCount) : visibleOrdinaryTitleCountOnPage(page, "#map");

test(`point-coordinate oracle observes three moving hidden nodes independently of text packing (${transport})`, async ({ page }, info) => {
  const map = await fixture(page), before = await frame(page, map);
  const visible = await titles(page, map);
  await map.evaluate(element => {
    for (const index of [2, 3, 4]) {
      const point = element.querySelector<HTMLElement>(`[data-node-id="document:${index}"]`)!;
      point.dataset.pointX = String(Number(point.dataset.pointX) + 1);
    }
  });
  const after = await frame(page, map);
  const legacy = (frame: typeof before) => ({ ...frame, points: frame.points.filter(point => ["document:0", "document:1"].includes(point.id)) });
  const report = { observedBefore: before.points.length, observedAfter: after.points.length, visibleTitles: visible,
    moved: movedPointCount(before, after), legacyDisplayFilteredMoved: movedPointCount(legacy(before), legacy(after)) };
  await info.attach("hidden-point-counterexample", { body: JSON.stringify(report), contentType: "application/json" });
  expect(report.moved).toBe(3);
  expect(report).toMatchObject({ observedBefore: 40, observedAfter: 40, visibleTitles: 2, legacyDisplayFilteredMoved: 0 });
  expect(after.camera).toEqual(before.camera);
  await map.locator("span").evaluateAll(labels => labels.forEach(label => { (label as HTMLElement).style.display = "none"; }));
  expect(await titles(page, map)).toBe(0);
  expect((await frame(page, map)).points).toHaveLength(40);
});

test(`point-coordinate oracle rejects text/CSS-only and frozen-position motion (${transport})`, async ({ page }) => {
  const map = await fixture(page, 3, 0), before = await frame(page, map);
  await map.locator("span").evaluateAll(labels => labels.forEach(label => {
    (label as HTMLElement).style.display = "block";
    (label as HTMLElement).style.transform = "translate(75px,90px)";
    label.textContent = "Different moving title";
  }));
  const after = await frame(page, map);
  expect(before.points).toHaveLength(3); expect(after.points).toHaveLength(3);
  expect(movedPointCount(before, after)).toBe(0); expect(after.camera).toEqual(before.camera);
});

test(`point-coordinate oracle exposes camera-only projection so the unchanged camera assertion rejects it (${transport})`, async ({ page }) => {
  const map = await fixture(page, 3, 3), before = await frame(page, map);
  await map.evaluate(element => {
    const canvas = element.querySelector("canvas") as HTMLCanvasElement & { __zoom: { x: number; y: number; k: number } };
    canvas.__zoom.x += 10;
    element.querySelectorAll<HTMLElement>("span").forEach(label => { label.dataset.pointX = String(Number(label.dataset.pointX) + 10); });
  });
  const after = await frame(page, map);
  expect(movedPointCount(before, after)).toBe(3);
  expect(after.camera).not.toEqual(before.camera);
});

test(`point-coordinate oracle cannot manufacture motion from changed, empty or duplicate IDs (${transport})`, async ({ page }) => {
  const map = await fixture(page, 3, 3), before = await frame(page, map);
  await map.locator("span").evaluateAll(labels => labels.forEach((label, index) => {
    (label as HTMLElement).dataset.nodeId = `replacement:${index}`;
    (label as HTMLElement).dataset.pointX = "900";
  }));
  expect(movedPointCount(before, await frame(page, map))).toBe(0);
  await map.locator("span").evaluateAll(labels => labels.forEach(label => { (label as HTMLElement).dataset.nodeId = "same-original-id"; }));
  const duplicateBefore = await frame(page, map);
  await map.locator("span").evaluateAll(labels => labels.forEach(label => { (label as HTMLElement).dataset.pointX = "950"; }));
  const duplicateAfter = await frame(page, map);
  expect(duplicateBefore.points).toHaveLength(1); expect(duplicateAfter.points).toHaveLength(1);
  expect(movedPointCount(duplicateBefore, duplicateAfter)).toBe(1);
  await map.locator("span").evaluateAll(labels => labels.forEach(label => { (label as HTMLElement).dataset.nodeId = ""; }));
  expect((await frame(page, map)).points).toHaveLength(0);
});

test(`point-coordinate oracle rejects absent/nonfinite positions and active-title substitutions (${transport})`, async ({ page }) => {
  const map = await fixture(page, 7, 7);
  await map.locator("span").evaluateAll(labels => {
    (labels[0] as HTMLElement).dataset.pointX = "NaN";
    (labels[1] as HTMLElement).dataset.pointY = "Infinity";
    delete (labels[2] as HTMLElement).dataset.pointX;
    (labels[3] as HTMLElement).dataset.pointY = "";
    (labels[4] as HTMLElement).dataset.fullTitle = "true";
    (labels[5] as HTMLElement).dataset.nodeId = " ";
  });
  expect((await frame(page, map)).points).toEqual([{ id: "document:6", x: 106, y: 200 }]);
});

test(`point-coordinate oracle retains the bounded forty-row observation contract (${transport})`, async ({ page }) => {
  const map = await fixture(page, 41, 41);
  await expect(frame(page, map)).rejects.toThrow("40-row budget");
});
}

test("point observers resolve the replacement host inside every call and reject a detached retired Element", async ({ page }) => {
  const map = await fixture(page, 3, 2);
  const retired = await map.elementHandle();
  if (!retired) throw new Error("Fixture must expose its original Element");
  const before = await readWorkspacePointFrameOnPage(page, "#map");
  await retired.evaluate(element => {
    const replacement = element.cloneNode(true) as HTMLElement;
    const canvas = replacement.querySelector("canvas") as HTMLCanvasElement & { __zoom: { x: number; y: number; k: number } };
    canvas.__zoom = { x: 200, y: 300, k: 2 };
    replacement.querySelectorAll<HTMLElement>("span").forEach((label, index) => {
      label.dataset.nodeId = `replacement:${index}`;
      label.dataset.pointX = String(500 + index);
    });
    element.replaceWith(replacement);
  });
  const after = await readWorkspacePointFrameOnPage(page, "#map");
  expect(after.points.map(point => point.id)).toEqual(["replacement:0", "replacement:1", "replacement:2"]);
  expect(after.camera).toEqual({ x: 200, y: 300, k: 2 });
  expect(movedPointCount(before, after)).toBe(0);
  expect(await visibleOrdinaryTitleCountOnPage(page, "#map")).toBe(2);
  await expect(retired.evaluate(readWorkspacePointFrame)).rejects.toThrow("connected current-document host");
  await expect(retired.evaluate(visibleOrdinaryTitleCount)).rejects.toThrow("connected current-document host");
  await retired.dispose();
});

test("point observers reject multiple connected roots instead of choosing a visible or first root", async ({ page }) => {
  const map = await fixture(page, 3, 2);
  await map.evaluate(element => { const second = element.cloneNode(true) as HTMLElement;
    element.after(second); (element as HTMLElement).style.display = "none"; });
  await expect(readWorkspacePointFrameOnPage(page, "#map")).rejects.toThrow("exactly one connected current-document host");
  await expect(visibleOrdinaryTitleCountOnPage(page, "#map")).rejects.toThrow("exactly one connected current-document host");
  await page.evaluate(() => document.querySelector("#map")!.remove());
  expect((await readWorkspacePointFrameOnPage(page, "#map")).points).toHaveLength(3);
  expect(await visibleOrdinaryTitleCountOnPage(page, "#map")).toBe(2);
});

test("point observers reject a missing current host rather than reporting an empty passing snapshot", async ({ page }) => {
  await fixture(page);
  await page.evaluate(() => document.querySelector("#map")!.remove());
  await expect(readWorkspacePointFrameOnPage(page, "#map")).rejects.toThrow("exactly one connected current-document host");
  await expect(visibleOrdinaryTitleCountOnPage(page, "#map")).rejects.toThrow("exactly one connected current-document host");
});

test("point observers reject detached provided Elements instead of accepting stale coordinates or zero visibility", async ({ page }) => {
  const map = await fixture(page, 3, 2);
  const retired = await map.elementHandle();
  if (!retired) throw new Error("Fixture must expose its original Element");
  await retired.evaluate(element => element.remove());
  const observations = await Promise.allSettled([
    retired.evaluate(readWorkspacePointFrame), retired.evaluate(visibleOrdinaryTitleCount),
  ]);
  expect(observations.map(observation => observation.status)).toEqual(["rejected", "rejected"]);
  for (const observation of observations) {
    if (observation.status === "rejected") expect(String(observation.reason)).toContain("connected current-document host");
  }
  await retired.dispose();
});

test("Page point-observer wrappers use only one evaluate with the actual serialized reader and selector", async () => {
  const calls: Array<{ observer: unknown; selector: unknown }> = [];
  const page = new Proxy({}, { get(_target, key) {
    if (key !== "evaluate") throw new Error(`No Locator, ElementHandle or prewait permitted: ${String(key)}`);
    return (observer: unknown, selector: unknown) => { calls.push({ observer, selector }); return Promise.resolve(null); };
  } }) as Page;
  await readWorkspacePointFrameOnPage(page);
  await visibleOrdinaryTitleCountOnPage(page);
  await readWorkspacePointFrameOnPage(page, "#replacement");
  await visibleOrdinaryTitleCountOnPage(page, "#replacement");
  expect(calls).toEqual([
    { observer: readWorkspacePointFrame, selector: WORKSPACE_GRAPH_HOST_SELECTOR },
    { observer: visibleOrdinaryTitleCount, selector: WORKSPACE_GRAPH_HOST_SELECTOR },
    { observer: readWorkspacePointFrame, selector: "#replacement" },
    { observer: visibleOrdinaryTitleCount, selector: "#replacement" },
  ]);
});

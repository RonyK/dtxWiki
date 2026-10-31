import { expect, test, type Page, type Response, type TestInfo } from "@playwright/test";

import type { WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";
import { login } from "./auth";

const OWNER_EMAIL = "e2e-graph-refine-owner@example.com";
const OVERVIEW_PATH = "/graph/overview/query";

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test(`real Map Refine searches visible paths and restores unplaced filters at ${viewport.width}px`, async ({ page }, testInfo) => {
    test.setTimeout(90_000);
    await page.setViewportSize(viewport);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await login(page.request, OWNER_EMAIL);
    const errors: string[] = [];
    const failedReads: number[] = [];
    const posts: unknown[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    page.on("response", (response) => {
      if (response.url().includes("/api/v1/workspaces/") && response.status() >= 400) failedReads.push(response.status());
    });
    page.on("request", (request) => {
      if (request.url().includes(OVERVIEW_PATH) && request.method() === "POST") posts.push(request.postDataJSON());
    });

    // Dedicated persisted corpus: Alpha in Research Delta, Beta at tree root,
    // Gamma without placement. No application API or canvas is intercepted.
    const initialResponse = nextOverview(page);
    await page.goto("/graph");
    const initial = await scene(initialResponse);
    expect(labels(initial)).toEqual(["Alpha", "Beta", "Gamma"]);
    expect(initial.facet_counts.folders.find((folder) => folder.value === "unplaced")?.count).toBe(2);
    const researchFolder = initial.facet_counts.folders.find((folder) => folder.label === "Research Delta")?.value;
    expect(researchFolder).toBeTruthy();
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "3");

    const refine = page.getByRole("button", { name: "Refine", exact: true });
    const dialog = page.getByRole("dialog", { name: "Refine data" });
    const search = dialog.getByRole("searchbox", { name: "Search published documents", exact: true });
    const folder = dialog.getByRole("combobox", { name: "Folder", exact: true });
    const folderRule = dialog.getByRole("combobox", { name: "Folder rule", exact: true });
    const apply = dialog.getByRole("button", { name: "Apply data filters", exact: true });
    const openRefine = async () => {
      await refine.click();
      // Let the modal's initial accessibility focus finish before typing.
      await expect(dialog.getByRole("button", { name: "Close Refine data", exact: true })).toBeFocused();
    };
    const draftFolderRule = async (value: string, mode: "include" | "exclude" | "only") => {
      const countBeforeDraft = posts.length;
      await folder.selectOption(value);
      await folderRule.selectOption(mode);
      expect(posts).toHaveLength(countBeforeDraft);
      await dialog.getByRole("button", { name: /^(?:Add|Update) folder rule$/ }).click();
      expect(posts).toHaveLength(countBeforeDraft);
    };
    const editFolderRule = async (label: string) => {
      const summary = dialog.locator("summary").filter({ hasText: /^Folder rules · \d+$/ });
      if (!(await summary.evaluate((element) => (element.closest("details") as HTMLDetailsElement).open))) {
        await summary.click();
      }
      await dialog.getByRole("button", { name: `Edit folder rule: ${label}`, exact: true }).click();
      await expect(dialog.getByRole("searchbox", { name: "Find folder", exact: true })).toBeFocused();
    };
    await openRefine();
    // A restrictive data filter can leave isolated matches. Explicitly retain
    // them using the existing immediate display setting, not a hidden bypass.
    await dialog.getByRole("checkbox", { name: "Show unlinked documents", exact: true }).check();
    expect(posts).toHaveLength(1);
    await search.fill("Research Delta");
    const pathResponse = nextOverview(page);
    await apply.click();
    expect(labels(await scene(pathResponse))).toEqual(["Alpha"]);
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "1");

    await openRefine();
    await search.fill("target");
    const routeWordResponse = nextOverview(page);
    await apply.click();
    expect(labels(await scene(routeWordResponse))).toEqual([]);

    await openRefine();
    await expect(search).toHaveValue("target");
    await search.fill("");
    await expect(search).toHaveValue("");
    const resetResponse = nextOverview(page);
    await apply.click();
    expect(labels(await scene(resetResponse))).toEqual(["Alpha", "Beta", "Gamma"]);

    await openRefine();
    await draftFolderRule("unplaced", "only");
    const onlyResponse = nextOverview(page);
    await apply.click();
    const only = await scene(onlyResponse);
    expect(labels(only)).toEqual(["Beta", "Gamma"]);
    expect(posts.at(-1)).toMatchObject({ folders: { only: ["unplaced"], include: [], exclude: [] } });
    expect(only.nodes.every((node) => node.grain === "document" && node.primary_folder_id === null)).toBe(true);
    const countBeforeCancel = posts.length;
    await openRefine();
    await draftFolderRule("unplaced", "exclude");
    await search.fill("Alpha");
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(posts).toHaveLength(countBeforeCancel);
    await openRefine();
    await editFolderRule("No visible placement");
    await expect(folder).toHaveValue("unplaced");
    await expect(folderRule).toHaveValue("only");
    await expect(search).toHaveValue("");
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();

    const restoredResponse = nextOverview(page);
    await page.reload();
    expect(labels(await scene(restoredResponse))).toEqual(["Beta", "Gamma"]);
    expect(posts.at(-1)).toMatchObject({ folders: { only: ["unplaced"], include: [], exclude: [] } });
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2");
    await openRefine();
    await editFolderRule("No visible placement");
    await expect(folder).toHaveValue("unplaced");
    await expect(folderRule).toHaveValue("only");
    await expect(dialog.getByRole("checkbox", { name: "Show unlinked documents", exact: true })).toBeChecked();
    await draftFolderRule("unplaced", "exclude");
    const excludeResponse = nextOverview(page);
    await apply.click();
    expect(labels(await scene(excludeResponse))).toEqual(["Alpha"]);
    expect(posts.at(-1)).toMatchObject({ folders: { exclude: ["unplaced"], only: [], include: [] } });

    await openRefine();
    await draftFolderRule("unplaced", "include");
    await draftFolderRule(researchFolder!, "include");
    await capture(page, testInfo, `real-refine-${viewport.width}.png`);
    const mixedResponse = nextOverview(page);
    await apply.click();
    expect(labels(await scene(mixedResponse))).toEqual(["Alpha", "Beta", "Gamma"]);
    const mixedRequest = posts.at(-1) as { folders: { include: string[]; exclude: string[]; only: string[] } };
    expect(mixedRequest.folders.include).toContain("unplaced");
    expect(mixedRequest.folders.include).toHaveLength(2);
    expect(mixedRequest.folders.exclude).toEqual([]);
    expect(mixedRequest.folders.only).toEqual([]);
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "3");
    if (viewport.width < 600) await page.getByRole("button", { name: "Open canvas fullscreen", exact: true }).click();
    await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]')).toBeVisible();
    await expect(page.getByTestId("graph-canvas")).toHaveAttribute("data-map-node-count", "3");
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await capture(page, testInfo, `real-refine-map-${viewport.width}.png`);
    expect(failedReads).toEqual([]);
    expect(errors).toEqual([]);
  });
}

function nextOverview(page: Page) {
  return page.waitForResponse((response) => response.url().includes(OVERVIEW_PATH)
    && response.request().method() === "POST", { timeout: 30_000 });
}

async function capture(page: Page, testInfo: TestInfo, name: string) {
  const path = testInfo.outputPath(name);
  await page.screenshot({ path });
  await testInfo.attach(name, { path, contentType: "image/png" });
}

async function scene(responsePromise: Promise<Response>) {
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  return (await response.json()).data as WorkspaceGraphScene;
}

function labels(payload: WorkspaceGraphScene) {
  expect(payload.counts.candidate_documents).toBe(payload.nodes.length);
  return payload.nodes.map((node) => node.label).sort();
}

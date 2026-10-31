import { expect, test as base, type Page, type Response } from "@playwright/test";

import type { WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";
import { currentWorkspace, login } from "./auth";
import { expectNativeCounts, installNativeDrawMeter } from "./workspace_graph_native_draw";
import { createOverviewResponseReader } from "./workspace_graph_response_reader";

const DOCUMENTS = 12_000;
const PC_BUDGET = { documents: 10_000, relations: 100_000 };
const test = base.extend({ trace: "off" });

function overview(page: Page, workspaceId: string, clusterId?: string) {
  return page.waitForResponse(response => {
    if (!response.url().endsWith(`/workspaces/${workspaceId}/graph/overview/query`)
      || response.request().method() !== "POST") return false;
    const request = response.request().postDataJSON();
    return request.budget.documents === PC_BUDGET.documents
      && request.budget.relations === PC_BUDGET.relations
      && request.expansion?.cluster_id === clusterId;
  });
}

function assertScene(scene: WorkspaceGraphScene) {
  expect(scene.counts.candidate_documents).toBe(DOCUMENTS);
  expect(scene.representation).toBe("aggregated");
  expect(scene.nodes.length).toBeGreaterThan(2_000);
  expect(scene.nodes.length).toBeLessThanOrEqual(PC_BUDGET.documents);
  expect(scene.edges.length).toBeLessThanOrEqual(PC_BUDGET.relations);
  expect(new Set(scene.nodes.map(node => node.id)).size).toBe(scene.nodes.length);
  expect(new Set(scene.edges.map(edge => edge.id)).size).toBe(scene.edges.length);
  const ids = new Set(scene.nodes.map(node => node.id));
  expect(scene.edges.every(edge => ids.has(edge.source) && ids.has(edge.target))).toBe(true);
  expect(scene.nodes.reduce((count, node) => count + (node.grain === "document" ? 1 : node.member_count), 0))
    .toBe(DOCUMENTS);
}

async function assertRendered(page: Page, scene: WorkspaceGraphScene) {
  const links = scene.edges.filter(edge => edge.source !== edge.target).length;
  await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]')).toBeVisible();
  await expect(page.getByTestId("graph-canvas")).toHaveAttribute("data-map-node-count", String(scene.nodes.length));
  await expect(page.getByTestId("graph-canvas")).toHaveAttribute("data-map-edge-count", String(links));
  await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", String(scene.nodes.length));
  await expect(page.getByTestId("workspace-graph-edge-list")).toHaveAttribute("data-total-count", String(scene.edges.length));
  await expectNativeCounts(page, scene.nodes.length, links);
}

test.describe("PC10K real-API aggregated GPU integration", () => {
  test.skip(process.env.DTXWIKI_E2E_GRAPH_10K_AGGREGATED !== "1",
    "Use npm run test:e2e with DTXWIKI_E2E_GRAPH_10K_AGGREGATED=1 for the isolated corpus");
  test.use({ viewport: { width: 1440, height: 900 } });

  test("above-budget cluster selection and expansion preserve the original API scene through native GL", async ({ page, browserName }, testInfo) => {
    test.skip(browserName !== "chromium", "Native WebGL integration targets Chromium");
    test.setTimeout(240_000);
    // Deterministic functional evidence, not normal-motion framing or a hardware SLA.
    await page.emulateMedia({ reducedMotion: "reduce" });
    await login(page.request, "e2e-graph-10k-aggregated-owner@example.com");
    const workspace = await currentWorkspace(page.request);
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await installNativeDrawMeter(page);
    const reader = await createOverviewResponseReader(page, workspace.id);
    try {
      const initialRead = page.waitForResponse(response => response.url().endsWith(`/workspaces/${workspace.id}/graph/overview/query`)
        && response.request().method() === "POST");
      await page.goto("/graph");
      const initial = await reader.read(await initialRead);
      expect(initial.nodes.length).toBeLessThanOrEqual(2_000);
      await expect(page.locator('.graph-canvas-frame[data-renderer-status="ready"]')).toBeVisible();
      const view = page.locator("details.workspace-graph-view-options");
      await view.locator("summary").click();
      const responsePromise = overview(page, workspace.id);
      await page.getByRole("combobox", { name: "Map node limit", exact: true }).selectOption("10k");
      const read = async (response: Response) => {
        expect(response.ok()).toBe(true);
        const scene = await reader.read(response);
        assertScene(scene);
        return scene;
      };
      const scene = await read(await responsePromise);
      await view.locator("summary").click();
      await assertRendered(page, scene);
      const cluster = scene.nodes.find(node => node.grain === "cluster"
        && node.cluster_kind === "link_cluster" && node.relation_count > 0);
      if (!cluster || cluster.grain !== "cluster") throw new Error("Expected a linked above-budget cluster");
      expect(scene.edges.some(edge => edge.source !== edge.target
        && (edge.source === cluster.id || edge.target === cluster.id))).toBe(true);
      const search = page.getByRole("searchbox", { name: "Find in visible map", exact: true });
      await search.fill(cluster.label);
      const row = page.getByTestId("workspace-graph-node-list")
        .locator(`li[data-graph-node-id="${cluster.id}"] button.workspace-graph-list-select`);
      await row.click();
      await expect(row).toHaveAttribute("aria-pressed", "true");
      const details = page.locator(".workspace-graph-details");
      await expect(details.getByRole("heading", { name: cluster.label, exact: true })).toBeVisible();
      const countFormat = new Intl.NumberFormat("en");
      await expect(details.locator("dt", { hasText: /^Members$/ }).locator("+ dd")).toHaveText(countFormat.format(cluster.member_count));
      await expect(details.locator("dt", { hasText: /^Relations$/ }).locator("+ dd")).toHaveText(countFormat.format(cluster.relation_count));
      await testInfo.attach("aggregated-cluster-selected.png", { body: await page.screenshot(), contentType: "image/png" });
      await search.clear();
      const expandedPromise = overview(page, workspace.id, cluster.id);
      await details.getByRole("button", { name: "Expand cluster", exact: true }).click();
      const expanded = await read(await expandedPromise);
      expect(expanded.graph_revision).toBe(scene.graph_revision);
      // Expansion may regroup other nodes to reveal members, but must make
      // progress rather than merely acknowledge an ignored expansion request.
      const previousDocuments = new Set(scene.nodes.filter(node => node.grain === "document").map(node => node.id));
      expect(expanded.nodes.some(node => node.grain === "document" && !previousDocuments.has(node.id))).toBe(true);
      await assertRendered(page, expanded);
      expect(errors).toEqual([]);
      await testInfo.attach("aggregated-cluster-expanded.png", { body: await page.screenshot(), contentType: "image/png" });
      await testInfo.attach("aggregated-api-gpu.json", {
        body: JSON.stringify({ source: "synthetic.v3 seed260917, original production API responses and native GL draws",
          scope: "functional selection/expansion; not GPU picking, normal-motion framing or hardware performance",
          before: { nodes: scene.nodes.length, edges: scene.edges.length, cluster },
          after: { nodes: expanded.nodes.length, edges: expanded.edges.length }, responses: reader.evidence() }),
        contentType: "application/json",
      });
    } finally {
      await reader.dispose();
    }
  });
});

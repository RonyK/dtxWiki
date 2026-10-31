import { expect, test, type Locator } from "@playwright/test";

import { currentWorkspace, login } from "./auth";

const GRAPH_OWNER_EMAIL = "e2e-graph-owner@example.com";

async function expectNavigationInside(link: Locator) {
  const geometry = await link.evaluate((linkElement) => {
    const container = linkElement.closest(".sidebar, .mobile-nav-drawer");
    if (!container) {
      throw new Error("Knowledge Map navigation container is required");
    }
    const parent = container.getBoundingClientRect();
    const item = linkElement.getBoundingClientRect();
    const label = linkElement.querySelector<HTMLElement>(".nav-item-label");
    const badge = linkElement.querySelector<HTMLElement>(".nav-preview-badge");
    if (!label || !badge) {
      throw new Error("Knowledge Map navigation label and badge are required");
    }
    return {
      badgeRight: badge.getBoundingClientRect().right,
      containerLeft: parent.left,
      containerRight: parent.right,
      labelWidth: label.clientWidth,
      labelScrollWidth: label.scrollWidth,
      linkLeft: item.left,
      linkRight: item.right,
    };
  });
  expect(geometry.linkLeft).toBeGreaterThanOrEqual(geometry.containerLeft);
  expect(geometry.linkRight).toBeLessThanOrEqual(geometry.containerRight - 1);
  expect(geometry.badgeRight).toBeLessThanOrEqual(geometry.containerRight - 1);
  expect(geometry.labelScrollWidth).toBeLessThanOrEqual(geometry.labelWidth);
}

for (const width of [1440, 1025]) {
  test(`Knowledge Map sidebar item stays inside its rail at ${width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await login(page.request, GRAPH_OWNER_EMAIL);
    const workspace = await currentWorkspace(page.request);
    await page.goto("/graph");

    const sidebar = page.locator("aside.sidebar");
    const link = sidebar.locator(`a[href="/workspaces/${workspace.id}/graph"]`);
    await expect(link).toHaveClass(/active/);
    await expect(link.locator(".nav-item-label")).toHaveText("Knowledge Map");
    await expect(link.locator(".nav-preview-badge")).toHaveText("Preview");
    await expectNavigationInside(link);
    await testInfo.attach(`knowledge-map-sidebar-${width}.png`, {
      body: await page.screenshot(),
      contentType: "image/png",
    });

    await page.getByRole("button", { name: "Collapse sidebar" }).click();
    await expect(sidebar).toHaveAttribute("data-collapsed", "true");
    await expect(link.locator(".nav-item-label")).toBeHidden();
    const collapsed = await Promise.all([
      sidebar.boundingBox(),
      link.boundingBox(),
    ]);
    expect(collapsed[0]).not.toBeNull();
    expect(collapsed[1]).not.toBeNull();
    expect(collapsed[1]!.x + collapsed[1]!.width).toBeLessThanOrEqual(
      collapsed[0]!.x + collapsed[0]!.width - 1,
    );
  });
}

test("Knowledge Map item stays inside the mobile navigation drawer", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page.request, GRAPH_OWNER_EMAIL);
  const workspace = await currentWorkspace(page.request);
  await page.goto("/graph");
  await page.getByRole("button", { name: "Open navigation" }).click();

  const drawer = page.getByRole("dialog", { name: "Mobile navigation" });
  const link = drawer.locator(`a[href="/workspaces/${workspace.id}/graph"]`);
  await expect(link).toBeVisible();
  await expectNavigationInside(link);
  await testInfo.attach("knowledge-map-mobile-drawer.png", {
    body: await page.screenshot(),
    contentType: "image/png",
  });
});

import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";

import { expect, test, type APIRequestContext, type APIResponse, type Locator, type Page } from "./fixtures";
import { authenticatePage, backendBaseUrl, currentWorkspace } from "./auth";

type PendingPage = { id: string; title: string; version_id: string; review_item_id: string };

// Ask promotion and acceptance are covered by backend integration tests. This
// journey covers the browser's evidence, group and single decision controls.
test("reads review evidence, approves a document group and a single review, and records Activity", async ({ page }, testInfo) => {
  await authenticatePage(page, testInfo, "review-group");
  const workspace = await currentWorkspace(page.request);
  const first = await pendingPage(page.request, workspace.id, "Grouped review candidate");
  const second = await pendingPage(page.request, workspace.id, "Independent review candidate");
  // Parallel reviews of one candidate are a supported ledger shape. Only the
  // duplicate ledger is seeded; content/submission/approval use real APIs.
  execFileSync(".venv/bin/python", [
    "-m", "tests.e2e.seed_review_siblings", workspace.id, first.review_item_id,
  ]);
  await page.goto(`/review?review=${first.review_item_id}`);
  const detail = page.locator(".review-detail-card");
  await expect(detail.locator("#review-detail-title")).toHaveText(first.title);
  await expect(detail.getByRole("button", { name: "Approve", exact: true })).toBeEnabled();
  await expect(detail.locator(".review-publication-notice")).toHaveText("First publication · nothing published yet → v1");
  await expect(detail.locator(".diff-viewer")).toContainText("Exact reviewed body");
  await detail.getByRole("tab", { name: "Review version content", exact: true }).click();
  await expect(detail.locator(".markdown-body")).toContainText("Exact reviewed body");
  await expect(detail.getByRole("link", { name: /Open current document:/ }))
    .toHaveAttribute("href", `/workspaces/${workspace.id}/context/pages/${first.id}`);
  await detail.getByText("Review information", { exact: true }).click();
  await expect(detail.getByText("Change evidence", { exact: true })).toBeVisible();

  const rail = page.locator(".review-rail");
  const firstGroup = rail.getByRole("region", { name: first.title, exact: true });
  await expect(firstGroup.locator(".review-row")).toHaveCount(3);
  await selectDocumentGroup(page, firstGroup);
  await firstGroup.getByRole("button", { name: "Approve selection", exact: true }).click();
  const confirmation = page.getByRole("dialog");
  await expect(confirmation).toContainText("Fixed selection: 3 items");
  await expect(confirmation).toContainText("2 other reviews of this version or its earlier drafts");
  await confirmation.getByRole("button", { name: "Approve 1", exact: true }).click();
  await expect(confirmation).toBeHidden();
  await expect(firstGroup).toHaveCount(0);
  await expect(page.locator(".review-bulk-outcomes")).toContainText("3 / 3");
  // The open review was decided by this run; it is not an outside-filters item.
  await expect(page.getByText(/outside the current queue filters/)).toHaveCount(0);
  const secondGroup = rail.getByRole("region", { name: second.title, exact: true });
  await expect(secondGroup).toBeVisible();
  await page.goto(`/review?review=${second.review_item_id}`);
  await expect(detail.locator("#review-detail-title")).toHaveText(second.title);
  await detail.getByText("Add a decision reason (optional)", { exact: true }).click();
  await detail.getByLabel("Decision reason").fill("Cross-screen workflow reviewed");
  await detail.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(secondGroup).toHaveCount(0);
  await page.goto("/activity");
  await expect(page.locator("article.activity-row").getByText("Review decision", { exact: true }).first()).toBeVisible();
});

// Engine behavior: a sticky decision header must not cover keyboard focus,
// and mobile review controls keep 44px targets without document overflow.
test("keeps focused review content and mobile review targets usable @cross-browser", async ({ page }, testInfo) => {
  await authenticatePage(page, testInfo, "review-layout");
  const workspace = await currentWorkspace(page.request);
  await pendingPage(page.request, workspace.id, "Second layout review");
  const candidate = await pendingPage(page.request, workspace.id, "Layout review");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/review?review=${candidate.review_item_id}`);
  const detail = page.locator(".review-detail-card");
  await expect(detail.getByRole("button", { name: "Approve", exact: true })).toBeEnabled();
  await expectNoDocumentOverflow(page);
  const comparison = detail.getByRole("tab", { name: "Compare changes", exact: true });
  await detail.getByRole("link", { name: /Open current document:/ }).focus();
  await comparison.evaluate((element) => {
    const main = element.closest<HTMLElement>(".review-main")!;
    const header = main.querySelector<HTMLElement>(".review-detail-header")!;
    const stickyTop = Number.parseFloat(getComputedStyle(header).top) || 0;
    const coveredTop = main.getBoundingClientRect().top + stickyTop + header.getBoundingClientRect().height - 8;
    main.scrollTop += element.getBoundingClientRect().top - coveredTop;
  });
  const covered = await reviewTabGeometry(comparison);
  expect(covered.tabTop).toBeLessThan(covered.headerBottom);
  await page.keyboard.press("Tab");
  await expect(comparison).toBeFocused();
  await expect.poll(async () => {
    const geometry = await reviewTabGeometry(comparison);
    return geometry.tabTop - geometry.headerBottom;
  }).toBeGreaterThanOrEqual(0);
  await page.getByRole("button", { name: "Open Review queue drawer", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Review queue drawer", exact: true });
  const rows = drawer.locator(".review-row-shell.has-bulk-select");
  await expect(rows).toHaveCount(2);
  for (const row of await rows.all()) {
    const target = row.locator(".review-row-select");
    await target.scrollIntoViewIfNeeded();
    const box = await target.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThanOrEqual(44);
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
  await expectNoDocumentOverflow(page);
});

async function pendingPage(request: APIRequestContext, workspaceId: string, title: string): Promise<PendingPage> {
  const path = `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages`;
  const created = await responseData<{ id: string; version_id: string; editing_revision: number }>(
    await request.post(path, { data: { title, body_md: `# ${title}\n\nExact reviewed body.\n` } }),
  );
  const submitted = await responseData<PendingPage>(await request.post(`${path}/${created.id}/submit-for-review`, {
    data: { version_id: created.version_id, expected_editing_revision: created.editing_revision, idempotency_key: randomUUID() },
  }));
  expect(submitted.review_item_id).toBeTruthy();
  return { ...submitted, id: created.id, title };
}

async function selectDocumentGroup(page: Page, group: Locator) {
  const selected = page.waitForResponse((response) => response.request().method() === "POST"
    && new URL(response.url()).pathname.endsWith("/reviews/selection"));
  const checkbox = group.getByRole("checkbox", { name: /Select all reviews matching/ });
  // The controlled checkbox becomes checked only after the complete server
  // snapshot returns; native check() requires an immediate local toggle.
  await checkbox.click();
  const response = await selected;
  expect(response.ok(), await response.text()).toBeTruthy();
  await expect(checkbox).toBeChecked();
  await expect(group).toHaveAttribute("aria-busy", "false");
}

async function responseData<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()).data as T;
}

async function expectNoDocumentOverflow(page: Page) {
  const geometry = await page.evaluate(() => ({
    width: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth,
    height: window.innerHeight, scrollHeight: document.documentElement.scrollHeight,
  }));
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.width + 1);
  expect(geometry.scrollHeight).toBeLessThanOrEqual(geometry.height + 1);
}

async function reviewTabGeometry(tab: Locator) {
  return tab.evaluate((element) => {
    const header = element.closest<HTMLElement>(".review-main")!.querySelector<HTMLElement>(".review-detail-header")!;
    return { tabTop: element.getBoundingClientRect().top, headerBottom: header.getBoundingClientRect().bottom };
  });
}

import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";

import { expect, test, type APIRequestContext, type APIResponse, type Locator, type Page } from "./fixtures";
import { authenticatePage, backendBaseUrl, currentWorkspace } from "./auth";

type PendingPage = { id: string; title: string; version_id: string; review_item_id: string };
type ReviewDetail = { status: string };

// The direct Context workflow covers exact content and same-candidate impact
// separately from the original Ask promotion journey below.
test("reads exact evidence, approves a document group, and records the decision in Activity", async ({ page }, testInfo) => {
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
  await expect(detail.locator(".review-publication-notice")).toHaveText("Approving publishes review version v1.");
  await expect(detail.locator(".diff-viewer")).toContainText("Exact reviewed body");
  await detail.getByRole("tab", { name: "Review version content", exact: true }).click();
  await expect(detail.locator(".markdown-body")).toContainText("Exact reviewed body");
  await expect(detail.getByRole("link", { name: /Open current document:/ }))
    .toHaveAttribute("href", `/workspaces/${workspace.id}/context/pages/${first.id}`);

  const rail = page.locator(".review-rail");
  const firstGroup = rail.getByRole("region", { name: first.title, exact: true });
  await expect(firstGroup.locator(".review-row")).toHaveCount(3);
  await selectDocumentGroup(page, firstGroup);
  await firstGroup.getByRole("button", { name: "Approve selection", exact: true }).click();
  const confirmation = page.getByRole("dialog");
  await expect(confirmation).toContainText("Fixed selection: 3 items");
  await expect(confirmation).toContainText("2 other reviews for the same candidate");
  await confirmation.getByRole("button", { name: "Approve 1", exact: true }).click();
  await expect(confirmation).toBeHidden();
  await expect(firstGroup).toHaveCount(0);
  await expect(page.locator(".review-bulk-outcomes")).toContainText("3 / 3");
  expect(["approved", "superseded"]).toContain((await reviewDetail(page.request, workspace.id, first.review_item_id)).status);
  expect((await reviewDetail(page.request, workspace.id, second.review_item_id)).status).toBe("pending");
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

async function reviewDetail(request: APIRequestContext, workspaceId: string, reviewId: string): Promise<ReviewDetail> {
  return responseData(await request.get(`${backendBaseUrl}/api/v1/workspaces/${workspaceId}/reviews/${reviewId}`));
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

test("reviews a retained Ask API proposal and records activity", async ({ page }, testInfo) => {
  await authenticatePage(page, testInfo, "cross-screen");
  const workspace = await currentWorkspace(page.request);
  const evidenceResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
    {
      data: {
        title: "Review workflow evidence",
        body_md: "# Review workflow evidence\n\nCapture the review workflow evidence.",
      },
    },
  );
  expect(evidenceResponse.ok()).toBeTruthy();
  const evidence = (await evidenceResponse.json()).data as {
    content_hash: string;
    id: string;
    version_id: string;
  };
  const publishEvidenceResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/` +
      `${evidence.id}/versions/${evidence.version_id}/publish`,
    {
      data: {
        expected_content_hash: evidence.content_hash,
        reason: "Seed evidence for the retained Ask API workflow.",
      },
    },
  );
  expect(publishEvidenceResponse.ok(), await publishEvidenceResponse.text())
    .toBeTruthy();

  // Ask's browser route is unavailable. Seed through its retained API so this
  // still exercises the real proposal, Review decision, and Activity workflow.
  const sessionResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/ask/sessions`,
    { data: {} },
  );
  expect(sessionResponse.ok(), await sessionResponse.text()).toBeTruthy();
  const sessionId = (await sessionResponse.json()).data.id as string;
  const turnResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/ask/sessions/${sessionId}/turns`,
    { data: { user_message: "Capture the review workflow evidence" } },
  );
  expect(turnResponse.ok(), await turnResponse.text()).toBeTruthy();
  const turn = (await turnResponse.json()).data as {
    id: string;
    cited_context_page_ids: string[];
  };
  expect(turn.cited_context_page_ids).toContain(evidence.id);
  const proposalResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/ask/turns/${turn.id}/promote`,
    {
      data: {
        save_mode: "create_page",
        page_type: "guide",
        title: "Cross-screen workflow answer",
      },
    },
  );
  expect(proposalResponse.ok(), await proposalResponse.text()).toBeTruthy();
  const proposal = (await proposalResponse.json()).data as {
    id: string;
    proposed_parent_tree_node_id: string;
    proposed_title: string;
  };
  // A retried Personal workspace can hold a pending request left by the prior
  // attempt, so scope the review assertions to the proposal this run accepts
  // instead of asserting a globally empty rail.
  const acceptResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/ingress-proposals/${proposal.id}/accept`,
    {
      data: {
        final_save_mode: "create_page",
        final_parent_node_id: proposal.proposed_parent_tree_node_id,
        final_title: proposal.proposed_title,
        final_page_type: "guide",
        review_required: true,
      },
    },
  );
  expect(acceptResponse.ok(), await acceptResponse.text()).toBeTruthy();
  const accepted = (await acceptResponse.json()).data as {
    proposal_id: string;
    review_item_id: string | null;
  };
  expect(accepted.proposal_id).toBe(proposal.id);
  expect(accepted.review_item_id).toBeTruthy();

  await page.goto(`/review?review=${encodeURIComponent(accepted.review_item_id!)}`);

  await expect(
    page.getByRole("complementary", { name: "Review queue" }),
  ).toBeVisible();
  const reviewRequests = page.locator(".review-row");
  // Select this run's exact review through its permalink; older pending
  // requests must not change which proposal the browser approves.
  await page.getByText("Review information", { exact: true }).click();
  await expect(page.getByText("Change evidence", { exact: true })).toBeVisible();
  await expect(
    page.getByText(`accept context ingress proposal ${proposal.id}`),
  ).toBeVisible();
  const pendingBefore = await reviewRequests.count();
  expect(pendingBefore).toBeGreaterThan(0);
  await page.getByText("Add a decision reason (optional)", { exact: true }).click();
  await page
    .getByLabel("Decision reason")
    .fill("Cross-screen workflow reviewed");
  await page.getByRole("button", { name: "Approve" }).click();

  await expect(reviewRequests).toHaveCount(pendingBefore - 1);

  await page.goto("/activity");

  await expect(
    page.getByRole("heading", { name: "Activity", exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator("article.activity-row")
      .getByText("Review decision", { exact: true })
      .first(),
  ).toBeVisible();
});

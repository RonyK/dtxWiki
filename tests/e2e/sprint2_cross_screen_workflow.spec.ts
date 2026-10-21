import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";

import { expect, test, type APIRequestContext, type APIResponse, type Locator, type Page } from "./fixtures";
import { authenticatePage, backendBaseUrl, currentWorkspace } from "./auth";

type PendingPage = { id: string; title: string; version_id: string; review_item_id: string };
type ReviewDetail = {
  status: string;
  decisions: Array<{ decision_id: string; decision_type: string; decision_reason: string | null }>;
};

// The direct Context workflow covers exact content and same-candidate impact
// separately from the original Ask promotion journey below.
test("reads exact evidence, approves a document group, and records the decision in Activity @cross-browser", async ({ page }, testInfo) => {
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
  await expect(detail.getByRole("tab", { name: "Compare changes" })).toHaveAttribute("aria-selected", "true");
  await expect(detail.locator(".diff-viewer")).toContainText("Exact reviewed body");
  await detail.getByRole("tab", { name: "Review version content", exact: true }).click();
  await expect(detail.locator(".markdown-body")).toContainText("Exact reviewed body");
  const currentDocument = detail.getByRole("link", { name: /Open current document:/ });
  await expect(currentDocument).toHaveAttribute("href", `/workspaces/${workspace.id}/context/pages/${first.id}`);
  await expect(currentDocument).toHaveAttribute("target", "_blank");
  await expect(detail.locator(".review-document-identity h3 a")).toHaveCount(0);
  await expectNoDocumentOverflow(page);

  const rail = page.locator(".review-rail");
  const firstGroup = rail.getByRole("region", { name: first.title, exact: true });
  const secondGroup = rail.getByRole("region", { name: second.title, exact: true });
  await expect(firstGroup.locator(".review-row")).toHaveCount(3);
  const checkboxLabels = await firstGroup.locator(".review-row-select input").evaluateAll(
    (inputs) => inputs.map((input) => input.getAttribute("aria-label")),
  );
  expect(checkboxLabels.every(Boolean)).toBe(true);
  expect(new Set(checkboxLabels).size).toBe(3);
  for (const variant of ["muted", "warn", "danger"]) {
    await expect(firstGroup.locator(`.review-row .status-pill-${variant}`)).toHaveCount(1);
  }
  await expect(firstGroup.locator(".review-document-kind")).toHaveText("Context");
  await expect(firstGroup.locator('[aria-current="true"]')).toHaveCount(1);
  const border = await firstGroup.locator('[aria-current="true"]').evaluate((element) => Number.parseFloat(getComputedStyle(element).borderLeftWidth));
  expect(border).toBeLessThanOrEqual(1.5);
  await page.getByRole("button", { name: "Collapse all", exact: true }).click();
  await expect(firstGroup.locator(".review-document-group-body")).toBeHidden();
  await expect(secondGroup.locator(".review-document-group-body")).toBeHidden();
  await expect(firstGroup.locator(".review-document-group-header").getByText("Viewing", { exact: true })).toBeVisible();
  await expect(detail.locator(".markdown-body")).toContainText("Exact reviewed body");
  await page.getByRole("button", { name: "Expand all", exact: true }).click();
  await expect(firstGroup.locator(".review-document-group-body")).toBeVisible();
  await selectDocumentGroup(page, secondGroup);
  await selectDocumentGroup(page, firstGroup);
  await expect(firstGroup.getByText("3 selected for this document", { exact: true })).toBeVisible();
  const previewResponse = page.waitForResponse((response) => response.request().method() === "POST"
    && new URL(response.url()).pathname.endsWith("/reviews/selection"));
  await rail.getByRole("button", { name: "Approve selected", exact: true }).click();
  const preview = await responseData<{
    eligible_count: number;
    auto_settled_review_item_ids: string[];
    items: Array<{ id: string; eligible: boolean; target_title: string; target_version_no: number;
      auto_settled_review_item_ids: string[] }>;
  }>(await previewResponse);
  expect(preview.eligible_count).toBe(2);
  const firstItems = preview.items.filter((item) => item.target_title === first.title);
  const firstRepresentative = firstItems.find((item) => item.eligible)!;
  const secondRepresentative = preview.items.find((item) => item.target_title === second.title && item.eligible)!;
  expect(firstItems).toHaveLength(3);
  expect([...firstRepresentative.auto_settled_review_item_ids].sort()).toEqual(
    firstItems.filter((item) => item.id !== firstRepresentative.id).map((item) => item.id).sort(),
  );
  expect(secondRepresentative.auto_settled_review_item_ids).toEqual([]);
  expect([...preview.auto_settled_review_item_ids].sort()).toEqual([...firstRepresentative.auto_settled_review_item_ids].sort());
  const confirmation = page.getByRole("dialog");
  await expect(confirmation).toContainText("Fixed selection: 4 items");
  const previewItems = confirmation.locator(".review-bulk-preview-items > li");
  const firstImpact = previewItems.filter({ hasText: first.title }).filter({ hasText: "Eligible for approval" });
  const secondImpact = previewItems.filter({ hasText: second.title });
  await expect(firstImpact.locator("strong")).toHaveText(`${first.title} · v${firstRepresentative.target_version_no}`);
  await expect(firstImpact.locator(".review-bulk-item-impact")).toContainText("2 other reviews for the same candidate");
  await expect(secondImpact.locator("strong")).toHaveText(`${second.title} · v${secondRepresentative.target_version_no}`);
  await expect(secondImpact.locator(".review-bulk-item-impact")).toHaveCount(0);
  await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(confirmation).toBeHidden();
  await firstGroup.getByRole("button", { name: "Approve selection", exact: true }).click();
  await expect(confirmation).toContainText("Fixed selection: 3 items");
  await expect(confirmation).toContainText("2 other reviews for the same candidate");
  await confirmation.getByRole("button", { name: "Approve 1", exact: true }).click();
  await expect(confirmation).toBeHidden();
  await expect(firstGroup).toHaveCount(0);
  await expect(secondGroup.getByRole("checkbox", { name: /Select all reviews matching/ })).toBeChecked();
  await expect(page.locator(".review-bulk-outcomes")).toContainText("3 / 3");
  expect(["approved", "superseded"]).toContain((await reviewDetail(page.request, workspace.id, first.review_item_id)).status);
  expect((await reviewDetail(page.request, workspace.id, second.review_item_id)).status).toBe("pending");
  await page.goto("/activity");
  await expect(page.getByRole("heading", { name: "Activity", exact: true })).toBeVisible();
  await expect(page.locator("article.activity-row").getByText("Review decision", { exact: true }).first()).toBeVisible();
});

test("preserves a change-request reason and retries one unreceived decision with its original ID @cross-browser", async ({ page }, testInfo) => {
  await authenticatePage(page, testInfo, "review-uncertain");
  const workspace = await currentWorkspace(page.request);
  const candidate = await pendingPage(page.request, workspace.id, "Recover review request");
  const attempts: Array<{ decision_id: string; decision_reason: string; audience_kind: string }> = [];
  await page.route(`**/reviews/${candidate.review_item_id}/decision`, async (route) => {
    attempts.push(route.request().postDataJSON());
    if (attempts.length === 1) await route.abort("failed");
    else await route.continue();
  });
  await page.goto(`/review?review=${candidate.review_item_id}`);
  const detail = page.locator(".review-detail-card");
  await detail.getByRole("button", { name: "Request changes", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Request changes", exact: true });
  await dialog.getByRole("radio", { name: /^Me/ }).check();
  await dialog.getByLabel("Decision reason", { exact: true }).fill("Keep this reason across a failed send.");
  await dialog.getByRole("button", { name: "Request changes", exact: true }).click();
  await expect(page.getByText(/The decision result is unconfirmed/)).toBeVisible();
  await expect(dialog.getByLabel("Decision reason", { exact: true })).toHaveValue("Keep this reason across a failed send.");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(detail.getByRole("button", { name: "Request changes", exact: true })).toBeDisabled();
  await page.reload();
  await expect(page.getByRole("button", { name: "Retry the same decision", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Retry the same decision", exact: true }).click();
  await expect(detail.getByText(/Requested changes saved/)).toBeVisible();
  expect(attempts).toHaveLength(2);
  expect(attempts[0]).toEqual(attempts[1]);
  expect(attempts[1].decision_id).toMatch(/^[0-9a-f-]{36}$/);
  const saved = await reviewDetail(page.request, workspace.id, candidate.review_item_id);
  expect(saved.status).toBe("changes_requested");
  const decisions = saved.decisions.filter((item) => item.decision_type === "request_changes");
  expect(decisions).toHaveLength(1);
  expect(decisions[0].decision_id).toBe(attempts[0].decision_id);
  expect(decisions[0].decision_reason).toBe("Keep this reason across a failed send.");
});

test("keeps a saved mobile approval after response loss and queue failure, then opens the next review explicitly @cross-browser", async ({ page }, testInfo) => {
  await authenticatePage(page, testInfo, "review-mobile-receipt");
  const workspace = await currentWorkspace(page.request);
  const next = await pendingPage(page.request, workspace.id, "Next review remains");
  const candidate = await pendingPage(page.request, workspace.id, "Mobile saved approval");
  let committed = false;
  let decisionsSent = 0;
  await page.route(`**/reviews/${candidate.review_item_id}/decision`, async (route) => {
    decisionsSent += 1;
    const response = await route.fetch();
    expect(response.ok()).toBeTruthy();
    committed = true;
    await route.abort("failed");
  });
  const queuePattern = /\/reviews\?[^#]*$/;
  await page.route(queuePattern, async (route) => {
    if (committed) await route.fulfill({ status: 503, json: {
      error_code: "test_queue_unavailable", message: "Queue unavailable", request_id: "e2e-review-queue", warnings: [],
    } });
    else await route.continue();
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/review?review=${candidate.review_item_id}`);
  const detail = page.locator(".review-detail-card");
  const approve = detail.getByRole("button", { name: "Approve", exact: true });
  await expect(approve).toBeEnabled();
  await expectNoDocumentOverflow(page);
  await page.getByRole("button", { name: "Open Review queue drawer", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Review queue drawer", exact: true });
  await expect(drawer).toBeVisible();
  const mobileRows = drawer.locator(".review-row-shell.has-bulk-select");
  await expect(mobileRows).toHaveCount(2);
  const touchTargets = [];
  for (const row of await mobileRows.all()) {
    const target = await row.locator(".review-row-select").boundingBox();
    const title = await row.locator(".review-row-identity strong").boundingBox();
    expect(target).not.toBeNull();
    expect(title).not.toBeNull();
    expect(target!.width).toBeGreaterThanOrEqual(44);
    expect(target!.height).toBeGreaterThanOrEqual(44);
    expect(target!.x + target!.width).toBeLessThanOrEqual(title!.x);
    touchTargets.push({ target, title });
  }
  await testInfo.attach("review-mobile-touch-targets", {
    body: JSON.stringify(touchTargets), contentType: "application/json",
  });
  await page.getByRole("button", { name: "Close Review queue drawer", exact: true }).click();
  await expect(drawer).toBeHidden();
  await approve.click();
  await expect(detail.getByText(/Approved saved/)).toBeVisible();
  await expect(detail.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
  await expect(detail.getByRole("button", { name: "Next review", exact: true })).toBeDisabled();
  await expect(page).toHaveURL(new RegExp(`review=${candidate.review_item_id}$`));
  expect(decisionsSent).toBe(1);
  const saved = await reviewDetail(page.request, workspace.id, candidate.review_item_id);
  expect(saved.status).toBe("approved");
  expect(saved.decisions.filter((item) => item.decision_type === "approve")).toHaveLength(1);
  await page.unroute(queuePattern);
  await page.getByRole("button", { name: "Reload queue", exact: true }).click();
  await expect(detail.getByRole("button", { name: "Next review", exact: true })).toBeEnabled();
  await detail.getByRole("button", { name: "Next review", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`review=${next.review_item_id}$`));
  await expect(page.locator("#review-detail-title")).toHaveText(next.title);
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

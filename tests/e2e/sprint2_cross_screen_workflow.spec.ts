import { expect, test } from "./fixtures";

import { authenticatePage, backendBaseUrl, currentWorkspace } from "./auth";

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
  const reviewRequests = page.getByRole("button", {
    name: /context ingress accept/i,
  });
  // Select this run's exact review through its permalink; older pending
  // requests must not change which proposal the browser approves.
  await expect(page.getByText("Change evidence")).toBeVisible();
  await expect(
    page.getByText(`accept context ingress proposal ${proposal.id}`),
  ).toBeVisible();
  const pendingBefore = await reviewRequests.count();
  expect(pendingBefore).toBeGreaterThan(0);
  await page
    .getByLabel("Decision reason")
    .fill("Cross-screen workflow reviewed");
  const decisionResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.startsWith(
        `/api/v1/workspaces/${workspace.id}/reviews/`,
      ) &&
      new URL(response.url()).pathname.endsWith("/decision"),
  );
  await page.getByRole("button", { name: "Approve" }).click();
  const decisionResponse = await decisionResponsePromise;
  expect(decisionResponse.ok()).toBeTruthy();
  const reviewId = (await decisionResponse.json()).data.id;
  expect(reviewId).toEqual(expect.any(String));

  await expect(reviewRequests).toHaveCount(pendingBefore - 1);

  const feedResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      new URL(response.url()).pathname ===
        `/api/v1/workspaces/${workspace.id}/activity/feed`,
  );
  await page.goto("/activity");
  const feedResponse = await feedResponsePromise;
  expect(feedResponse.ok()).toBeTruthy();
  const feed = (await feedResponse.json()).data;
  expect(feed.entries).toEqual(expect.arrayContaining([
    expect.objectContaining({
      event: expect.objectContaining({
        body: "review.decision",
        target: expect.objectContaining({
          target_kind: "review_item",
          target_id: reviewId,
        }),
      }),
    }),
  ]));

  await expect(
    page.getByRole("heading", { name: "Activity", exact: true }),
  ).toBeVisible();
  const decisionRow = page.getByRole("article").filter({
    has: page.getByText("Review decision", { exact: true }),
  });
  await expect(decisionRow).toHaveCount(1);
  await expect(decisionRow).toBeVisible();
  const historyLink = decisionRow.getByRole("link", {
    name: "View activity list",
    exact: true,
  });
  await historyLink.click();
  await expect(page).toHaveURL((url) =>
    url.pathname === "/activity/history" &&
    url.searchParams.get("period") === "all" &&
    url.searchParams.get("kind") === "all" &&
    url.searchParams.get("subtype") === "all" &&
    url.searchParams.get("group_reads") === "1",
  );
  await expect(decisionRow).toHaveCount(1);
  await expect(decisionRow).toBeVisible();
});

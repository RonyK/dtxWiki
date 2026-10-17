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

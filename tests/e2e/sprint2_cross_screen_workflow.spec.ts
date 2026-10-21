import { expect, test } from "./fixtures";

import { authenticatePage, backendBaseUrl, currentWorkspace } from "./auth";

test("promotes a ask answer through review and activity", async ({ page }, testInfo) => {
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
        reason: "Seed browser-visible Ask evidence.",
      },
    },
  );
  expect(publishEvidenceResponse.ok(), await publishEvidenceResponse.text())
    .toBeTruthy();

  await page.goto("/ask");

  const composer = page.getByRole("textbox", { name: "Message" });
  await expect(composer).toBeVisible();
  await composer.fill("Capture the review workflow evidence");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.getByText("Turn 1")).toBeVisible();
  await page.getByRole("button", { name: "Save answer" }).click();

  await expect(page.getByRole("heading", { name: "Save answer" })).toBeVisible();
  await page.getByRole("checkbox", { name: "Require review" }).check();
  await page.getByRole("button", { name: "Propose placement" }).click();

  await expect(
    page.getByRole("button", { name: "Accept placement" }),
  ).toBeEnabled();
  // A retried Personal workspace can hold a pending request left by the prior
  // attempt, so scope the review assertions to the proposal this run accepts
  // instead of asserting a globally empty rail.
  const acceptResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/context\/ingress-proposals\/[^/]+\/accept$/.test(response.url()),
  );
  await page.getByRole("button", { name: "Accept placement" }).click();
  const acceptResponse = await acceptResponsePromise;
  expect(acceptResponse.ok()).toBeTruthy();
  const proposalId = acceptResponse
    .url()
    .match(/\/context\/ingress-proposals\/([^/]+)\/accept$/)?.[1];
  expect(proposalId).toBeTruthy();

  await expect(page.getByText("Saved for review")).toBeVisible();

  await page.goto("/review");

  await expect(
    page.getByRole("complementary", { name: "Review queue" }),
  ).toBeVisible();
  const reviewRequests = page.getByRole("button", {
    name: /context ingress accept/i,
  });
  // The rail sorts pending requests newest-first, so the request created
  // moments ago is the first row; the evidence reason pins it to this
  // run's proposal before approving.
  const reviewRequest = reviewRequests.first();
  await expect(reviewRequest).toBeVisible();
  const pendingBefore = await reviewRequests.count();
  await reviewRequest.click();
  await expect(page.getByText("Change evidence")).toBeVisible();
  await expect(
    page.getByText(`accept context ingress proposal ${proposalId}`),
  ).toBeVisible();
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

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

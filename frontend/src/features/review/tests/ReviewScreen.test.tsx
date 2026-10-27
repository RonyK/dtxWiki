import { readAppStyles } from "../../../test-support/appStyles";

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  formatRelativeTimestamp,
  formatVersionTimestamp,
} from "../../../shared/lib/datetime";
import { i18n } from "../../../shared/i18n";
import { ReviewScreen } from "../ReviewScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

function workspaceResponse(role?: string) {
  return {
    data: {
      current_workspace_id: WORKSPACE_ID,
      workspaces: role
        ? [{ id: WORKSPACE_ID, slug: "default", title: "Default", role }]
        : [],
    },
  };
}

describe("ReviewScreen", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("defines routing dialog radio layout styles", () => {
    const styles = readAppStyles();

    expect(styles).toContain(".review-routing-options");
    expect(styles).toContain("grid-template-columns: auto 1fr;");
    expect(styles).toContain(".review-routing-option-hint");
    expect(styles).toContain(".review-rail .reader-tabs");
    expect(styles).toContain("white-space: nowrap;");
    expect(styles).toContain(".review-row-main p");
    expect(styles).toContain(".review-row-target-meta");
    expect(styles).toContain(".review-row-type");
    expect(styles).toMatch(
      /@media \(max-width: 1024px\), \(any-pointer: coarse\)[\s\S]*\.review-bulk-select-all,[\s\S]*\.review-bulk-bar \.icon-button,[\s\S]*\.review-queue-card__header[\s\S]*min-height: 44px;/,
    );
  });

  it("keeps the Preview badge out of the Review rail header", async () => {
    mockReviewQueue([reviewItem()]);
    const { container } = render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Publish the selected Context draft");
    const railHeader = container.querySelector(".review-rail-header");
    expect(railHeader).not.toBeNull();
    expect(railHeader?.querySelector(".nav-preview-badge")).toBeNull();
    expect(within(railHeader as HTMLElement).queryByText("Preview")).toBeNull();
  });

  it("uses localized initial-load copy instead of exposing a generic error", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new Error("raw request req-private-review-load"),
    );
    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Failed to load Review")).toBeInTheDocument();
    expect(screen.queryByText(/req-private-review-load/)).toBeNull();
  });

  it("reports a localized refresh failure and leaves the loaded queue visible", async () => {
    const user = userEvent.setup();
    let reviewListCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
        return jsonResponse({ data: {} });
      }
      if (
        url
        === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`
      ) {
        reviewListCalls += 1;
        if (reviewListCalls > 1) {
          throw new Error("raw request req-private-review-refresh");
        }
        return jsonResponse({ data: { items: [reviewItem()], next_cursor: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Publish the selected Context draft"))
      .toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh reviews" }));

    expect(await screen.findByText("Failed to refresh Review")).toBeInTheDocument();
    expect(screen.queryByText(/req-private-review-refresh/)).toBeNull();
    expect(screen.getByText("Publish the selected Context draft")).toBeVisible();
  });

  it("opens review detail and approves without a reason", async () => {
    let decisionBody: unknown = null;
    let decisionCount = 0;
    const clipboardWrite = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        // Editor role so the workspace-wide tab count badges render below.
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({
          data: reviewCounts({
            pending: decisionCount === 0 ? 1 : 0,
            resolved: decisionCount === 0 ? 0 : 1,
          }),
        });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
        return jsonResponse({
          data: {
            review: {
              name: "review",
              title: "Review risk policy",
              policy: {
                preset: "balanced",
                automation: {
                  auto_approve_by_risk: {
                    low: { enabled: true, delay_days: 7 },
                    medium: { enabled: true, delay_days: 7 },
                    high: { enabled: false, delay_days: 7 },
                  },
                },
              },
            },
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: {
            items: decisionCount === 0 ? [reviewItem()] : [],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-1/decision`) {
        decisionBody = JSON.parse(String(init?.body));
        decisionCount += 1;
        return jsonResponse({ data: { ...reviewItem(), status: "approved" } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Context page publish")).toBeInTheDocument();
    expect(screen.getByText("Review risk policy")).toBeInTheDocument();
    expect(screen.getByText("Eligible now")).toBeInTheDocument();
    // The lifecycle tabs render in the rail without opening "Filters".
    expect(screen.getByRole("tab", { name: /Pending.*1/ })).toBeInTheDocument();
    const reviewDateLabel = formatReviewDateForTest(reviewItem().created_at);
    expect(screen.getByText(reviewDateLabel)).toBeInTheDocument();
    await userEvent.click(screen.getByText("Publish the selected Context draft"));
    expect(screen.getByText("Review detail / Context page publish")).toBeInTheDocument();
    expect(screen.getByText("Change evidence")).toBeInTheDocument();
    expect(screen.getByText("Draft created")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Feedback Target" })).toBeInTheDocument();
    expect(screen.getByText(/Create draft - feedback-target - v1/)).toBeInTheDocument();
    expect(screen.getByText("77a2c536...4fdc")).toHaveAttribute(
      "title",
      "77a2c536-ff53-4479-a0ae-65fb20c64fdc",
    );
    expect(screen.getAllByTitle("high")[0]).toHaveClass("status-pill-danger");
    expect(screen.getByText("Citation check")).toBeInTheDocument();
    expect(screen.getByText("Passed")).toHaveClass("status-pass");
    expect(screen.getByText("Why this risk?")).toBeInTheDocument();
    expect(screen.getByText("Context draft create classified as high risk."))
      .toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Copy full Change Set ID" }));
    expect(clipboardWrite).toHaveBeenCalledWith(
      "77a2c536-ff53-4479-a0ae-65fb20c64fdc",
    );
    expect(await screen.findByRole("button", { name: "Change Set ID copied" })).toBeVisible();
    const approveButton = screen.getByRole("button", { name: "Approve" });
    const requestChangesButton = screen.getByRole("button", {
      name: "Request changes",
    });
    const rejectButton = screen.getByRole("button", { name: "Reject" });
    expect(approveButton).toBeEnabled();
    expect(approveButton).toHaveClass("primary");
    expect(requestChangesButton).toBeEnabled();
    expect(requestChangesButton).toHaveClass("warning");
    // Reject is now always clickable; a missing reason opens a prompt instead
    // of disabling the button (item 6).
    expect(rejectButton).toBeEnabled();
    expect(rejectButton).toHaveClass("danger");
    // The decisions are the detail heading's action cluster, like every other
    // detail screen, not a strip floating below the header.
    expect(approveButton.closest(".detail-heading-actions")).not.toBeNull();
    expect(approveButton.closest(".review-detail-header")).not.toBeNull();
    expect(
      approveButton.compareDocumentPosition(screen.getByText("Citation check")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      approveButton.compareDocumentPosition(screen.getByLabelText("Decision reason")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    await userEvent.click(approveButton);

    expect(decisionBody).toEqual({
      decision_type: "approve",
      decision_reason: null,
    });
    // The rail empty state now names the lane it is empty for, so "nothing to
    // review" no longer reads as "nothing anywhere".
    expect(await screen.findByText("No pending items")).toBeInTheDocument();
  });

  it("writes the rail empty state per lane instead of splicing the tab label", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ pending: 0 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url.includes("/reviews?")) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    // Each lane owns its phrasing: "No Changes requested items" was ungrammatical
    // and mid-sentence capitalised because the tab label was interpolated.
    expect(await screen.findByText("No pending items")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /^Changes requested/ }));
    expect(
      await screen.findByText("No change-requested items"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /^Resolved/ }));
    expect(await screen.findByText("No resolved items")).toBeInTheDocument();
  });

  it("distinguishes same-type queue rows by their target titles", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ pending: 2 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
        return jsonResponse({ data: {} });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: {
            items: [
              reviewItem({ id: "review-a", targetTitle: "Research Persona" }),
              reviewItem({ id: "review-b", targetTitle: "Support Persona" }),
            ],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Research Persona")).toBeInTheDocument();
    expect(screen.getByText("Support Persona")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Research Persona/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Support Persona/ }))
      .toBeInTheDocument();
  });

  it("shows the target version on a queue row", async () => {
    mockReviewQueue([
      reviewItem({
        targetTitle: "Versioned Context page",
        targetVersionNo: 3,
      }),
    ]);

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    const target = await screen.findByText("Versioned Context page");
    const row = target.closest(".review-row");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("v3")).toBeInTheDocument();
  });

  it("groups loaded pending drafts for one page and orders them by version", async () => {
    mockReviewQueue([
      reviewItem({
        id: "review-a-v1",
        reviewType: "context_update",
        targetId: "page-a",
        targetTitle: "Grouped page",
        targetVersionNo: 1,
        createdAt: "2026-05-01T00:00:00Z",
      }),
      reviewItem({
        id: "review-b-v9",
        reviewType: "context_update",
        targetId: "page-b",
        targetTitle: "Other page",
        targetVersionNo: 9,
        createdAt: "2026-05-04T00:00:00Z",
      }),
      reviewItem({
        id: "review-a-v3",
        reviewType: "context_update",
        targetId: "page-a",
        targetTitle: "Grouped page",
        targetVersionNo: 3,
        createdAt: "2026-05-05T00:00:00Z",
      }),
      reviewItem({
        id: "review-a-v2",
        reviewType: "context_update",
        targetId: "page-a",
        targetTitle: "Grouped page",
        targetVersionNo: 2,
        createdAt: "2026-05-03T00:00:00Z",
      }),
    ]);

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    // The rail loaded every page (no next cursor), so 3 IS the page's total
    // and the header states it rather than hedging with "shown".
    expect(
      await screen.findByRole("group", {
        name: "Grouped page · 3 pending drafts",
      }),
    ).toBeInTheDocument();
    await screen.findByText("Other page");
    expect(
      [...document.querySelectorAll(".review-row .review-version-badge")].map(
        (badge) => badge.textContent,
      ),
    ).toEqual(["v3", "v2", "v1", "v9"]);
  });

  it("says only what it can see while more results are still outstanding", async () => {
    mockReviewQueue(
      [
        reviewItem({
          id: "review-a-v1",
          reviewType: "context_update",
          targetId: "page-a",
          targetTitle: "Grouped page",
          targetVersionNo: 1,
          createdAt: "2026-05-01T00:00:00Z",
        }),
        reviewItem({
          id: "review-a-v2",
          reviewType: "context_update",
          targetId: "page-a",
          targetTitle: "Grouped page",
          targetVersionNo: 2,
          createdAt: "2026-05-03T00:00:00Z",
        }),
      ],
      "cursor-2",
    );

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    // A third draft for this page may sit on the next page of results, so the
    // header must not claim 2 is the total.
    expect(
      await screen.findByRole("group", {
        name: "Grouped page · 2 pending drafts shown",
      }),
    ).toBeInTheDocument();
  });

  it.each([
    {
      count: 1,
      copy:
        "Approving will publish this version. 1 earlier pending review will be settled with it.",
    },
    {
      count: 2,
      copy:
        "Approving will publish this version. 2 earlier pending reviews will be settled with it.",
    },
  ])(
    "warns before approval when $count earlier review(s) will be settled",
    async ({ count, copy }) => {
      mockReviewQueue([
        reviewItem({
          reviewType: "context_update",
          supersededReviewCount: count,
          targetTitle: `Settlement impact ${count}`,
        }),
      ]);

      render(
        <MemoryRouter>
          <ReviewScreen />
        </MemoryRouter>,
      );

      await userEvent.click(await screen.findByText(`Settlement impact ${count}`));
      expect(await screen.findByText(copy)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    },
  );

  it("adds no settlement warning when approving will settle zero earlier reviews", async () => {
    mockReviewQueue([
      reviewItem({
        reviewType: "context_update",
        supersededReviewCount: 0,
        targetTitle: "No settlement impact",
      }),
    ]);

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByText("No settlement impact"));
    expect(await screen.findByRole("heading", { name: "No settlement impact" }))
      .toBeInTheDocument();
    expect(screen.queryByText(/earlier pending reviews?/i)).toBeNull();
  });

  it("keeps old reconciliation branches collapsed until explicitly requested", async () => {
    const user = userEvent.setup();
    const requestedUrls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      requestedUrls.push(url);
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ pending: 2 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
        return jsonResponse({ data: {} });
      }
      if (
        url
        === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`
      ) {
        return jsonResponse({
          data: {
            items: [reviewItem({ summary: "Recent review" })],
            next_cursor: null,
            deferred_reconciliation_count: 1,
          },
        });
      }
      if (
        url
        === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50&include_deferred_reconciliations=true`
      ) {
        return jsonResponse({
          data: {
            items: [
              reviewItem({ summary: "Recent review" }),
              reviewItem({
                id: "old-reconciliation",
                reviewType: "context_reconciliation",
                summary: "Old reconciliation branch",
              }),
            ],
            next_cursor: null,
            deferred_reconciliation_count: 1,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Recent review")).toBeInTheDocument();
    expect(screen.queryByText("Old reconciliation branch")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Filters" }));
    await user.click(
      screen.getByRole("checkbox", {
        name: "Show 1 older reconciliation branch",
      }),
    );

    expect(await screen.findByText("Old reconciliation branch"))
      .toBeInTheDocument();
    expect(requestedUrls).toContain(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50&include_deferred_reconciliations=true`,
    );
  });

  it("localizes stable summary codes in Korean and preserves legacy prose", async () => {
    await i18n.changeLanguage("ko");
    try {
      mockReviewQueue([
        reviewItem({
          id: "review-context-update",
          summary: "context update draft requires review",
          summaryCode: "context_update_requires_review",
          targetTitle: "Translated summary page",
          diffSummary: "stale edit preserved as alternative draft",
        }),
        reviewItem({
          id: "review-citation-warning",
          summary: "workflow_sequence_incomplete",
          summaryCode: "context_update_workflow_sequence_incomplete",
          targetTitle: "Workflow warning page",
        }),
        reviewItem({
          id: "review-legacy-summary",
          summary: "context draft requires review",
          summaryCode: null,
          targetTitle: "Legacy summary page",
        }),
        reviewItem({
          id: "review-custom-summary",
          summary: "Custom backend audit summary",
          summaryCode: null,
          targetTitle: "Custom summary page",
        }),
        reviewItem({
          id: "review-unknown-code",
          summary: "Future backend summary",
          summaryCode: "future_summary_code",
          targetTitle: "Future summary page",
        }),
      ]);

      render(
        <MemoryRouter>
          <ReviewScreen />
        </MemoryRouter>,
      );

      expect(
        await screen.findByText("Context 수정 초안을 리뷰해야 합니다"),
      ).toBeInTheDocument();
      expect(
        screen.getByText(
          "워크플로 순서가 완료되지 않아 Context 수정 초안을 리뷰해야 합니다",
        ),
      ).toBeInTheDocument();
      expect(screen.queryByText("workflow_sequence_incomplete")).toBeNull();
      expect(screen.getByText("context draft requires review")).toBeInTheDocument();
      expect(screen.getByText("Custom backend audit summary")).toBeInTheDocument();
      expect(screen.getByText("Future backend summary")).toBeInTheDocument();

      await userEvent.click(screen.getByText("Translated summary page"));
      expect(
        await screen.findAllByText("Context 수정 초안을 리뷰해야 합니다"),
      ).toHaveLength(2);
      expect(
        await screen.findByText("stale edit preserved as alternative draft"),
      ).toBeInTheDocument();
    } finally {
      await act(async () => {
        await i18n.changeLanguage("en");
      });
    }
  });

  it("does not repeat a summary used as the row title fallback", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
        return jsonResponse({ data: {} });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: {
            items: [reviewItem({ targetTitle: null })],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findAllByText("Publish the selected Context draft"),
    ).toHaveLength(1);
  });

  it("rejects without requiring a decision reason", async () => {
    let decisionBody: unknown = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: { items: [reviewItem()], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-1/decision`) {
        decisionBody = JSON.parse(String(init?.body));
        return jsonResponse({ data: { ...reviewItem(), status: "rejected" } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByText("Publish the selected Context draft"));
    const rejectButton = await screen.findByRole("button", { name: "Reject" });
    expect(rejectButton).toBeEnabled();
    // Reject no longer requires a reason, but it is terminal, so the first click
    // opens a confirm dialog rather than submitting. No decision is sent until
    // the reviewer confirms inside the dialog.
    await userEvent.click(rejectButton);
    expect(decisionBody).toBeNull();

    const dialog = await screen.findByRole("dialog", {
      name: "Reject this review?",
    });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Reject" }),
    );

    expect(decisionBody).toEqual({
      decision_type: "reject",
      decision_reason: null,
    });
    // The confirm dialog closes once the decision lands.
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Reject this review?" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("cancels the reject confirm without submitting a decision", async () => {
    let decisionRequested = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: { items: [reviewItem()], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-1/decision`) {
        decisionRequested = true;
        return jsonResponse({ data: { ...reviewItem(), status: "rejected" } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByText("Publish the selected Context draft"));
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));

    const dialog = await screen.findByRole("dialog", {
      name: "Reject this review?",
    });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Cancel" }),
    );

    // Cancelling dismisses the dialog and never hits the decision endpoint.
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Reject this review?" }),
      ).not.toBeInTheDocument(),
    );
    expect(decisionRequested).toBe(false);
    // The detail stays open so the reviewer can pick a different action.
    expect(
      screen.getByRole("heading", { name: "Feedback Target" }),
    ).toBeInTheDocument();
  });

  it("renders the decision ledger with humanized enum labels", async () => {
    // A review whose ledger covers decision_type/actor_type/change_set_status —
    // each must render through domainLabel, never as a raw snake_case enum.
    const decided = {
      ...reviewItem(),
      decisions: [
        {
          decision_id: "decision-1",
          decision_type: "request_changes",
          actor_type: "system",
          actor_id: null,
          actor_display_name: null,
          decision_reason: null,
          audience_kind: null,
          audience_user_id: null,
          resulting_review_status: "changes_requested",
          resulting_change_set_status: "revision_pending",
          created_at: "2026-05-09T01:00:00Z",
        },
      ],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [decided], next_cursor: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByText("Publish the selected Context draft"));
    // decision_type -> labels.decisionType, actor_type -> labels.actorType,
    // resulting_review_status -> humanizeEnum, resulting_change_set_status ->
    // labels.changeSetStatus.
    expect(await screen.findByText("Requested changes")).toBeInTheDocument();
    expect(
      screen.getByText("System -> Changes requested / Revision pending"),
    ).toBeInTheDocument();
    expect(screen.queryByText("request_changes")).not.toBeInTheDocument();
    expect(screen.queryByText(/revision_pending/)).not.toBeInTheDocument();
  });

  it("labels a changes_requested status pill with the translated copy", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: {
            items: [reviewItem({ status: "changes_requested" })],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    // The rail is scoped to one lifecycle, so it no longer repeats a status
    // pill per row; the detail heading is where the status is stated. The pill
    // carries the warn tone but the translated "Changes requested" label (C4
    // StatusPill label routing), never raw snake_case.
    await userEvent.click(
      await screen.findByText("Publish the selected Context draft"),
    );
    const pill = screen
      .getAllByText("Changes requested")
      .find((node) => node.classList.contains("status-pill"));
    expect(pill).toBeDefined();
    expect(pill).toHaveClass("status-pill-warn");
    expect(pill?.closest(".review-detail-header")).not.toBeNull();
    expect(screen.queryByText("changes_requested")).not.toBeInTheDocument();
    expect(screen.queryByText("changes requested")).not.toBeInTheDocument();
  });

  it("disables reject when the current actor cannot decide", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: { items: [reviewItem({ canDecide: false })], next_cursor: null },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByText("Publish the selected Context draft"));

    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });

  it("keeps the lifecycle tabs outside the Filters disclosure", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({
          data: reviewCounts({ changes_requested: 2, resolved: 3 }),
        });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [reviewItem()], next_cursor: null } });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=changes_requested&limit=50`
      ) {
        return jsonResponse({
          data: {
            items: [
              reviewItem({
                id: "review-2",
                summary: "Routed queue item",
                status: "changes_requested",
              }),
            ],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Context page publish");
    const filtersToggle = screen.getByRole("button", { name: "Filters" });
    expect(filtersToggle).toHaveAttribute("aria-expanded", "false");
    // All three queues are reachable from the collapsed rail.
    expect(screen.getByRole("tab", { name: /^Pending/ })).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /^Resolved/ }),
    ).toBeInTheDocument();

    // The results tabpanel is labelled by a tab that is actually mounted.
    const panel = screen.getByRole("tabpanel");
    const labelledBy = panel.getAttribute("aria-labelledby") ?? "";
    expect(labelledBy).toBe("review-lifecycle-tab-pending");
    expect(document.getElementById(labelledBy)).toBe(
      screen.getByRole("tab", { name: /^Pending/ }),
    );

    // Switching queues works with the disclosure still closed.
    await user.click(screen.getByRole("tab", { name: /^Changes requested/ }));
    expect(await screen.findByText("Routed queue item")).toBeInTheDocument();
    expect(filtersToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("tabpanel")).toHaveAttribute(
      "aria-labelledby",
      "review-lifecycle-tab-changes_requested",
    );
    expect(
      document.getElementById("review-lifecycle-tab-changes_requested"),
    ).not.toBeNull();

    // Audience and Risk stay behind the disclosure.
    expect(screen.queryByRole("radio", { name: /To me/ })).toBeNull();
    expect(screen.queryByRole("radio", { name: "high" })).toBeNull();
    await user.click(filtersToggle);
    expect(screen.getByRole("radio", { name: /To me/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "high" })).toBeInTheDocument();
  });

  it("sends status and risk filters to the server and appends load-more results", async () => {
    const user = userEvent.setup();
    const requestedUrls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      requestedUrls.push(url);
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: { items: [reviewItem()], next_cursor: "cursor-2" },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50&cursor=cursor-2`
      ) {
        return jsonResponse({
          data: {
            items: [reviewItem({ id: "review-2", summary: "Second page item" })],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&limit=50`) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&risk_level=high&limit=50`
      ) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Context page publish");
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Second page item")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Resolved/ }));
    await waitFor(() =>
      expect(requestedUrls).toContain(
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&limit=50`,
      ),
    );
    // Risk stays behind the "Filters" disclosure.
    expect(screen.queryByRole("radio", { name: "high" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Filters" }));
    await user.click(screen.getByRole("radio", { name: "high" }));
    await waitFor(() =>
      expect(requestedUrls).toContain(
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&risk_level=high&limit=50`,
      ),
    );
  });

  it("splits the Resolved tab into approved/rejected via a status chip, not the resolved alias (UI-14)", async () => {
    const user = userEvent.setup();
    const requestedUrls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      requestedUrls.push(url);
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
        return jsonResponse({ data: {} });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&limit=50`) {
        return jsonResponse({
          data: {
            items: [reviewItem({ id: "review-approved", summary: "Approved item" })],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=rejected&limit=50`) {
        return jsonResponse({
          data: {
            items: [reviewItem({ id: "review-rejected", summary: "Rejected item" })],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );
    await screen.findByRole("tab", { name: /^Pending/ });

    // Not rendered outside the Resolved tab -- pending has no "approved"/
    // "rejected" split to make.
    expect(screen.queryByRole("radio", { name: "approved" })).toBeNull();

    await user.click(screen.getByRole("tab", { name: /^Resolved/ }));
    await screen.findByText("Approved item");
    await user.click(screen.getByRole("button", { name: "Filters" }));
    expect(screen.getByRole("radio", { name: "approved" })).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "rejected" }));

    await screen.findByText("Rejected item");
    expect(screen.queryByText("Approved item")).not.toBeInTheDocument();
    // The concrete status, not the "resolved" alias, is what actually
    // reaches the server for this request.
    expect(requestedUrls).toContain(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=rejected&limit=50`,
    );
  });

  it("scopes to 'To me' with a forced changes_requested status filter", async () => {
    const user = userEvent.setup();
    const requestedUrls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      requestedUrls.push(url);
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse());
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ changes_requested: 1, to_me: 1 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [reviewItem()], next_cursor: null } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=changes_requested&limit=50`) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=changes_requested&scope=to_me&limit=50`
      ) {
        return jsonResponse({
          data: {
            items: [
              reviewItem({
                id: "review-2",
                summary: "Routed to me",
                audienceKind: "requester",
              }),
            ],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Context page publish");
    await user.click(screen.getByRole("tab", { name: /Changes requested/ }));
    expect(screen.queryByLabelText("Status")).toBeNull();
    // Audience stays behind the "Filters" disclosure.
    expect(screen.queryByRole("radio", { name: /To me/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Filters" }));
    await user.click(screen.getByRole("radio", { name: /To me/ }));

    await waitFor(() =>
      expect(requestedUrls).toContain(
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=changes_requested&scope=to_me&limit=50`,
      ),
    );
    // Per-row audience chip surfaces the routed audience.
    expect(screen.getAllByText("To requester").length).toBeGreaterThan(0);
  });

  it("routes request-changes to the chosen audience via the dialog", async () => {
    const user = userEvent.setup();
    let decisionBody: Record<string, unknown> | null = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse());
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: { items: decisionBody ? [] : [reviewItem()], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-1/decision`) {
        decisionBody = JSON.parse(String(init?.body));
        return jsonResponse({ data: { ...reviewItem(), status: "changes_requested" } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await user.click(await screen.findByText("Publish the selected Context draft"));
    await screen.findByRole("heading", { name: "Feedback Target" });
    await user.click(screen.getByRole("button", { name: "Request changes" }));

    const dialog = screen.getByRole("dialog");
    expect(dialog.querySelector(".dialog-form")).not.toBeNull();
    expect(
      within(dialog).getByRole("group", {
        name: "Who should act on these changes",
      }),
    ).toBeInTheDocument();
    // chat_owner_available is true on the fixture, so the chat owner radio is enabled.
    await user.click(within(dialog).getByRole("radio", { name: "All editors" }));
    await user.type(within(dialog).getByLabelText("Decision reason"), "Please revise");
    await user.click(within(dialog).getByRole("button", { name: "Request changes" }));

    await waitFor(() =>
      expect(decisionBody).toEqual({
        decision_type: "request_changes",
        decision_reason: "Please revise",
        audience_kind: "workspace_editors",
      }),
    );
  });

  it("disables the chat-owner option when no chat owner is resolvable", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse());
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: {
            items: [reviewItem({ chatOwnerAvailable: false })],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await user.click(await screen.findByText("Publish the selected Context draft"));
    await screen.findByRole("heading", { name: "Feedback Target" });
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("radio", { name: /Chat owner/ })).toBeDisabled();
    expect(
      within(dialog).getByText("This change has no resolvable chat owner."),
    ).toBeInTheDocument();
  });

  it("keeps the dialog open and warns when the server reports chat_owner_unresolvable", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse());
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [reviewItem()], next_cursor: null } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-1/decision`) {
        // Stale flag: backend rejects the chat-owner route at submit time with
        // the canonical error envelope.
        return new Response(
          JSON.stringify({
            error_code: "chat_owner_unresolvable",
            message: "Chat owner could not be resolved.",
            request_id: "req-1",
            warnings: [],
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await user.click(await screen.findByText("Publish the selected Context draft"));
    await screen.findByRole("heading", { name: "Feedback Target" });
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Decision reason"), "Revise");
    await user.click(within(dialog).getByRole("button", { name: "Request changes" }));

    expect(
      await within(dialog).findByText(/no resolvable chat owner/),
    ).toBeInTheDocument();
    // Dialog stays open so the reviewer can pick another audience.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("keeps a generic routing failure localized inside the open dialog", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse());
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [reviewItem()], next_cursor: null } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-1/decision`) {
        throw new Error("raw request req-private-review-routing");
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await user.click(await screen.findByText("Publish the selected Context draft"));
    await screen.findByRole("heading", { name: "Feedback Target" });
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Decision reason"), "Revise");
    await user.click(within(dialog).getByRole("button", { name: "Request changes" }));

    expect(await within(dialog).findByText("Failed to decide Review"))
      .toBeInTheDocument();
    expect(within(dialog).queryByText(/req-private-review-routing/)).toBeNull();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("shows per-status queue cards with clickable risk chips and no Approve-current-filter", async () => {
    const user = userEvent.setup();
    let lastReviewsUrl = "";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({
          data: reviewCounts({
            pending: 3,
            status_by_risk: {
              pending: { high: 2, medium: 1, low: 0 },
              changes_requested: { high: 0, medium: 0, low: 0 },
              resolved: { high: 0, medium: 0, low: 0 },
            },
          }),
        });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url.includes("/reviews?")) {
        lastReviewsUrl = url;
        return jsonResponse({ data: { items: [reviewItem()], next_cursor: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    // UI-9: the overview title uses the shared feature-header structure so it
    // reads as a page title instead of body text.
    const overviewTitle = await screen.findByRole("heading", {
      level: 1,
      name: "Review operations",
    });
    expect(overviewTitle.closest(".feature-header")).not.toBeNull();
    // RO-2: the standalone Risk distribution + Approve-current-filter are gone.
    expect(screen.queryByText("Risk distribution")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Approve current filter" }),
    ).not.toBeInTheDocument();

    // RO-1: the Pending card carries High/Medium/Low chips with per-risk counts.
    const highChip = screen.getByRole("button", { name: /Pending.*High.*2/ });
    expect(highChip).toBeInTheDocument();

    // Clicking the High chip applies status=pending + risk=high to the queue.
    await user.click(highChip);
    await waitFor(() => expect(lastReviewsUrl).toContain("risk_level=high"));
    expect(lastReviewsUrl).toContain("status=pending");
  });

  it("renders recent decisions as keyboard-reachable review links", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({}) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse({
          data: [
            {
              decision_id: "dec-1",
              review_item_id: "rev-1",
              review_target_title: "Pricing page",
              target_title: "Pricing page",
              target_slug: "pricing-page",
              target_version_no: 3,
              decision_summary: "Context draft approved",
              decision_type: "approve",
              actor_display_name: "Dana Editor",
              decision_reason: "Looks correct",
              resulting_review_status: "approved",
              risk_level: "low",
              created_at: "2026-05-30T10:00:00Z",
            },
          ],
        });
      }
      if (url.includes("/reviews?")) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    // RD-1: the real decision row surfaces target, actor, and a localized type.
    expect(await screen.findByText("Pricing page")).toBeInTheDocument();
    expect(
      screen.getByText(/Dana Editor · Approved · Looks correct/),
    ).toBeInTheDocument();
    expect(screen.queryByText("No decisions yet")).not.toBeInTheDocument();
    const recentDecision = screen.getByRole("link", { name: /Pricing page/ });
    expect(recentDecision).toHaveAttribute("href", "/review?review=rev-1");
    expect(recentDecision.tabIndex).toBe(0);
    recentDecision.focus();
    expect(recentDecision).toHaveFocus();

    // RD-2: the activity link deep-links to the review sub-type of the
    // workspace activity feed.
    const moreActivities = screen.getByRole("link", {
      name: "View all activity",
    });
    expect(moreActivities).toHaveAttribute(
      "href",
      "/activity?kind=workspace&subtype=review",
    );
  });

  it("localizes a coded recent-decision fallback when target metadata is absent", async () => {
    await i18n.changeLanguage("ko");
    try {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/workspaces`) {
          return jsonResponse(workspaceResponse("editor"));
        }
        if (isReviewCountsUrl(url)) {
          return jsonResponse({ data: reviewCounts({}) });
        }
        if (isRecentDecisionsUrl(url)) {
          return jsonResponse({
            data: [
              {
                decision_id: "dec-coded",
                review_item_id: "rev-coded",
                review_target_title: null,
                target_title: null,
                target_slug: null,
                target_version_no: null,
                decision_summary: "context draft requires review",
                decision_summary_code: "context_draft_requires_review",
                decision_type: "approve",
                actor_display_name: "Dana Editor",
                decision_reason: null,
                resulting_review_status: "approved",
                risk_level: "low",
                created_at: "2026-05-30T10:00:00Z",
              },
            ],
          });
        }
        if (url.includes("/reviews?")) {
          return jsonResponse({ data: { items: [], next_cursor: null } });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

      render(
        <MemoryRouter>
          <ReviewScreen />
        </MemoryRouter>,
      );

      const localized = "Context 초안을 리뷰해야 합니다";
      expect(await screen.findByRole("link", { name: new RegExp(localized) }))
        .toHaveAttribute("href", "/review?review=rev-coded");
      expect(screen.queryByText("context draft requires review")).toBeNull();
    } finally {
      await act(async () => {
        await i18n.changeLanguage("en");
      });
    }
  });

  it("selects the second recent decision and discards the first late response", async () => {
    const user = userEvent.setup();
    let firstRequested = false;
    let releaseFirst: () => void = () => {};
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return Promise.resolve(jsonResponse(workspaceResponse("editor")));
      }
      if (isReviewCountsUrl(url)) {
        return Promise.resolve(jsonResponse({ data: reviewCounts({ pending: 2 }) }));
      }
      if (isRecentDecisionsUrl(url)) {
        return Promise.resolve(
          jsonResponse({
            data: [
              recentDecision("decision-first", "review-first", "First target"),
              recentDecision("decision-second", "review-second", "Second target"),
            ],
          }),
        );
      }
      if (
        url
        === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`
      ) {
        return Promise.resolve(
          jsonResponse({ data: { items: [], next_cursor: null } }),
        );
      }
      if (
        url
        === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-first`
      ) {
        firstRequested = true;
        return new Promise<Response>((resolve) => {
          releaseFirst = () =>
            resolve(
              jsonResponse({
                data: reviewItem({
                  id: "review-first",
                  targetTitle: "First target",
                }),
              }),
            );
        });
      }
      if (
        url
        === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-second`
      ) {
        return Promise.resolve(
          jsonResponse({
            data: reviewItem({
              id: "review-second",
              targetTitle: "Second target",
            }),
          }),
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
        <LocationDisplay />
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("link", { name: /First target/ }));
    await waitFor(() => expect(firstRequested).toBe(true));
    await user.click(screen.getByRole("link", { name: /Second target/ }));

    expect(
      await screen.findByRole("heading", { name: "Second target" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      "?review=review-second",
    );

    releaseFirst();
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));
    expect(
      screen.getByRole("heading", { name: "Second target" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "First target" }),
    ).not.toBeInTheDocument();
  });

  it("renders the recent-decision time as a relative label but keeps the long tooltip", async () => {
    const decidedAt = "2026-05-30T10:00:00Z";
    // Pin "now" three hours after the decision so it lands in the relative
    // (<7d) window the shared formatter uses. Mock Date.now() (not fake timers)
    // so testing-library's async findBy polling still advances.
    vi.spyOn(Date, "now").mockReturnValue(
      new Date("2026-05-30T13:00:00Z").getTime(),
    );

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({}) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse({
          data: [
            {
              decision_id: "dec-1",
              review_item_id: "rev-1",
              review_target_title: "Pricing page",
              target_title: "Pricing page",
              target_slug: "pricing-page",
              target_version_no: 3,
              decision_summary: "Context draft approved",
              decision_type: "approve",
              actor_display_name: "Dana Editor",
              resulting_review_status: "approved",
              risk_level: "low",
              created_at: decidedAt,
            },
          ],
        });
      }
      if (url.includes("/reviews?")) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    const time = (await screen.findByText("Pricing page"))
      .closest(".history-row")
      ?.querySelector("time.meta-line");
    expect(time).not.toBeNull();
    // Visible text is the shared relative label, not the absolute compact one.
    expect(time).toHaveTextContent(formatRelativeTimestamp(decidedAt, "en"));
    expect(time?.textContent).not.toBe(formatVersionTimestamp(decidedAt));
    // The hover tooltip still carries the full long-format instant.
    expect(time).toHaveAttribute(
      "title",
      formatVersionTimestamp(decidedAt, "long"),
    );
    expect(time).toHaveAttribute("dateTime", decidedAt);
  });

  it("bulk-approves selected reviews after confirmation", async () => {
    const user = userEvent.setup();
    let bulkBody: Record<string, unknown> | null = null;
    let approved = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({
          data: reviewCounts({ pending: approved ? 0 : 1, resolved: approved ? 1 : 0 }),
        });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: {
            items: approved
              ? []
              : [
                  reviewItem({
                    reviewType: "context_update",
                    supersededReviewCount: 2,
                  }),
                ],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/decide-bulk`) {
        bulkBody = JSON.parse(String(init?.body));
        approved = true;
        return jsonResponse({ data: { approved: ["review-1"], skipped: [] } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    // The bulk bar is present before anything is selected because it hosts the
    // select-all control; the approve action only appears once a selection
    // exists.
    const selectAll = await screen.findByRole("checkbox", {
      name: "Select all 1 shown",
    });
    expect(selectAll).not.toBeChecked();
    expect(screen.queryByRole("button", { name: "Approve selected" })).toBeNull();

    await user.click(selectAll);
    expect(
      screen.getByRole("checkbox", { name: "Select Feedback Target" }),
    ).toBeChecked();

    // Clicking it again clears the whole selection.
    await user.click(screen.getByRole("checkbox", { name: /Select all|selected/ }));
    expect(
      screen.getByRole("checkbox", { name: "Select Feedback Target" }),
    ).not.toBeChecked();

    await user.click(await screen.findByRole("checkbox", { name: "Select Feedback Target" }));
    await user.click(screen.getByRole("button", { name: "Approve selected" }));
    const dialog = screen.getByRole("dialog", { name: "Review bulk approval" });
    expect(
      within(dialog).getByText(
        "Approving the selected page versions will also settle 2 earlier pending reviews.",
      ),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /Approve/ }));

    await waitFor(() =>
      expect(bulkBody).toEqual({
        review_item_ids: ["review-1"],
        filters: null,
        decision_type: "approve",
      }),
    );
  });

  it("restores a review detail URL even when the item is beyond the first page", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ pending: 2 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: { items: [reviewItem({ id: "review-1" })], next_cursor: "next" },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-99`) {
        return jsonResponse({
          data: reviewItem({
            id: "review-99",
            targetTitle: "Page Beyond First Batch",
          }),
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/review?review=review-99"]}>
        <ReviewScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Page Beyond First Batch" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Review detail / Context page publish"),
    ).toBeInTheDocument();
  });

  it("restores a review detail URL for a first-page item", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ pending: 1 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: { items: [reviewItem({ id: "review-1" })], next_cursor: null },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/review?review=review-1"]}>
        <ReviewScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Feedback Target" }),
    ).toBeInTheDocument();
  });

  it("switches the lifecycle tab when restoring a resolved review deep link", async () => {
    const resolvedItem = () =>
      reviewItem({
        id: "review-9",
        status: "approved",
        summary: "Resolved deep link item",
        targetTitle: "Resolved Deep Link Target",
      });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ resolved: 1 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: { items: [reviewItem({ id: "review-1" })], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&limit=50`) {
        return jsonResponse({
          data: { items: [resolvedItem()], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-9`) {
        return jsonResponse({ data: resolvedItem() });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/review?review=review-9"]}>
        <ReviewScreen />
      </MemoryRouter>,
    );

    // The deep-linked item is resolved, so the screen switches off the default
    // Pending tab to the matching queue instead of injecting a resolved row
    // into the Pending rail.
    expect(
      await screen.findByRole("heading", { name: "Resolved Deep Link Target" }),
    ).toBeInTheDocument();
    // The always-visible rail tabs are the cue for the auto-switched queue.
    expect(screen.getByRole("tab", { name: /Resolved/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("button", { name: "Filters" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(
      screen.queryByText("Publish the selected Context draft"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Request changes" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Decision reason")).not.toBeInTheDocument();
  });

  it("keeps reconciliation visible but hides generic decision actions", async () => {
    mockReviewQueue([
      reviewItem({
        id: "review-reconciliation",
        reviewType: "context_reconciliation",
        canDecide: false,
        summary: "Resolve the diverged Context branch",
      }),
    ]);

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByText("Resolve the diverged Context branch"),
    );
    expect(screen.getByText("Context reconciliation")).toBeInTheDocument();
    expect(
      screen.getByText(/stays pending until it is reconciled or explicitly discarded/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Request changes" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.queryByLabelText("Decision reason")).toBeNull();
    expect(screen.queryByRole("link", { name: "Open reconciliation" })).toBeNull();
    expect(
      screen.queryByRole("link", { name: /conversation/i }),
    ).not.toBeInTheDocument();
  });

  it("links to the workbench only from explicit reconciliation identifiers", async () => {
    mockReviewQueue([
      reviewItem({
        id: "review-reconciliation",
        reviewType: "context_reconciliation",
        canDecide: false,
        summary: "Resolve the diverged Context branch",
        reconciliationAvailable: true,
        reconciliationPageId: "context-1",
        reconciliationVersionId: "version-alt",
        reconciliationBaseVersionNo: 12,
        reconciliationHeadAdvanceCount: 3,
        reconciliationConflictCount: 2,
        reconciliationBranchActorKind: "agent",
        reconciliationBranchActorDisplayName: "Research assistant",
        reconciliationChatSessionId: "chat-42",
      }),
    ]);

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Base v12")).toBeInTheDocument();
    expect(screen.getByText("HEAD advanced 3 times")).toBeInTheDocument();
    expect(screen.getByText("2 conflicts")).toBeInTheDocument();
    expect(screen.getByText("Author: agent · Research assistant")).toHaveAttribute(
      "data-actor-kind",
      "agent",
    );
    expect(
      screen.getByRole("link", {
        name: "Open conversation for Feedback Target",
      }),
    ).toHaveAttribute("href", "/chats/chat-42");

    await userEvent.click(
      screen.getByText("Resolve the diverged Context branch"),
    );

    expect(
      screen.getByRole("link", { name: "Open reconciliation" }),
    ).toHaveAttribute(
      "href",
      "/context?target=context-1&tab=history&reconcile=version-alt",
    );
    expect(screen.getByText("Reconciliation provenance")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open source conversation" }),
    ).toHaveAttribute("href", "/chats/chat-42");
  });

  it("bulk-discards only explicitly eligible reconciliation reviews with a reason", async () => {
    const items = [
      reviewItem({
        id: "reconcile-1",
        reviewType: "context_reconciliation",
        canDecide: false,
        canDiscardReconciliation: true,
        summary: "First reconciliation",
      }),
      reviewItem({
        id: "reconcile-2",
        reviewType: "context_reconciliation",
        canDecide: false,
        canDiscardReconciliation: true,
        summary: "Second reconciliation",
      }),
    ];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/workspaces`) {
          return jsonResponse(workspaceResponse("editor"));
        }
        if (isReviewCountsUrl(url)) {
          return jsonResponse({ data: reviewCounts({ pending: 2 }) });
        }
        if (isRecentDecisionsUrl(url)) {
          return jsonResponse(emptyReviewPage());
        }
        if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
          return jsonResponse({ data: {} });
        }
        if (
          url
          === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`
        ) {
          return jsonResponse({ data: { items, next_cursor: null } });
        }
        if (
          url
          === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/reconciliations/discard-bulk`
        ) {
          return jsonResponse({
            data: {
              discarded_review_item_ids: ["reconcile-1"],
              skipped: [
                {
                  review_item_id: "reconcile-2",
                  error_code: "review_state_changed",
                },
              ],
            },
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByLabelText("Select all 2 shown"),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Discard selected branches" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Discard selected reconciliation branches?",
    });
    const confirm = within(dialog).getByRole("button", {
      name: "Discard selected branches",
    });
    expect(confirm).toBeDisabled();
    await userEvent.type(
      within(dialog).getByLabelText("Discard reason"),
      "Branches are obsolete",
    );
    await userEvent.click(confirm);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/reconciliations/discard-bulk`,
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            review_item_ids: ["reconcile-1", "reconcile-2"],
            reason: "Branches are obsolete",
          }),
        }),
      ),
    );
    expect(
      await screen.findByText(/Discarded 1 branches\. Skipped 1\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/review_state_changed/)).toBeInTheDocument();
  });

  it("clears the review param when a deep-linked review fails to load", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-404`) {
        return new Response(
          JSON.stringify({
            error_code: "not_found",
            message: "Review not found",
            request_id: "req-404",
            warnings: [],
          }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/review?review=review-404"]}>
        <ReviewScreen />
        <LocationDisplay />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "The requested item could not be found.",
      ),
    ).toBeInTheDocument();
    // The stale ?review param is dropped so a refresh does not retry the dead
    // deep link forever.
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).not.toHaveTextContent(
        "review=",
      ),
    );
    // Flush the router repair and all async loader continuations; the notice
    // must remain after ?review is removed, without another collection load.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(screen.getByText("The requested item could not be found.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) =>
      input.toString() === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`,
    )).toHaveLength(1);
  });

  it("shows the resolution timestamp on Resolved rows to match the date grouping", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ resolved: 1 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&limit=50`) {
        return jsonResponse({
          data: {
            items: [
              reviewItem({
                id: "review-7",
                status: "approved",
                summary: "Approved long after creation",
                createdAt: "2026-05-02T03:04:00Z",
                updatedAt: "2026-06-10T11:22:00Z",
              }),
            ],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Review operations" });
    await user.click(screen.getByRole("tab", { name: /Resolved/ }));
    await screen.findByText("Approved long after creation");

    // Both the date divider and the row timestamp follow the resolution date,
    // so the row no longer looks misfiled under its divider.
    expect(
      screen.getByText(formatReviewDateForTest("2026-06-10T11:22:00Z")),
    ).toBeInTheDocument();
    expect(
      screen.getByText(formatVersionTimestamp("2026-06-10T11:22:00Z")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(formatVersionTimestamp("2026-05-02T03:04:00Z")),
    ).not.toBeInTheDocument();
  });

  it("returns from review detail to the overview via the Review breadcrumb", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ pending: 1 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({
          data: { items: [reviewItem({ id: "review-1" })], next_cursor: null },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await user.click(await screen.findByText("Publish the selected Context draft"));
    expect(await screen.findByRole("heading", { name: "Feedback Target" }))
      .toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Review" }));
    expect(await screen.findByRole("heading", { name: "Review operations" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Feedback Target" }))
      .not.toBeInTheDocument();
  });

  it("lets an editor claim and edit a change directly", async () => {
    const user = userEvent.setup();
    let claimed = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts({ changes_requested: 1, to_me: 1 }) });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [reviewItem()], next_cursor: null } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=changes_requested&limit=50`) {
        return jsonResponse({
          data: {
            items: [
              reviewItem({
                audienceKind: "workspace_editors",
                canClaim: true,
                status: "changes_requested",
              }),
            ],
            next_cursor: null,
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-1/claim` &&
        init?.method === "POST"
      ) {
        claimed = true;
        return jsonResponse({
          data: {
            ...reviewItem({
              audienceKind: "workspace_editors",
              canClaim: true,
              status: "changes_requested",
            }),
            claimed_by: "me",
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/review"]}>
        <Routes>
          <Route path="/review" element={<ReviewScreen />} />
          <Route path="/context/:slug/edit" element={<PageEditorDestination />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByText("Publish the selected Context draft"));
    await screen.findByRole("heading", { name: "Feedback Target" });
    expect(screen.queryByRole("button", { name: "Edit directly" })).toBeNull();
    await user.click(screen.getByRole("tab", { name: /Changes requested/ }));
    await user.click(await screen.findByText("Publish the selected Context draft"));
    await user.click(screen.getByRole("button", { name: "Edit directly" }));

    expect(await screen.findByText("Page editor")).toBeInTheDocument();
    expect(screen.getByText(/\?from=review&reviewItemId=review-1/))
      .toBeInTheDocument();
    expect(claimed).toBe(true);
  });

  it("drops a stale load-more page after the lifecycle filter changes", async () => {
    const user = userEvent.setup();
    let releaseStalePage: () => void = () => {};
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return Promise.resolve(jsonResponse(workspaceResponse("editor")));
      }
      if (isReviewCountsUrl(url)) {
        return Promise.resolve(jsonResponse({ data: reviewCounts() }));
      }
      if (isRecentDecisionsUrl(url)) {
        return Promise.resolve(jsonResponse(emptyReviewPage()));
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return Promise.resolve(
          jsonResponse({
            data: { items: [reviewItem()], next_cursor: "cursor-2" },
          }),
        );
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50&cursor=cursor-2`
      ) {
        // Held open so the page resolves only after the tab has switched.
        return new Promise<Response>((resolve) => {
          releaseStalePage = () =>
            resolve(
              jsonResponse({
                data: {
                  items: [
                    reviewItem({
                      id: "review-stale",
                      summary: "Stale pending page item",
                    }),
                  ],
                  next_cursor: "cursor-3",
                },
              }),
            );
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&limit=50`) {
        return Promise.resolve(
          jsonResponse({
            data: {
              items: [
                reviewItem({
                  id: "review-9",
                  summary: "Resolved queue item",
                  status: "approved",
                }),
              ],
              next_cursor: null,
            },
          }),
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Publish the selected Context draft");
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await user.click(screen.getByRole("tab", { name: /Resolved/ }));
    await screen.findByText("Resolved queue item");

    releaseStalePage();
    // setTimeout(0) runs after the released fetch's microtask chain settles.
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));

    expect(screen.queryByText("Stale pending page item")).not.toBeInTheDocument();
    // The stale page's cursor must not resurrect the load-more row either.
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("retires a queued infinite-scroll callback when filters reuse a cursor", async () => {
    const user = userEvent.setup();
    const observers: Array<{
      callback: IntersectionObserverCallback;
      instance: IntersectionObserver;
      disconnect: ReturnType<typeof vi.fn>;
    }> = [];
    class MockIntersectionObserver implements IntersectionObserver {
      readonly root = null;
      readonly rootMargin = "120px";
      readonly thresholds = [0];
      readonly disconnect = vi.fn();
      readonly observe = vi.fn();
      readonly takeRecords = vi.fn(() => []);
      readonly unobserve = vi.fn();

      constructor(callback: IntersectionObserverCallback) {
        observers.push({ callback, instance: this, disconnect: this.disconnect });
      }
    }
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);

    let pendingLoadMoreCalls = 0;
    let resolvedLoadMoreCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("editor"));
      }
      if (isReviewCountsUrl(url)) {
        return jsonResponse({ data: reviewCounts() });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`
      ) {
        return jsonResponse({
          data: { items: [reviewItem()], next_cursor: "shared-cursor" },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&limit=50`
      ) {
        return jsonResponse({
          data: {
            items: [
              reviewItem({
                id: "review-resolved",
                summary: "Resolved queue item",
                status: "approved",
              }),
            ],
            next_cursor: "shared-cursor",
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50&cursor=shared-cursor`
      ) {
        pendingLoadMoreCalls += 1;
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=resolved&limit=50&cursor=shared-cursor`
      ) {
        resolvedLoadMoreCalls += 1;
        return jsonResponse({
          data: {
            items: [
              reviewItem({
                id: "review-resolved-more",
                summary: "Resolved infinite-scroll item",
                status: "approved",
              }),
            ],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Publish the selected Context draft");
    await waitFor(() => expect(observers).toHaveLength(1));
    const staleObserver = observers[0];
    await user.click(screen.getByRole("tab", { name: /Resolved/ }));
    await screen.findByText("Resolved queue item");
    await waitFor(() => expect(observers.length).toBeGreaterThan(1));
    expect(staleObserver.disconnect).toHaveBeenCalled();

    act(() => {
      staleObserver.callback(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        staleObserver.instance,
      );
    });
    await act(() => Promise.resolve());
    expect(pendingLoadMoreCalls).toBe(0);

    const currentObserver = observers.at(-1)!;
    act(() => {
      currentObserver.callback(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        currentObserver.instance,
      );
    });
    expect(
      await screen.findByText("Resolved infinite-scroll item"),
    ).toBeInTheDocument();
    expect(resolvedLoadMoreCalls).toBe(1);
    expect(pendingLoadMoreCalls).toBe(0);
  });

  it("hides workspace-zeroed badges for viewers and shows the personal All count", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse("viewer"));
      }
      if (isReviewCountsUrl(url)) {
        // The server zeroes workspace-wide counts for sub-editor roles but
        // still computes the personal to_me/from_me counts.
        return jsonResponse({
          data: reviewCounts({ pending: 0, changes_requested: 0, to_me: 2 }),
        });
      }
      if (isRecentDecisionsUrl(url)) {
        return jsonResponse(emptyReviewPage());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=changes_requested&limit=50`
      ) {
        return jsonResponse({
          data: {
            items: [
              reviewItem({
                id: "review-2",
                summary: "Routed to me",
                audienceKind: "requester",
                status: "changes_requested",
              }),
            ],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Review operations" });
    // Workspace-wide counts are hidden, so the tab names carry no "0 items".
    expect(screen.getByRole("tab", { name: "Pending" })).toHaveTextContent(
      /^Pending$/,
    );
    const changesTab = screen.getByRole("tab", { name: "Changes requested" });
    expect(changesTab).toHaveTextContent(/^Changes requested$/);

    await user.click(changesTab);
    await screen.findByText("Routed to me");
    await user.click(screen.getByRole("button", { name: "Filters" }));
    // "All" is clamped to to_me on the server, so it shows the personal count.
    const allRadio = screen.getByRole("radio", { name: /All/ });
    expect(allRadio).toHaveTextContent("2");
    expect(allRadio).not.toHaveTextContent("0");
    expect(screen.getByRole("radio", { name: /To me/ })).toHaveTextContent("2");
  });
});

function PageEditorDestination() {
  const location = useLocation();
  return (
    <div>
      <p>Page editor</p>
      <p>{location.search}</p>
    </div>
  );
}

function LocationDisplay() {
  const location = useLocation();
  return <p data-testid="location-search">{location.search}</p>;
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function formatReviewDateForTest(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(value));
}

function isReviewCountsUrl(url: string) {
  return url.startsWith(
    `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/counts`,
  );
}

function isRecentDecisionsUrl(url: string) {
  return url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/decisions/recent?limit=5`;
}

function emptyReviewPage() {
  return { data: [] };
}

function mockReviewQueue(
  items: ReturnType<typeof reviewItem>[],
  nextCursor: string | null = null,
) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    if (url === `${BASE}/api/v1/workspaces`) {
      return jsonResponse(workspaceResponse("editor"));
    }
    if (isReviewCountsUrl(url)) {
      return jsonResponse({ data: reviewCounts({ pending: items.length }) });
    }
    if (isRecentDecisionsUrl(url)) {
      return jsonResponse(emptyReviewPage());
    }
    if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
      return jsonResponse({ data: {} });
    }
    if (
      url
      === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=50`
    ) {
      return jsonResponse({
        data: { items, next_cursor: nextCursor },
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  });
}

function reviewCounts(
  overrides: Partial<{
    pending: number;
    changes_requested: number;
    resolved: number;
    risk_counts: { high: number; medium: number; low: number };
    status_by_risk: {
      pending: { high: number; medium: number; low: number };
      changes_requested: { high: number; medium: number; low: number };
      resolved: { high: number; medium: number; low: number };
    };
    to_me: number;
    from_me: number;
    auto_approve_candidate_count: number;
    auto_approve_candidate_counts_by_risk: { high: number; medium: number; low: number };
  }> = {},
) {
  return {
    pending: 1,
    changes_requested: 0,
    resolved: 0,
    risk_counts: { high: 1, medium: 0, low: 0 },
    status_by_risk: {
      pending: { high: 1, medium: 0, low: 0 },
      changes_requested: { high: 0, medium: 0, low: 0 },
      resolved: { high: 0, medium: 0, low: 0 },
    },
    to_me: 0,
    from_me: 0,
    auto_approve_candidate_count: 0,
    auto_approve_candidate_counts_by_risk: { high: 0, medium: 0, low: 0 },
    ...overrides,
  };
}

function recentDecision(
  decisionId: string,
  reviewItemId: string,
  targetTitle: string,
) {
  return {
    decision_id: decisionId,
    review_item_id: reviewItemId,
    review_target_title: targetTitle,
    target_title: targetTitle,
    target_slug: targetTitle.toLowerCase().replaceAll(" ", "-"),
    target_version_no: 1,
    decision_summary: "Context draft approved",
    decision_type: "approve",
    actor_display_name: "Dana Editor",
    decision_reason: null,
    resulting_review_status: "approved",
    risk_level: "low",
    created_at: "2026-05-30T10:00:00Z",
  };
}

function reviewItem(
  overrides: {
    id?: string;
    summary?: string;
    summaryCode?: string | null;
    targetTitle?: string | null;
    targetId?: string;
    targetVersionNo?: number | null;
    reviewType?: string;
    supersededReviewCount?: number;
    diffSummary?: string | null;
    audienceKind?: "chat_owner" | "requester" | "workspace_editors" | null;
    claimedBy?: string | null;
    chatOwnerAvailable?: boolean;
    canClaim?: boolean;
    canDecide?: boolean;
    status?: string;
    createdAt?: string;
    updatedAt?: string;
    reconciliationAvailable?: boolean;
    reconciliationPageId?: string | null;
    reconciliationVersionId?: string | null;
    canDiscardReconciliation?: boolean;
    reconciliationBaseVersionNo?: number | null;
    reconciliationHeadAdvanceCount?: number | null;
    reconciliationConflictCount?: number | null;
    reconciliationBranchActorKind?: string | null;
    reconciliationBranchActorDisplayName?: string | null;
    reconciliationChatSessionId?: string | null;
  } = {},
) {
  return {
    id: overrides.id ?? "review-1",
    workspace_id: WORKSPACE_ID,
    change_set_id: "77a2c536-ff53-4479-a0ae-65fb20c64fdc",
    review_type: overrides.reviewType ?? "context_page_publish",
    status: overrides.status ?? "pending",
    risk_level: "high",
    risk_policy_version: "2026-05-31.balanced.v1",
    risk_resolution_summary: "Context draft create classified as high risk.",
    risk_signals: {
      reason_codes: ["default:context_draft_create:low", "force_high:citation_warning"],
      citation_warning: true,
    },
    risk_policy_snapshot: {
      preset: "balanced",
      policy_version: "2026-05-31.balanced.v1",
    },
    decision_summary: overrides.summary ?? "Publish the selected Context draft",
    decision_summary_code: overrides.summaryCode ?? null,
    current_audience_kind: overrides.audienceKind ?? null,
    claimed_by: overrides.claimedBy ?? null,
    claimed_at: null,
    can_decide: overrides.canDecide ?? true,
    can_claim: overrides.canClaim ?? false,
    reconciliation_available: overrides.reconciliationAvailable,
    reconciliation_page_id: overrides.reconciliationPageId ?? null,
    reconciliation_version_id: overrides.reconciliationVersionId ?? null,
    can_discard_reconciliation: overrides.canDiscardReconciliation,
    reconciliation_base_version_no:
      overrides.reconciliationBaseVersionNo ?? null,
    reconciliation_head_advance_count:
      overrides.reconciliationHeadAdvanceCount ?? null,
    reconciliation_conflict_count:
      overrides.reconciliationConflictCount ?? null,
    reconciliation_branch_actor_kind:
      overrides.reconciliationBranchActorKind ?? null,
    reconciliation_branch_actor_display_name:
      overrides.reconciliationBranchActorDisplayName ?? null,
    reconciliation_chat_session_id:
      overrides.reconciliationChatSessionId ?? null,
    chat_owner_available: overrides.chatOwnerAvailable ?? true,
    superseded_review_count: overrides.supersededReviewCount ?? 0,
    created_at: overrides.createdAt ?? "2026-05-09T00:00:00Z",
    updated_at: overrides.updatedAt ?? "2026-05-09T00:00:00Z",
    evidence: {
      change_set_status: "draft_created",
      trigger_type: "mcp_write_draft",
      reason: "create cited context draft",
      cite_call_skipped: false,
      operations: [
        {
          operation_id: `${overrides.id ?? "review-1"}-operation`,
          target_type: "context_page",
          target_id: overrides.targetId ?? "context-1",
          target_title:
            overrides.targetTitle === undefined
              ? "Feedback Target"
              : overrides.targetTitle,
          target_slug: "feedback-target",
          target_version_no: overrides.targetVersionNo ?? 1,
          operation: "create_draft",
          diff_summary: overrides.diffSummary ?? null,
          diff_ref: "context_page_version:version-1",
        },
      ],
    },
    decisions: [],
  };
}

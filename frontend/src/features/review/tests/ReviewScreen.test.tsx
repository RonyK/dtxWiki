import { readAppStyles } from "../../../test-support/appStyles";

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  formatRelativeTimestamp,
  formatVersionTimestamp,
} from "../../../shared/lib/datetime";
import { i18n } from "../../../shared/i18n";
import { ReviewScreen } from "../ReviewScreen";
import * as pagesApi from "../../../shared/api/pages";
import { ApiError } from "../../../shared/api/client";
import type { ReviewEvidenceOperation, ReviewItem, ReviewSelection } from "../../../shared/api/operations";
import { AuthProvider, useOptionalAuth } from "../../../shared/auth/AuthContext";
import { reviewDraftKey } from "../../../shared/lib/reviewDraftStorage";

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
    sessionStorage.clear();
    reviewFixtures.clear();
    Object.defineProperty(Element.prototype, "scrollTo", { configurable: true, value: vi.fn() });
    vi.stubGlobal("matchMedia", vi.fn().mockImplementation((query: string) => ({
      matches: false, media: query, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    })));
    vi.spyOn(pagesApi, "readPage").mockImplementation(async (workspaceId, pageId, versionId) =>
      exactPage(workspaceId, pageId, versionId ?? "version-1"));
    vi.spyOn(pagesApi, "diffPageVersions").mockImplementation(async (_workspaceId, _pageId, from, to) => ({
      from_version_id: from, to_version_id: to,
      from_version_no: from ? Number(from.split("-").at(-1)) : 0,
      to_version_no: Number(to.split("-").at(-1)),
      baseline: from ? "version" : "empty", additions: 1, deletions: 0,
      lines: ["+Exact candidate body"], metadata_changes: [],
    }));
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
    mockReviewFetch(async (input) => {
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
    let finishDecision: () => void = () => {};
    const clipboardWrite = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    mockReviewFetch(async (input, init) => {
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
        await new Promise<void>(resolve => { finishDecision = resolve; });
        decisionCount += 1;
        const receipt = reviewItem({ status: "approved", updatedAt: "2026-10-22T10:00:00Z" });
        receipt.evidence!.operations[0].content!.is_current_candidate = false;
        return jsonResponse({ data: receipt });
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
    const reviewDateLabel = formatVersionTimestamp(reviewItem().created_at);
    expect(screen.getByText(reviewDateLabel)).toBeInTheDocument();
    await userEvent.click(screen.getByText("Publish the selected Context draft"));
    expect(screen.getByText("Review detail / Context page publish")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Review information"));
    expect(screen.getByText("Change evidence")).toBeInTheDocument();
    expect(screen.getByText("Draft created")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Feedback Target" })).toBeInTheDocument();
    expect(screen.getByText(/Create draft - feedback-target - v1/)).toBeInTheDocument();
    expect(screen.getByText("77a2c536...4fdc")).toHaveAttribute(
      "title",
      "77a2c536-ff53-4479-a0ae-65fb20c64fdc",
    );
    expect(screen.getAllByTitle("high")[0]).toHaveClass("status-pill-danger");
    expect(document.querySelector(".review-diagnostics"))
      .toHaveTextContent("Recorded citation-call skip flag: No");
    expect(screen.queryByText("Passed")).not.toBeInTheDocument();
    expect(screen.getByText(/^Why this risk\?/)).toBeInTheDocument();
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
    await waitFor(() => expect(rejectButton).toBeEnabled());
    expect(rejectButton).toHaveClass("danger");
    // The decisions are the detail heading's action cluster, like every other
    // detail screen, not a strip floating below the header.
    expect(approveButton.closest(".detail-heading-actions")).not.toBeNull();
    expect(approveButton.closest(".review-detail-header")).not.toBeNull();
    expect(
      approveButton.compareDocumentPosition(screen.getByText("Review information")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      approveButton.compareDocumentPosition(screen.getByLabelText("Decision reason")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    await userEvent.click(approveButton);
    expect(screen.queryByText(i18n.t("review:workflow.decisionOutcomeUnknown"))).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry the same decision" })).not.toBeInTheDocument();
    await act(async () => { finishDecision(); });

    expect(decisionBody).toEqual({
      decision_type: "approve",
      decision_reason: null,
      decision_id: expect.any(String),
    });
    // The rail empty state now names the lane it is empty for, so "nothing to
    // review" no longer reads as "nothing anywhere".
    expect(await screen.findByText("No pending items")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(savedDecisionLabel()).closest('[role="status"]')).toHaveFocus());
    expect(pagesApi.readPage).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("link", { name: "Review" }));
    expect(screen.queryByText(i18n.t("review:workflow.outsideFilters"))).not.toBeInTheDocument();
  });

  it("writes the rail empty state per lane instead of splicing the tab label", async () => {
    const user = userEvent.setup();
    mockReviewFetch(async (input) => {
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
      <MemoryRouter initialEntries={["/review?retained=value"]}>
        <ReviewScreen />
        <LocationDisplay />
        <HistoryControls />
        <NavigateButton to="/review?retained=value&older=1" label="Older-only empty queue" />
        <NavigateButton to="/review?retained=value&older=1&risk=high" label="Filtered empty queue" />
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

    // The empty-state reset must clear an older-only filter as well as risk,
    // preserve unrelated query keys, and remain navigable with browser history.
    await user.click(screen.getByRole("button", { name: "Older-only empty queue" }));
    expect(await screen.findByText("No reviews match these filters.")).toBeInTheDocument();
    const emptyState = document.querySelector<HTMLElement>(".review-rail-empty")!;
    await user.click(within(emptyState).getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("No pending items")).toBeInTheDocument();
    expect(screen.getByTestId("location-search")).toHaveTextContent("?retained=value");
    expect(screen.getByTestId("location-search")).not.toHaveTextContent("older=");
    await user.click(screen.getByRole("button", { name: "History back" }));
    expect(await screen.findByText("No reviews match these filters.")).toBeInTheDocument();
    expect(screen.getByTestId("location-search")).toHaveTextContent("older=1");
    await user.click(screen.getByRole("button", { name: "History forward" }));
    expect(await screen.findByText("No pending items")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Filtered empty queue" }));
    expect(await screen.findByText("No reviews match these filters.")).toBeInTheDocument();
    await user.click(within(document.querySelector<HTMLElement>(".review-rail-empty")!)
      .getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("No pending items")).toBeInTheDocument();
    expect(screen.getByTestId("location-search").textContent).toBe("?retained=value");
  });

  it("distinguishes same-type queue rows by their target titles", async () => {
    mockReviewFetch(async (input) => {
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

    const row = await screen.findByRole("button", { name: /^Versioned Context page · v3 ·/ });
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
      reviewItem({ id: "plain", targetTitle: "Plain publish", targetId: "plain-page", createdAt: "2026-05-01T00:00:00Z" }),
    ]);

    render(
      <MemoryRouter>
        <ReviewScreen />
      </MemoryRouter>,
    );

    // The rail loaded every page (no next cursor), so 3 IS the page's total
    // and the header states it rather than hedging with "shown".
    expect(
      await screen.findByRole("region", { name: "Grouped page" }),
    ).toBeInTheDocument();
    const groupedPage = screen.getByRole("region", { name: "Grouped page" });
    expect(groupedPage.querySelector(".review-document-group-header .count-chip"))
      .toHaveTextContent("3");
    expect(within(groupedPage).getByText("Context", { exact: true })).toBeVisible();
    await screen.findByRole("region", { name: "Other page" });
    expect(
      [...document.querySelectorAll(".review-row .review-row-identity strong")].map(
        (badge) => badge.textContent,
      ),
    ).toEqual(["v3", "v2", "v1", "v9", "Plain publish"]);
    expect(screen.queryByRole("region", { name: "Plain publish" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Plain publish · v1 ·/ }).closest(".review-document-group")).toBeNull();
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
      await screen.findByRole("region", { name: "Grouped page" }),
    ).toBeInTheDocument();
    const group = screen.getByRole("region", { name: "Grouped page" });
    expect(group.querySelector(".review-document-group-header .count-chip"))
      .toHaveTextContent("2+");
  });

  it.each([
    {
      count: 1,
      copy:
        "This version will be published; 1 other review of this version or its earlier drafts will also be settled.",
    },
    {
      count: 2,
      copy:
        "This version will be published; 2 other reviews of this version or its earlier drafts will also be settled.",
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

      await userEvent.click(await screen.findByRole("button", { name: new RegExp(`^Settlement impact ${count} · v1 ·`) }));
      expect(await screen.findByText(copy)).toBeInTheDocument();
      await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
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

    await userEvent.click(await screen.findByRole("button", { name: /^No settlement impact · v1 ·/ }));
    expect(await screen.findByRole("heading", { level: 2, name: "No settlement impact" }))
      .toBeInTheDocument();
    expect(screen.queryByText(/other reviews of this version/i)).toBeNull();
  });

  it("keeps old reconciliation branches collapsed until explicitly requested", async () => {
    const user = userEvent.setup();
    const requestedUrls: string[] = [];
    mockReviewFetch(async (input) => {
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

      await userEvent.click(screen.getByRole("button", { name: /^Translated summary page · v1 ·/ }));
      expect(
        await screen.findAllByText("Context 수정 초안을 리뷰해야 합니다"),
      ).toHaveLength(2);
      expect(
        await within(screen.getByRole("region", { name: i18n.t("review:document.targets") })).findByText("stale edit preserved as alternative draft"),
      ).toBeInTheDocument();
    } finally {
      await act(async () => {
        await i18n.changeLanguage("en");
      });
    }
  });

  it("does not repeat a summary used as the row title fallback", async () => {
    mockReviewFetch(async (input) => {
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
    mockReviewFetch(async (input, init) => {
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
    await waitFor(() => expect(rejectButton).toBeEnabled());
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
      decision_id: expect.any(String),
      expected_impact_fingerprint: "impact-review-1",
    });
    // The confirm dialog closes once the decision lands.
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Reject this review?" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("compares against the draft parent and reconfirms refreshed rejection impact before saving", async () => {
    const user = userEvent.setup();
    const current = reviewItem({ reviewType: "context_update", targetId: "exact-page", targetTitle: "Layered draft", targetVersionNo: 3 });
    Object.assign(current.evidence!.operations[0].content!, {
      baseline_version_id: "version-1", baseline_version_no: 1,
      draft_parent_version_id: "version-2", draft_parent_version_no: 2,
      can_diff: false, unavailable_reason: "diff_too_large",
    });
    current.reject_effect = {
      ...current.reject_effect!, effect: "restore_previous_draft",
      published_version_id: "version-1", published_version_no: 1,
      resulting_draft_version_id: "version-2", resulting_draft_version_no: 2,
      cascaded_versions: [7, 4, 6, 5].map(version => ({ version_id: `version-${version}`, version_no: version })),
      visible_cascaded_review_ids: ["later-review"], cascaded_review_count: 2,
      visible_sibling_count: 1, impact_fingerprint: "impact-first",
      blocked_reason: "private_edit_in_progress",
    };
    // The private edit can end without changing this review's list revision.
    const ready: ReviewItem = { ...current, reject_effect: {
      ...current.reject_effect, blocked_reason: null, impact_fingerprint: "impact-ready",
    } };
    const updated: ReviewItem = {
      ...ready,
      evidence: { ...current.evidence!, operations: current.evidence!.operations.map(operation => ({ ...operation, diff_summary: "Updated draft evidence" })) },
      reject_effect: {
        ...ready.reject_effect!, restores_review: true, impact_fingerprint: "impact-updated",
        cascaded_versions: [...current.reject_effect.cascaded_versions!, { version_id: "version-9", version_no: 9 }],
      },
    };
    const decisionBodies: Array<Record<string, unknown>> = [];
    let detailCalls = 0;
    let finishCancelledRefresh: () => void = () => {};
    let finishReadyRefresh: () => void = () => {};
    let finishImpactRefresh: () => void = () => {};
    mockReviewQueue([current], null, (url, init) => {
      if (url.endsWith("/reviews/review-1")) {
        detailCalls += 1;
        if ([1, 3].includes(detailCalls)) return jsonResponse({ data: current });
        if (detailCalls === 2) return new Promise<Response>(resolve => {
          finishCancelledRefresh = () => resolve(jsonResponse({ data: ready }));
        });
        if (detailCalls === 4) return new Promise<Response>(resolve => {
          finishReadyRefresh = () => resolve(jsonResponse({ data: ready }));
        });
        if (detailCalls === 5) return new Response(JSON.stringify({
          error_code: "review_detail_unavailable", message: "Could not recheck the rejection impact", warnings: [],
        }), { status: 503, headers: { "Content-Type": "application/json" } });
        if (detailCalls === 6) return jsonResponse({ data: ready });
        return new Promise<Response>(resolve => {
          finishImpactRefresh = () => resolve(jsonResponse({ data: updated }));
        });
      }
      if (url.endsWith("/decision")) {
        decisionBodies.push(JSON.parse(String(init?.body)));
        return decisionBodies.length === 1 ? new Response(JSON.stringify({
          error_code: "review_decision_impact_changed", message: "Impact changed", warnings: [],
        }), { status: 409, headers: { "Content-Type": "application/json" } })
          : jsonResponse({ data: { ...updated, status: "rejected" } });
      }
      if (url.endsWith("/reviews?status=pending&limit=50") && decisionBodies.length === 2) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
    });
    render(<MemoryRouter initialEntries={["/review?review=review-1"]}><ReviewScreen /></MemoryRouter>);

    const comparison = await screen.findByRole("group", { name: "Comparison baseline" });
    const parent = within(comparison).getByRole("button", { name: "This draft's change (v2 → v3)" });
    const published = within(comparison).getByRole("button", { name: "What publishing changes (published v1 → v3)" });
    expect(parent).toHaveAttribute("aria-pressed", "true");
    expect(published).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("Previous draft v2 → Review version v3")).toBeVisible();
    await waitFor(() => expect(pagesApi.diffPageVersions).toHaveBeenLastCalledWith(
      WORKSPACE_ID, "exact-page", "version-2", "version-3", expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    await user.click(published);
    expect(published).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Current published version v1 → Review version v3")).toBeVisible();
    expect(screen.getByText(i18n.t("review:content.tooLarge"))).toBeVisible();
    expect(vi.mocked(pagesApi.diffPageVersions).mock.calls.some(([, , from]) => from === "version-1")).toBe(false);
    // The parent gets its own bounded API request; its actual size error is
    // handled independently of the published baseline's preflight flag.
    vi.mocked(pagesApi.diffPageVersions).mockRejectedValueOnce(new ApiError(413, {
      error_code: "page_diff_too_large", message: "Too large", request_id: "review-parent-diff", warnings: [],
    }));
    await user.click(parent);
    await waitFor(() => expect(pagesApi.diffPageVersions).toHaveBeenLastCalledWith(
      WORKSPACE_ID, "exact-page", "version-2", "version-3", expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    expect(await screen.findByText(i18n.t("review:content.tooLarge"))).toBeVisible();
    expect(screen.queryByText(i18n.t("review:content.versionMismatch"))).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Reject" }));
    let dialog = await screen.findByRole("dialog", { name: "Reject this review?" });
    await waitFor(() => expect(detailCalls).toBe(2));
    expect(within(dialog).getByRole("button", { name: "Reject" })).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox", { name: "Decision reason" }), "Keep this reason");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Reject" }));
    dialog = await screen.findByRole("dialog", { name: "Reject this review?" });
    await waitFor(() => expect(detailCalls).toBe(3));
    await act(async () => { finishCancelledRefresh(); });
    expect(within(dialog).getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(within(dialog).getByText(i18n.t("review:workflow.reasons.review_reject_blocked_by_private_edit"))).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(detailCalls).toBe(4));
    const reason = within(dialog).getByRole("textbox", { name: "Decision reason" });
    reason.focus();
    await act(async () => { finishReadyRefresh(); });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Reject" })).toBeEnabled());
    expect(reason).toHaveValue("Keep this reason");
    expect(reason).toHaveFocus();
    // A subsequent failed read must not authorize the previously ready impact.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Reject" }));
    dialog = await screen.findByRole("dialog", { name: "Reject this review?" });
    expect(await within(dialog).findByText("Could not recheck the rejection impact")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(decisionBodies).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Reject" })).toBeEnabled());
    expect(within(dialog).getByText("Layered draft")).toBeVisible();
    expect(within(dialog).getByText("Review version v3")).toBeVisible();
    expect(within(dialog).getByText("v3 · this review")).toBeVisible();
    expect(within(dialog).getByText("Also rejected")).toBeVisible();
    expect(within(dialog).getByText("v4–v7 (4 versions) · later drafts written on v3 (2 reviews settled)")).toBeVisible();
    expect(within(dialog).queryByText(/v4, v5/)).not.toBeInTheDocument();
    expect(within(dialog).getByText("Edit head")).toBeVisible();
    expect(within(dialog).getByText("The edit base returns to v2. Its review stays open.")).toBeVisible();
    expect(within(dialog).getByText("Published")).toBeVisible();
    expect(within(dialog).getByText("v1 stays published")).toBeVisible();
    expect(within(dialog).getByText(i18n.t("review:workflow.rejectSiblingImpact", { count: 1 }))).toBeVisible();
    expect(within(dialog).getByRole("textbox", { name: "Decision reason" })).toHaveValue("Keep this reason");
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    expect(decisionBodies[0]).toEqual({
      decision_type: "reject", decision_reason: "Keep this reason", decision_id: expect.any(String),
      expected_impact_fingerprint: "impact-ready",
    });
    await waitFor(() => expect(detailCalls).toBe(7));
    expect(dialog).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Reject" })).toBeDisabled();
    within(dialog).getByRole("textbox", { name: "Decision reason" }).focus();
    await act(async () => { finishImpactRefresh(); });
    expect(await within(dialog).findByText("The rejection's impact changed while you were confirming. Check the updated impact and reject again.")).toBeVisible();
    expect(within(dialog).getByText("v4–v7, v9 (5 versions) · later drafts written on v3 (2 reviews settled)")).toBeVisible();
    expect(within(dialog).getByText("The edit base returns to v2. Its review was already closed, so a new review opens.")).toBeVisible();
    expect(within(dialog).getByRole("textbox", { name: "Decision reason" })).toHaveValue("Keep this reason");
    expect(within(dialog).getByRole("textbox", { name: "Decision reason" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    expect(decisionBodies).toHaveLength(2);
    expect(decisionBodies[1]).toEqual({
      decision_type: "reject", decision_reason: "Keep this reason", decision_id: expect.any(String),
      expected_impact_fingerprint: "impact-updated",
    });
    expect(decisionBodies[1].decision_id).not.toBe(decisionBodies[0].decision_id);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Reject this review?" })).not.toBeInTheDocument());
    expect(screen.queryByText(i18n.t("review:workflow.decisionOutcomeUnknown"))).not.toBeInTheDocument();
    expect(await screen.findByText("No pending items")).toBeVisible();
  });

  it("cancels the reject confirm without submitting a decision", async () => {
    let decisionRequested = false;
    mockReviewFetch(async (input) => {
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
      screen.getByRole("heading", { level: 2, name: "Feedback Target" }),
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
    mockReviewFetch(async (input) => {
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
    mockReviewFetch(async (input) => {
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
    mockReviewFetch(async (input) => {
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
    mockReviewFetch(async (input) => {
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
      <MemoryRouter initialEntries={["/review?review=review-1"]}>
        <ReviewScreen /><HistoryControls />
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
    const rail = screen.getByRole("complementary", { name: "Review queue" });
    const panel = within(rail).getByRole("tabpanel");
    const labelledBy = panel.getAttribute("aria-labelledby") ?? "";
    expect(labelledBy).toBe("review-lifecycle-tab-pending");
    expect(document.getElementById(labelledBy)).toBe(
      screen.getByRole("tab", { name: /^Pending/ }),
    );

    // Switching queues works with the disclosure still closed.
    await user.click(screen.getByRole("tab", { name: /^Changes requested/ }));
    expect(await screen.findByText("Routed queue item")).toBeInTheDocument();
    expect(filtersToggle).toHaveAttribute("aria-expanded", "false");
    expect(within(rail).getByRole("tabpanel")).toHaveAttribute(
      "aria-labelledby",
      "review-lifecycle-tab-changes_requested",
    );
    expect(
      document.getElementById("review-lifecycle-tab-changes_requested"),
    ).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "History back" }));
    await screen.findByRole("heading", { name: "Feedback Target", level: 2 });
    await user.click(screen.getByRole("button", { name: "History forward" }));
    await screen.findByText("Routed queue item");

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
    mockReviewFetch(async (input) => {
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
    mockReviewFetch(async (input) => {
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
    mockReviewFetch(async (input) => {
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
    mockReviewFetch(async (input, init) => {
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
    await screen.findByRole("heading", { level: 2, name: "Feedback Target" });
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
        decision_id: expect.any(String),
      }),
    );
  });

  it("disables the chat-owner option when no chat owner is resolvable", async () => {
    const user = userEvent.setup();
    mockReviewFetch(async (input) => {
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
    await screen.findByRole("heading", { level: 2, name: "Feedback Target" });
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("radio", { name: /Chat owner/ })).toBeDisabled();
    expect(
      within(dialog).getByText("This change has no resolvable chat owner."),
    ).toBeInTheDocument();
  });

  it("keeps the dialog open and warns when the server reports chat_owner_unresolvable", async () => {
    const user = userEvent.setup();
    mockReviewFetch(async (input) => {
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
    await screen.findByRole("heading", { level: 2, name: "Feedback Target" });
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

  it.each(["Reject", "Request changes"])("keeps %s outcome uncertainty localized inside its open dialog", async (action) => {
    const user = userEvent.setup();
    mockReviewQueue([reviewItem()], null, url => url.endsWith("/decision")
      ? Promise.reject(new Error("raw request req-private-review-routing")) : undefined);
    render(<MemoryRouter initialEntries={["/review?review=review-1"]}><ReviewScreen /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole("button", { name: action })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: action }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Decision reason"), "Revise");
    await user.click(within(dialog).getByRole("button", { name: action }));
    expect(await within(dialog).findByText(i18n.t("review:workflow.decisionOutcomeUnknown"))).toBeInTheDocument();
    expect(within(dialog).queryByText(i18n.t("review:workflow.decisionFailedKeepDraft"))).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: action })).toBeDisabled();
    expect(within(dialog).getByLabelText("Decision reason")).toHaveValue("Revise");
    expect(within(dialog).queryByText(/req-private-review-routing/)).toBeNull();
  });

  it("shows per-status queue cards with clickable risk chips and no Approve-current-filter", async () => {
    const user = userEvent.setup();
    let lastReviewsUrl = "";
    mockReviewFetch(async (input) => {
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
    mockReviewFetch(async (input) => {
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
      mockReviewFetch(async (input) => {
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
    mockReviewFetch((input) => {
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
        <NavigateButton to="/review?review=review-second" label="Go to second recent review" />
      </MemoryRouter>,
    );

    const firstLink = await screen.findByRole("link", { name: /First target/ });
    await user.click(firstLink);
    await waitFor(() => expect(firstRequested).toBe(true));
    await user.click(screen.getByRole("button", { name: "Go to second recent review" }));

    expect(
      await screen.findByRole("heading", { level: 2, name: "Second target" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      "?review=review-second",
    );

    await act(async () => { releaseFirst(); });
    expect(
      screen.getByRole("heading", { level: 2, name: "Second target" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { level: 2, name: "First target" }),
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

    mockReviewFetch(async (input) => {
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
    const draftKeys = ["review-1", "sibling-1", "sibling-2"].map(id => reviewDraftKey("actor-1", WORKSPACE_ID, id));
    draftKeys.forEach(key => { sessionStorage.setItem(key, "Private draft"); sessionStorage.setItem(`${key}.pending`, "{}"); });
    mockReviewFetch(async (input, init) => {
      const url = input.toString();
      if (url.endsWith("/auth/me")) return jsonResponse({ data: { user_id: "actor-1", email: "review@example.invalid", display_name: "Reviewer", status: "active", is_server_admin: false } });
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
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/selection`) {
        expect(JSON.parse(String(init?.body))).toEqual({ review_item_ids: ["review-1"] });
        const preview = selectionPreview([reviewItem()], {
          auto_settled_review_item_ids: ["sibling-1", "sibling-2"],
        });
        preview.items[0].auto_settled_review_item_ids = ["sibling-1", "sibling-2"];
        return jsonResponse({ data: preview });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/decide-bulk`) {
        bulkBody = JSON.parse(String(init?.body));
        approved = true;
        return jsonResponse({ data: { approved: ["review-1"], skipped: [], auto_settled_review_item_ids: ["sibling-1", "sibling-2"] } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <AuthProvider><MemoryRouter>
        <ReviewScreen /><AuthIdentity />
      </MemoryRouter></AuthProvider>,
    );

    await screen.findByText("Authenticated actor: actor-1");
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
      screen.getByRole("checkbox", { name: /^Select Feedback Target · v1 ·/ }),
    ).toBeChecked();

    // Clicking it again clears the whole selection.
    await user.click(selectAll);
    expect(
      screen.getByRole("checkbox", { name: /^Select Feedback Target · v1 ·/ }),
    ).not.toBeChecked();

    await user.click(await screen.findByRole("checkbox", { name: /^Select Feedback Target · v1 ·/ }));
    await user.click(screen.getByRole("button", { name: "Approve selected" }));
    const dialog = await screen.findByRole("dialog", { name: "Review bulk approval" });
    expect(
      within(dialog).getAllByText(
        "2 other reviews of this version or its earlier drafts will also be settled.",
      ),
    ).toHaveLength(2); // Global union and the selected document/version row.
    await user.click(within(dialog).getByRole("button", { name: /Approve/ }));

    await waitFor(() =>
      expect(bulkBody).toEqual({
        selection_token: "fixed-selection-token",
        decision_type: "approve",
        batch_limit: 50,
      }),
    );
    await waitFor(() => draftKeys.forEach(key => {
      expect(sessionStorage.getItem(key)).toBeNull();
      expect(sessionStorage.getItem(`${key}.pending`)).toBeNull();
    }));
  });

  it("restores a review detail URL even when the item is beyond the first page", async () => {
    const fetchMock = mockReviewFetch(async (input) => {
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
      await screen.findByRole("heading", { level: 2, name: "Page Beyond First Batch" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Review detail / Context page publish"),
    ).toBeInTheDocument();
    expect(screen.queryByText(i18n.t("review:workflow.outsideFilters"))).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => input.toString().endsWith("/reviews/review-99"))).toHaveLength(1);
  });

  it("loads selected capabilities once per revision and ignores a late previous selection", async () => {
    const user = userEvent.setup();
    const first = reviewItem({ targetTitle: "First detail" });
    const second = reviewItem({ id: "second", targetTitle: "Second detail", targetId: "second-page" });
    let finishFirst: (response: Response) => void = () => {};
    const fetchMock = mockReviewQueue([first, second], null, url => {
      if (url.endsWith("/reviews/review-1")) return new Promise(resolve => { finishFirst = resolve; });
      if (url.endsWith("/reviews/second")) return jsonResponse({ data: { ...second,
        actions: { ...second.actions, reject: { allowed: second.updated_at !== first.updated_at } },
      } });
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/review?review=review-1"]}><ReviewScreen /></MemoryRouter>);
    await screen.findByRole("heading", { name: "First detail", level: 2 });
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /^Second detail · v1 ·/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    await act(async () => { finishFirst(jsonResponse({ data: first })); });
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Refresh reviews" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh reviews" })).toBeEnabled());
    const reads = () => fetchMock.mock.calls.filter(([input]) => input.toString().endsWith("/reviews/second"));
    expect(reads()).toHaveLength(1);
    second.updated_at = "2026-10-22T12:00:00Z";
    await user.click(screen.getByRole("button", { name: "Refresh reviews" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled());
    expect(reads()).toHaveLength(2);
  });

  it("keeps a resolved deep link separate from the pending rail", async () => {
    const resolvedItem = () =>
      reviewItem({
        id: "review-9",
        status: "approved",
        summary: "Resolved deep link item",
        targetTitle: "Resolved Deep Link Target",
      });
    mockReviewFetch(async (input) => {
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

    // Direct links keep their status separate from the current filtered queue;
    // they must neither change that queue nor inject a resolved row into it.
    expect(
      await screen.findByRole("heading", { level: 2, name: "Resolved Deep Link Target" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Pending/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(within(screen.getByRole("complementary", { name: "Review queue" }))
      .queryByText("Resolved Deep Link Target")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Filters" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByText("Publish the selected Context draft")).toBeInTheDocument();
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
    const fetchMock = mockReviewFetch(
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

  it("retains a failed deep link for an explicit detail retry", async () => {
    const user = userEvent.setup();
    let retry = false;
    mockReviewFetch(async (input) => {
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
        if (retry) return jsonResponse({ data: reviewItem({ id: "review-404" }) });
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

    expect(await screen.findByText("The requested item could not be found.")).toBeInTheDocument();
    expect(screen.getByTestId("location-search")).toHaveTextContent("review=review-404");
    expect(screen.queryByText(i18n.t("review:workflow.outsideFilters"))).not.toBeInTheDocument();
    retry = true;
    await user.click(screen.getByRole("button", { name: i18n.t("review:workflow.retryDetail") }));
    expect(await screen.findByRole("heading", { name: "Feedback Target", level: 2 })).toBeVisible();
  });

  it("shows the resolution timestamp on Resolved rows to match the date grouping", async () => {
    const user = userEvent.setup();
    mockReviewFetch(async (input) => {
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

    await screen.findByRole("heading", { level: 1, name: "Review operations" });
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
    mockReviewFetch(async (input) => {
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
    const title = await screen.findByRole("heading", { level: 2, name: "Feedback Target" });
    await waitFor(() => expect(title).toHaveFocus());
    await user.click(screen.getByRole("link", { name: "Review" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Review operations" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2, name: "Feedback Target" }))
      .not.toBeInTheDocument();
  });

  it("lets an editor claim and edit a change directly", async () => {
    const user = userEvent.setup();
    let claimed = false;
    mockReviewFetch(async (input, init) => {
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
    await screen.findByRole("heading", { level: 2, name: "Feedback Target" });
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
    mockReviewFetch((input) => {
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

    await act(async () => { releaseStalePage(); });

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
    mockReviewFetch(async (input) => {
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
    mockReviewFetch(async (input) => {
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

    await screen.findByRole("heading", { level: 1, name: "Review operations" });
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
  it("preserves actor-scoped reason drafts through review history and remounts", async () => {
    const user = userEvent.setup();
    const frames = new Map<number, FrameRequestCallback>();
    let frameId = 0;
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      const id = ++frameId; frames.set(id, callback); return id;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
    const first = reviewItem({ id: "first", targetTitle: "First draft", targetId: "page-first" });
    const second = reviewItem({ id: "second", targetTitle: "Second draft", targetId: "page-second" });
    mockReviewQueue([first, second]);
    const key = reviewDraftKey("actor-1", WORKSPACE_ID, first.id);
    sessionStorage.setItem(reviewDraftKey("other-actor", WORKSPACE_ID, first.id), "Other actor note");
    sessionStorage.setItem(reviewDraftKey("actor-1", "other-workspace", first.id), "Other workspace note");
    const mount = () => render(
      <AuthProvider>
        <MemoryRouter initialEntries={["/review?review=first&retained=value"]}>
          <ReviewScreen /><AuthIdentity /><LocationDisplay /><HistoryControls />
        </MemoryRouter>
      </AuthProvider>,
    );
    const view = mount();
    await screen.findByText("Authenticated actor: actor-1");
    await screen.findByRole("heading", { name: "First draft", level: 2 });
    await user.click(screen.getByText("Add a decision reason (optional)"));
    const reason = screen.getByRole("textbox", { name: "Decision reason" });
    expect(reason).toHaveValue("");
    await user.type(reason, "Please keep the first note");
    expect(sessionStorage.getItem(key)).toBe("Please keep the first note");
    expect(localStorage.getItem(key)).toBeNull();
    await user.click(screen.getByRole("button", { name: /^Second draft · v1 ·/ }));
    await screen.findByRole("heading", { name: "Second draft", level: 2 });
    await user.click(screen.getByText("Add a decision reason (optional)"));
    expect(screen.getByRole("textbox", { name: "Decision reason" })).toHaveValue("");
    const secondReason = screen.getByRole("textbox", { name: "Decision reason" });
    await user.click(secondReason);
    // A delayed drawer/title focus must not steal a control the user already
    // reached. Drive both frames directly, without timing sleeps or retries.
    act(() => {
      for (let frame = 0; frame < 2; frame += 1) {
        const callbacks = [...frames.values()]; frames.clear();
        callbacks.forEach(callback => callback(0));
      }
    });
    expect(secondReason).toHaveFocus();
    await user.type(secondReason, "Independent second note");
    expect(secondReason).toHaveValue("Independent second note");
    await user.click(screen.getByRole("button", { name: /^Second draft · v1 ·/ }));
    await user.click(screen.getByRole("button", { name: "History back" }));
    await screen.findByRole("heading", { name: "First draft", level: 2 });
    expect(screen.getByLabelText("Decision reason")).toHaveValue("Please keep the first note");
    expect(screen.getByTestId("location-search")).toHaveTextContent("retained=value");
    await user.click(screen.getByRole("button", { name: "History forward" }));
    await screen.findByRole("heading", { name: "Second draft", level: 2 });
    expect(screen.getByLabelText("Decision reason")).toHaveValue("Independent second note");
    view.unmount();
    mount();
    await screen.findByText("Authenticated actor: actor-1");
    await screen.findByRole("heading", { name: "First draft", level: 2 });
    expect(screen.getByLabelText("Decision reason")).toHaveValue("Please keep the first note");
    expect(sessionStorage.getItem(reviewDraftKey("other-actor", WORKSPACE_ID, first.id)))
      .toBe("Other actor note");
  });

  it("selects the entire document beyond loaded rows and reconfirms a changed snapshot", async () => {
    const user = userEvent.setup();
    const current = reviewItem({ id: "current", reviewType: "context_update", targetTitle: "Grouped document", targetId: "page-group", targetVersionNo: 12 });
    const sibling = reviewItem({ id: "same-candidate", reviewType: "context_update", targetTitle: "Grouped document", targetId: "page-group", targetVersionNo: 12 });
    const older = reviewItem({ id: "older", reviewType: "context_update", targetTitle: "Grouped document", targetId: "page-group", targetVersionNo: 11 });
    const arrival = reviewItem({ id: "new-arrival", reviewType: "context_update", targetTitle: "Grouped document", targetId: "page-group", targetVersionNo: 13 });
    const unrelated = reviewItem({ id: "unrelated", reviewType: "context_update", targetTitle: "Other document", targetId: "page-other" });
    const fixed = [current, sibling, older];
    const selectionBodies: unknown[] = [];
    const decisionBodies: unknown[] = [];
    let changed = false;
    let finishFailedSnapshot: (response: Response) => void = () => {};
    let failSnapshot = true;
    const preview = () => selectionPreview(fixed, {
      selection_token: changed ? "reconfirmed-selection-token" : "fixed-selection-token",
      eligible_count: changed ? 0 : 1,
      auto_settled_review_item_ids: changed ? [] : [sibling.id],
      items: fixed.map(item => ({
        id: item.id, eligible: !changed && item.id === current.id,
        will_auto_settle: !changed && item.id === sibling.id,
        reason_code: !changed && item.id === current.id ? null
          : !changed && item.id === sibling.id ? "superseded_by_approved_publish" : "stale_candidate",
        target_title: "Grouped document", target_version_no: item.evidence!.operations[0].content!.candidate_version_no,
        auto_settled_review_item_ids: !changed && item.id === current.id ? [sibling.id] : [],
      })),
    });
    mockReviewQueue([current, unrelated], "cursor-more", (url, init) => {
      if (url.endsWith("/reviews/selection")) {
        const request = JSON.parse(String(init?.body));
        if (failSnapshot) {
          failSnapshot = false;
          return new Promise(resolve => { finishFailedSnapshot = resolve; });
        }
        selectionBodies.push(request);
        if (request.review_item_ids?.includes(arrival.id) && request.review_item_ids.includes(unrelated.id)) {
          const multi = [...fixed, arrival, unrelated];
          return jsonResponse({ data: selectionPreview(multi, {
            selection_token: "multi-candidate-selection-token", eligible_count: 2,
            auto_settled_review_item_ids: ["outside-filter-sibling-a", "outside-filter-sibling-b", "outside-filter-sibling-c"],
            items: multi.map(item => ({
              id: item.id, eligible: [arrival.id, unrelated.id].includes(item.id), will_auto_settle: false,
              reason_code: [arrival.id, unrelated.id].includes(item.id) ? null : "stale_candidate",
              target_title: item.evidence!.operations[0].document!.title,
              target_version_no: item.evidence!.operations[0].content!.candidate_version_no,
              auto_settled_review_item_ids: item.id === arrival.id ? ["outside-filter-sibling-a"]
                : item.id === unrelated.id ? ["outside-filter-sibling-b", "outside-filter-sibling-c"] : [],
            })),
          }) });
        }
        return jsonResponse({ data: preview() });
      }
      if (url.includes("cursor=cursor-more")) {
        return jsonResponse({ data: { items: [sibling, older, arrival], next_cursor: null } });
      }
      if (url.endsWith("/reviews/decide-bulk")) {
        decisionBodies.push(JSON.parse(String(init?.body)));
        changed = true;
        return jsonResponse({ data: {
          approved: [], skipped: [{ id: current.id, reason_code: "review_selection_changed", reason: "Candidate changed." }],
          outcomes: [{ id: current.id, outcome: "excluded", reason_code: "review_selection_changed", target_title: "Grouped document", target_version_no: 12 }],
          auto_settled_review_item_ids: [], requires_reconfirmation: true,
          processed_count: 0, remaining_count: 3, total: 3, selection_complete: false, next_selection_token: null,
        } });
      }
      if (changed && url.endsWith("/reviews?status=pending&limit=50")) {
        return jsonResponse({ data: { items: [arrival, current, sibling, older, unrelated], next_cursor: null } });
      }
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/review?review=current"]}><ReviewScreen /></MemoryRouter>);
    const rail = await screen.findByRole("complementary", { name: "Review queue" });
    const group = await within(rail).findByRole("region", { name: "Grouped document" });
    await user.click(screen.getByRole("button", { name: "Collapse all" }));
    expect(group.querySelector(".review-document-group-body")).not.toBeVisible();
    expect(group.querySelector(".review-document-group-header .review-current-label")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Expand all" }));
    expect(group.querySelector(".review-document-group-body")).toBeVisible();
    const groupToggle = within(group).getByRole("button", { name: "Collapse Grouped document" });
    expect(groupToggle).toHaveAccessibleDescription("Context");
    groupToggle.focus();
    await user.keyboard(" ");
    expect(groupToggle).toHaveAttribute("aria-expanded", "false");
    expect(groupToggle).toHaveAccessibleDescription("Context Viewing");
    expect(group.querySelector(".review-document-group-body")).not.toBeVisible();
    await user.keyboard("{Enter}");
    expect(groupToggle).toHaveAttribute("aria-expanded", "true");
    expect(group.querySelector(".review-document-group-body")).toBeVisible();
    const groupCheck = within(group).getByRole("checkbox", { name: /Select all reviews matching/ });
    await user.click(within(group).getByRole("checkbox", { name: /^Select Grouped document/ }));
    expect(groupCheck).toBePartiallyChecked();
    await user.click(groupCheck);
    expect(groupCheck).toBePartiallyChecked();
    await act(async () => { finishFailedSnapshot(new Response("{}", { status: 500 })); });
    await waitFor(() => expect(groupCheck).toBeEnabled());
    expect(groupCheck).toBePartiallyChecked();
    await user.click(groupCheck);
    expect(await within(group).findByText("3 selected for this document")).toBeVisible();
    expect(groupCheck).toBeChecked();
    expect(selectionBodies).toEqual([{ filters: { status: "pending", scope: "all", target_page_id: "page-group" } }]);
    expect(screen.queryByRole("button", { name: /^Grouped document · v11 ·/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await within(group).findByRole("button", { name: /^Grouped document · v13 ·/ });
    expect(groupCheck).toBePartiallyChecked();
    const rowLabels = [...group.querySelectorAll(".review-row-select input")]
      .map(input => input.getAttribute("aria-label")!);
    expect(rowLabels).toHaveLength(4);
    expect(new Set(rowLabels).size).toBe(4);
    rowLabels.forEach((label, index) => {
      expect(label).toContain(`Review ${index + 1}`);
      expect(label).toContain("high");
      expect(label).toContain(formatVersionTimestamp(current.created_at));
    });
    expect(within(group).getByRole("checkbox", { name: /^Select Grouped document · v13 ·/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /^Select Other document/ })).not.toBeChecked();
    await user.click(within(group).getByRole("button", { name: "Approve selection" }));
    const dialog = await screen.findByRole("dialog", { name: "Review bulk approval" });
    expect(selectionBodies[1]).toEqual({ review_item_ids: fixed.map(item => item.id) });
    expect(within(dialog).getByText("Eligible for approval")).toBeVisible();
    expect(within(dialog).getByText("Settled with the version being approved")).toBeVisible();
    expect(within(dialog).getByText(/can't be published as reviewed/)).toBeVisible();
    expect(within(dialog).getByText(/Fixed selection: 3 items/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Approve 1" }));
    expect(await screen.findByText(i18n.t("review:workflow.bulkChanged"))).toBeVisible();
    expect(decisionBodies).toEqual([{ selection_token: "fixed-selection-token", decision_type: "approve", batch_limit: 50 }]);
    expect(within(group).getByRole("checkbox", { name: /^Select Grouped document · v13 ·/ })).not.toBeChecked();
    await user.click(within(group).getByRole("button", { name: "Approve selection" }));
    const reconfirmed = await screen.findByRole("dialog", { name: "Review bulk approval" });
    expect(selectionBodies[2]).toEqual({ review_item_ids: fixed.map(item => item.id) });
    expect(within(reconfirmed).getByRole("button", { name: "Approve 0" })).toBeDisabled();
    expect(decisionBodies).toHaveLength(1);
    await user.click(within(reconfirmed).getByRole("button", { name: "Cancel" }));

    // Explicitly select two current candidates. Their visible sibling effects
    // include reviews outside the selected IDs and must be attributed per row.
    await user.click(within(group).getByRole("checkbox", { name: /^Select Grouped document · v13 ·/ }));
    await user.click(screen.getByRole("checkbox", { name: /^Select Other document/ }));
    await user.click(screen.getByRole("button", { name: "Approve selected" }));
    const multiDialog = await screen.findByRole("dialog", { name: "Review bulk approval" });
    expect(selectionBodies[3]).toEqual({ review_item_ids: [...fixed, arrival, unrelated].map(item => item.id) });
    const firstImpactRow = within(multiDialog).getByText("Grouped document · v13").closest("li")!;
    const secondImpactRow = within(multiDialog).getByText("Other document · v1").closest("li")!;
    expect(within(firstImpactRow).getByText(i18n.t("review:bulk.approvalImpact", { count: 1 }))).toBeVisible();
    expect(within(secondImpactRow).getByText(i18n.t("review:bulk.approvalImpact", { count: 2 }))).toBeVisible();
    expect(within(multiDialog).getByText(i18n.t("review:bulk.approvalImpact", { count: 3 }))).toBeVisible();
    expect(within(multiDialog).getByText("Grouped document · v11").closest("li")
      ?.querySelector(".review-bulk-item-impact")).toBeNull();
    expect(within(multiDialog).queryByText(/outside-filter-sibling/)).not.toBeInTheDocument();
    expect(within(multiDialog).getByRole("button", { name: "Approve 2" })).toBeEnabled();
    expect(decisionBodies).toHaveLength(1); // Preview never submits an approval.
  });

  it("requires the exact candidate body and applies each server action capability separately", async () => {
    const user = userEvent.setup();
    let finishBody: (page: pagesApi.PagePayload) => void = () => {};
    vi.mocked(pagesApi.readPage).mockImplementationOnce(() => new Promise(resolve => { finishBody = resolve; }));
    const current = reviewItem({ reviewType: "context_update", targetId: "exact-page", targetTitle: "Exact candidate", targetVersionNo: 3 });
    current.evidence!.operations[0].diff_ref = "context_page_version:wrong-legacy-version";
    const exactOperation = current.evidence!.operations[0];
    const placement: ReviewEvidenceOperation = {
      operation_id: "tree-placement", target_type: "workspace_tree_node", target_id: "placement-node",
      target_title: "Exact candidate", target_slug: "feedback-target", target_version_no: 3,
      operation: "tree_place", diff_summary: "Place this exact reviewed candidate in the workspace tree.", diff_ref: null,
      document: { ...exactOperation.document! },
      content: { ...exactOperation.content!, mode: "metadata_only", can_read: false, can_diff: false },
    };
    current.evidence!.operations.push(placement);
    const metadata = reviewItem({ id: "metadata", targetId: "metadata-page", targetTitle: "Metadata target", reviewType: "inline_comment", canDecide: false,
      actions: { approve: { allowed: false, reason_code: "claim_conflict" }, reject: { allowed: true }, request_changes: { allowed: false, reason_code: "not_in_audience" }, edit_directly: { allowed: false } },
      content: { mode: "metadata_only", baseline_kind: "unavailable", can_read: false, can_diff: false },
      document: null, diffSummary: "feedback_only: Proposed title",
    });
    mockReviewQueue([current, metadata], null, url => url.endsWith("/decision") ? new Response(JSON.stringify({
      error_code: "legacy_media_cutover_conflict", message: "Media cutover changed. Refresh this draft before publishing.", warnings: [],
    }), { status: 409, headers: { "Content-Type": "application/json" } }) : undefined);
    render(<MemoryRouter initialEntries={["/review?review=review-1"]}><ReviewScreen /></MemoryRouter>);
    await screen.findByRole("heading", { name: "Exact candidate", level: 2 });
    expect(screen.getByText(/Current published version v2 → Review version v3/)).toBeVisible();
    expect(await screen.findByText("Approving publishes v3 in place of v2.")).toBeVisible();
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled());
    await waitFor(() => expect(screen.getByRole("button", { name: "Request changes" })).toBeEnabled());
    expect(pagesApi.readPage).toHaveBeenCalledWith(WORKSPACE_ID, "exact-page", "version-3");
    expect(pagesApi.diffPageVersions).toHaveBeenCalledWith(WORKSPACE_ID, "exact-page", "version-2", "version-3", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    const documentLink = screen.getByRole("link", { name: "Open current document: Exact candidate (new tab)" });
    expect(documentLink).toHaveAttribute("href", "/context?target=exact-page");
    expect(documentLink).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("heading", { name: "Exact candidate", level: 3 }).closest("a")).toBeNull();
    expect(document.querySelectorAll(".review-evidence-document")).toHaveLength(1);
    expect(within(document.querySelector<HTMLElement>(".review-evidence-panel")!)
      .getByText(placement.diff_summary!)).toBeVisible();
    expect(pagesApi.readPage).toHaveBeenCalledTimes(1);
    await act(async () => { finishBody(exactPage(WORKSPACE_ID, "exact-page", "wrong-version")); });
    expect(await screen.findByText(i18n.t("review:content.originalUnavailable"))).toBeVisible();
    expect(approve).toBeDisabled();
    await user.click(screen.getByRole("tab", { name: "Review version content" }));
    await user.click(screen.getByRole("button", { name: "Reload content" }));
    await waitFor(() => expect(approve).toBeEnabled());
    expect(await screen.findByText("Exact candidate body")).toBeVisible();
    expect(pagesApi.readPage).toHaveBeenCalledTimes(2); // failed exact read plus explicit retry
    await user.click(approve);
    expect(await screen.findByText("Media cutover changed. Refresh this draft before publishing.")).toBeVisible();
    await waitFor(() => expect(approve).toBeEnabled());
    current.review_type = "inline_comment";
    current.updated_at = new Date(Date.parse(current.updated_at) + 1000).toISOString();
    await user.click(screen.getByRole("button", { name: "Refresh reviews" }));
    await waitFor(() => expect(screen.queryByText("Approving publishes v3 in place of v2.")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    current.review_type = "context_update";
    current.updated_at = new Date(Date.parse(current.updated_at) + 1000).toISOString();
    current.actions!.approve = { allowed: false, reason_code: "claim_conflict" };
    await user.click(screen.getByRole("button", { name: "Refresh reviews" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled());
    expect(screen.queryByText("Approving publishes v3 in place of v2.")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled());
    current.actions!.approve = { allowed: true };
    current.updated_at = new Date(Date.parse(current.updated_at) + 1000).toISOString();
    await user.click(screen.getByRole("button", { name: "Refresh reviews" }));
    expect(await screen.findByText("Approving publishes v3 in place of v2.")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    const otherCandidate: ReviewEvidenceOperation = {
      ...placement, operation_id: "other-candidate-placement", diff_summary: "Metadata for a different candidate.",
      content: { ...placement.content!, candidate_version_id: "version-2", candidate_version_no: 2 },
    };
    current.evidence!.operations.push(otherCandidate);
    current.updated_at = new Date(Date.parse(current.updated_at) + 1000).toISOString();
    await user.click(screen.getByRole("button", { name: "Refresh reviews" }));
    await waitFor(() => expect(document.querySelectorAll(".review-evidence-document")).toHaveLength(2));
    expect(screen.queryByText("Approving publishes v3 in place of v2.")).not.toBeInTheDocument();
    const evidenceCards = document.querySelectorAll<HTMLElement>(".review-evidence-document");
    expect(within(evidenceCards[0]).getByText(placement.diff_summary!)).toBeVisible();
    expect(within(evidenceCards[0]).queryByText(otherCandidate.diff_summary!)).not.toBeInTheDocument();
    expect(within(evidenceCards[1]).getByText(otherCandidate.diff_summary!)).toBeVisible();
    expect(within(evidenceCards[1]).getByText("Review version v2")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    expect(vi.mocked(pagesApi.readPage).mock.calls.every(([, , version]) => version === "version-3")).toBe(true);

    // An unsupported target must remain separate and must still block readiness,
    // even if untrusted metadata repeats a known document/candidate identifier.
    current.evidence!.operations.push({
      ...placement, operation_id: "unsupported-operation", operation: "future_operation",
      diff_summary: "Unsupported target evidence", content: { ...placement.content!, mode: "unsupported" },
    });
    current.updated_at = new Date(Date.parse(current.updated_at) + 1000).toISOString();
    await user.click(screen.getByRole("button", { name: "Refresh reviews" }));
    expect(await screen.findByText(i18n.t("review:content.unsupported"))).toBeVisible();
    expect(document.querySelectorAll(".review-evidence-document")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    for (const [kind, label] of [["parent", "Parent version v2"], ["empty", "Empty document"], ["unavailable", "Comparison baseline unavailable"]] as const) {
      current.evidence!.operations[0].content!.baseline_kind = kind;
      current.evidence!.operations[0].content!.baseline_version_id = kind === "parent" ? "version-2" : null;
      current.evidence!.operations[0].content!.baseline_version_no = kind === "parent" ? 2 : null;
      current.updated_at = new Date(Date.parse(current.updated_at) + 1000).toISOString();
      await user.click(screen.getByRole("button", { name: "Refresh reviews" }));
      expect(await screen.findByText(`${label} → Review version v3`)).toBeVisible();
    }
    const reads = vi.mocked(pagesApi.readPage).mock.calls.length;
    await user.click(screen.getByRole("button", { name: /^Metadata target · v1 ·/ }));
    await screen.findByRole("heading", { name: "Metadata target", level: 2 });
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled());
    expect(screen.getByText(i18n.t("review:workflow.reasons.claim_conflict"))).toBeVisible();
    expect(document.querySelector(".review-publication-notice")).toBeNull();
    const metadataPanel = within(document.querySelector<HTMLElement>(".review-evidence-panel")!);
    expect(metadataPanel.getByText("feedback_only: Proposed title")).toBeVisible();
    expect(metadataPanel.getByText("create cited context draft")).toBeVisible();
    expect(metadataPanel.queryByText(i18n.t("review:content.unsupported"))).not.toBeInTheDocument();
    expect(pagesApi.readPage).toHaveBeenCalledTimes(reads);
  });

  it("keeps a saved receipt locked through queue refresh failure and continues to the next review", async () => {
    const user = userEvent.setup();
    const current = reviewItem({ reviewType: "context_update", targetTitle: "Saved first", targetId: "page-first" });
    const next = reviewItem({ id: "next-review", targetTitle: "Next document", targetId: "page-next" });
    let saved = false;
    let failRefresh = true;
    const decisions: unknown[] = [];
    mockReviewQueue([current, next], null, (url, init) => {
      if (url.endsWith("/reviews/selection")) return jsonResponse({ data: selectionPreview([current]) });
      if (url.endsWith("/reviews/review-1/decision")) {
        decisions.push(JSON.parse(String(init?.body))); saved = true;
        return jsonResponse({ data: { ...current, status: "approved" } });
      }
      if (saved && url.includes("/reviews?")) {
        if (failRefresh) return Promise.reject(new Error("private queue refresh failure"));
        return jsonResponse({ data: { items: [next], next_cursor: null } });
      }
      if (isReviewCountsUrl(url)) return jsonResponse({ data: reviewCounts({ pending: saved ? 1 : 2, resolved: saved ? 1 : 0 }) });
      return undefined;
    });
    render(<AuthProvider><MemoryRouter initialEntries={["/review?review=review-1"]}><ReviewScreen /><AuthIdentity /><LocationDisplay /></MemoryRouter></AuthProvider>);
    await screen.findByText("Authenticated actor: actor-1");
    await screen.findByRole("heading", { name: "Saved first", level: 2 });
    await user.click(within(within(screen.getByRole("complementary", { name: "Review queue" })).getByRole("region", { name: "Saved first" })).getByRole("checkbox", { name: /Select all reviews matching/ }));
    await screen.findByText("1 selected for this document");
    await user.click(screen.getByText("Add a decision reason (optional)"));
    await user.type(screen.getByRole("textbox", { name: "Decision reason" }), "Reviewed carefully");
    const key = reviewDraftKey("actor-1", WORKSPACE_ID, current.id);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(i18n.t("review:workflow.savedRefreshFailed"))).toBeVisible();
    expect(screen.getByText(savedDecisionLabel())).toBeVisible();
    expect(document.querySelector(".review-publication-notice")).toBeNull();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve selected" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next review" })).toBeDisabled();
    expect(screen.getByRole("tab", { name: "Pending" })).toBeVisible();
    expect(within(screen.getByRole("complementary", { name: "Review queue" })).queryByRole("button", { name: /^Saved first · v1 ·/ })).not.toBeInTheDocument();
    expect(sessionStorage.getItem(key)).toBeNull();
    expect(sessionStorage.getItem(`${key}.pending`)).toBeNull();
    expect(decisions).toHaveLength(1);
    failRefresh = false;
    await user.click(screen.getByRole("button", { name: "Reload queue" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Next review" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Next review" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: /Pending.*1/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next review" }));
    await screen.findByRole("heading", { name: "Next document", level: 2 });
    expect(screen.getByTestId("location-search")).toHaveTextContent("review=next-review");
    expect(screen.getByLabelText("Decision reason")).toHaveValue("");
    expect(decisions).toHaveLength(1);
  });

  it("undoes a saved approval from the receipt and reopens the review", async () => {
    const user = userEvent.setup();
    const current = reviewItem({ targetTitle: "Undo target" });
    let approved = false;
    const undoCalls: string[] = [];
    mockReviewQueue([current], null, (url) => {
      if (url.endsWith("/reviews/review-1/decision")) {
        approved = true;
        return jsonResponse({ data: { ...current, status: "approved", undo: {
          decision_id: "decision-1", decision_type: "approve", available: true,
          reason_code: null, restore_version_id: null, restore_version_no: null, page_id: "context-1",
        } } });
      }
      if (url.includes("/reviews/review-1/decisions/")) {
        undoCalls.push(url); approved = false;
        return jsonResponse({ data: current });
      }
      if (url.includes("/reviews?")) return jsonResponse({ data: { items: approved ? [] : [current], next_cursor: null } });
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/review?review=review-1"]}><ReviewScreen /></MemoryRouter>);
    await screen.findByRole("heading", { name: "Undo target", level: 2 });
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(await screen.findByRole("button", { name: "Undo approval" }));
    expect(await screen.findByText("Approval undone. The review is waiting again.")).toBeVisible();
    expect(undoCalls).toEqual([`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-1/decisions/decision-1/undo`]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
  });

  it("recovers uncertain decisions after remount with read-first retries and one decision identity", async () => {
    const user = userEvent.setup();
    const current = reviewItem();
    const next = reviewItem({ id: "next-loaded", reviewType: "context_update", targetTitle: "Loaded next", targetId: "loaded-page", targetVersionNo: 2 });
    const older = reviewItem({ id: "older-loaded", reviewType: "context_update", targetTitle: "Loaded next", targetId: "loaded-page", targetVersionNo: 1, createdAt: "2026-05-10T00:00:00Z" });
    const bodies: Array<{ decision_id: string; decision_type: string; decision_reason: string }> = [];
    const events: string[] = [];
    let committed = false;
    const receipt = (actorId = "actor-1") => reviewItem({ status: "approved", decisions: [{
      decision_id: bodies[0].decision_id, decision_type: "approve", decision_reason: "Preserve this note",
      actor_id: actorId, actor_display_name: "Reviewer", actor_type: "human",
      audience_kind: null, audience_user_id: null, resulting_review_status: "approved",
      resulting_change_set_status: "applied", created_at: "2026-10-21T11:00:00Z",
    }] });
    const fetchMock = mockReviewQueue([current], null, (url, init) => {
      if (url.endsWith("/reviews/review-1/decision")) {
        bodies.push(JSON.parse(String(init?.body))); events.push(`post-${bodies.length}`);
        if (bodies.length === 2) committed = true;
        return Promise.reject(new Error("private response lost after submit"));
      }
      if (url.endsWith("/reviews/review-1")) {
        if (bodies.length) events.push(committed ? "read-receipt" : "read-unconfirmed");
        // Status and an unrelated actor's ledger entry are not this actor's receipt.
        return jsonResponse({ data: committed ? receipt() : bodies.length === 1
          ? { ...current, decisions: receipt("different-actor").decisions } : current });
      }
      if (committed && url.includes("/reviews?")) return jsonResponse({ data: { items: [older, next], next_cursor: "not-needed" } });
      return undefined;
    });
    const mount = () => render(<AuthProvider><MemoryRouter initialEntries={["/review?review=review-1"]}><ReviewScreen /><AuthIdentity /></MemoryRouter></AuthProvider>);
    const view = mount();
    await screen.findByText("Authenticated actor: actor-1");
    await screen.findByRole("heading", { name: "Feedback Target", level: 2 });
    await user.click(screen.getByText("Add a decision reason (optional)"));
    await user.type(screen.getByRole("textbox", { name: "Decision reason" }), "Preserve this note");
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(i18n.t("review:workflow.decisionOutcomeUnknown"))).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.queryByText(savedDecisionLabel())).not.toBeInTheDocument();
    const key = reviewDraftKey("actor-1", WORKSPACE_ID, current.id);
    expect(JSON.parse(sessionStorage.getItem(`${key}.pending`)!)).toMatchObject({ decisionId: bodies[0].decision_id, reason: "Preserve this note" });
    view.unmount();
    mount();
    await screen.findByText("Authenticated actor: actor-1");
    await screen.findByRole("heading", { name: "Feedback Target", level: 2 });
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(bodies).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Retry the same decision" }));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toEqual(bodies[0]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry the same decision" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Retry the same decision" }));
    expect(await screen.findByText(savedDecisionLabel())).toBeVisible();
    expect(events[0]).toBe("post-1");
    expect(events[events.indexOf("post-2") - 1]).toBe("read-unconfirmed");
    expect(events.at(-1)).toBe("read-receipt");
    expect(bodies).toHaveLength(2);
    expect(sessionStorage.getItem(`${key}.pending`)).toBeNull();
    expect(sessionStorage.getItem(key)).toBeNull();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Next review" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Next review" }));
    expect(await screen.findByRole("heading", { name: "Loaded next", level: 2 })).toBeVisible();
    expect(await screen.findByText("Approving publishes v2 in place of v1.")).toBeVisible();
    expect(fetchMock.mock.calls.some(([input]) => input.toString().includes("cursor=not-needed"))).toBe(false);
  });

  it.each(["history", "unmount"])("stops bulk continuation after %s and retains confirmed batches", async (interruption) => {
    const user = userEvent.setup();
    const items = ["first", "second", "third"].map(id => reviewItem({ id, targetId: id, targetTitle: id }));
    let batchCalls = 0;
    let finishBatch: (response: Response) => void = () => {};
    const fetchMock = mockReviewQueue(items, null, (url) => {
      if (url.endsWith("/selection")) return jsonResponse({ data: selectionPreview(items) });
      if (url.endsWith("/decide-bulk")) {
        batchCalls += 1;
        if (batchCalls === 2) return new Promise(resolve => { finishBatch = resolve; });
        return jsonResponse({ data: { approved: ["first"], skipped: [], processed_count: 1,
          total: 3, remaining_count: 2, next_selection_token: "second-batch", selection_complete: false } });
      }
      if (url.includes("/reviews?") && url.includes("risk_level=low")) return jsonResponse({ data: { items: [], next_cursor: null } });
      return undefined;
    });
    const view = render(<MemoryRouter initialEntries={["/review?risk=low", "/review"]}>
      <ReviewScreen /><HistoryControls />
    </MemoryRouter>);
    await user.click(await screen.findByRole("checkbox", { name: "Select all 3 shown" }));
    await user.click(screen.getByRole("button", { name: "Approve selected" }));
    const dialog = await screen.findByRole("dialog", { name: "Review bulk approval" });
    await user.click(within(dialog).getByRole("button", { name: "Approve 3" }));
    await waitFor(() => expect(batchCalls).toBe(2));
    if (interruption === "history") {
      await user.click(screen.getByRole("button", { name: "History back", hidden: true }));
      await screen.findByText(i18n.t("review:workflow.bulkStopped", { processed: 1, total: 3 }));
      await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => input.toString().includes("risk_level=low"))).toBe(true));
    } else view.unmount();
    const listCalls = () => fetchMock.mock.calls.filter(([input]) => input.toString().includes("/reviews?"));
    const beforeRelease = listCalls().length;
    await act(async () => { finishBatch(jsonResponse({ data: { approved: ["second"], skipped: [],
      total: 3, remaining_count: 1, next_selection_token: "third-batch", selection_complete: false } })); });
    if (interruption === "history") {
      expect(await screen.findByText(i18n.t("review:workflow.bulkStopped", { processed: 2, total: 3 }))).toBeVisible();
      expect(screen.getByText(/Approved 2 reviews/)).toBeVisible();
    }
    expect(batchCalls).toBe(2);
    expect(listCalls()).toHaveLength(beforeRelease);
    expect(fetchMock.mock.calls.filter(([input]) => input.toString().endsWith("/decide-bulk"))
      .map(([, init]) => JSON.parse(String(init?.body)).batch_limit)).toEqual([50, 50]);
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

// Lists and write receipts intentionally omit the detail-only preflight capabilities.
const reviewFixtures = new Map<string, ReviewItem>();
function mockReviewFetch(handler: (input: RequestInfo | URL, init?: RequestInit) => Response | Promise<Response>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input.toString();
    let response: Response;
    try { response = await handler(input, init); }
    catch (error) {
      const id = url.match(/\/reviews\/([^/?]+)$/)?.[1];
      const fixture = id && reviewFixtures.get(id);
      if (!(error instanceof Error) || !error.message.startsWith("Unexpected request:") || !fixture) throw error;
      response = jsonResponse({ data: fixture });
    }
    if (!response.ok || !url.includes("/reviews")) return response;
    const body = await response.json();
    const remember = (item: ReviewItem) => {
      if (item?.id && item.evidence) reviewFixtures.set(item.id, item);
      return { ...item, actions: null, reject_effect: null };
    };
    if (Array.isArray(body.data?.items)) body.data.items = body.data.items.map(remember);
    else if (body.data?.id && body.data.evidence) {
      const stripped = remember(body.data);
      if (init?.method && init.method !== "GET") body.data = stripped;
    }
    return jsonResponse(body);
  });
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
  override?: (url: string, init?: RequestInit) => Response | Promise<Response> | undefined,
) {
  return mockReviewFetch(async (input, init) => {
    const url = input.toString();
    const custom = override?.(url, init);
    if (custom !== undefined) return custom;
    if (url === `${BASE}/api/v1/auth/me`) {
      return jsonResponse({ data: { user_id: "actor-1", email: "review@example.invalid", display_name: "Reviewer", status: "active", is_server_admin: false } });
    }
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
    riskLevel?: string;
    actions?: ReviewItem["actions"];
    document?: ReviewEvidenceOperation["document"];
    content?: ReviewEvidenceOperation["content"];
    decisions?: ReviewItem["decisions"];
  } = {},
): ReviewItem {
  return {
    id: overrides.id ?? "review-1",
    workspace_id: WORKSPACE_ID,
    change_set_id: "77a2c536-ff53-4479-a0ae-65fb20c64fdc",
    review_type: overrides.reviewType ?? "context_page_publish",
    status: overrides.status ?? "pending",
    risk_level: overrides.riskLevel ?? "high",
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
    actions: overrides.actions ?? {
      approve: { allowed: overrides.canDecide ?? true, reason_code: null },
      reject: { allowed: overrides.canDecide ?? true, reason_code: null },
      request_changes: { allowed: overrides.canDecide ?? true, reason_code: null },
      edit_directly: { allowed: overrides.canClaim ?? false, reason_code: null },
    },
    reject_effect: {
      effect: "hide_unpublished", document_id: overrides.targetId ?? "context-1",
      candidate_version_id: `version-${overrides.targetVersionNo ?? 1}`,
      candidate_version_no: overrides.targetVersionNo ?? 1,
      published_version_id: null, published_version_no: null,
      visible_sibling_review_ids: [], visible_sibling_count: 0,
      cascaded_versions: [], visible_cascaded_review_ids: [], cascaded_review_count: 0,
      restores_review: false, metadata_kept: false, blocked_reason: null,
      impact_fingerprint: "impact-review-1",
    },
    reconciliation_available: overrides.reconciliationAvailable ?? false,
    reconciliation_page_id: overrides.reconciliationPageId ?? null,
    reconciliation_version_id: overrides.reconciliationVersionId ?? null,
    can_discard_reconciliation: overrides.canDiscardReconciliation ?? false,
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
          diff_ref: `context_page_version:version-${overrides.targetVersionNo ?? 1}`,
          document: overrides.document === undefined ? {
            document_kind: "context", document_id: overrides.targetId ?? "context-1",
            title: overrides.targetTitle === undefined ? "Feedback Target" : overrides.targetTitle,
            slug: "feedback-target", reader_href: `/context?target=${overrides.targetId ?? "context-1"}`,
            can_open: true, unavailable_reason: null,
          } : overrides.document,
          content: overrides.content ?? {
            mode: "context_version", candidate_version_id: `version-${overrides.targetVersionNo ?? 1}`,
            candidate_version_no: overrides.targetVersionNo ?? 1,
            baseline_kind: (overrides.targetVersionNo ?? 1) > 1 ? "published" : "empty",
            baseline_version_id: (overrides.targetVersionNo ?? 1) > 1 ? `version-${(overrides.targetVersionNo ?? 1) - 1}` : null,
            baseline_version_no: (overrides.targetVersionNo ?? 1) > 1 ? (overrides.targetVersionNo ?? 1) - 1 : null,
            can_read: true, can_diff: true, unavailable_reason: null, is_current_candidate: true,
          },
        },
      ],
    },
    decisions: overrides.decisions ?? [],
  };
}

function exactPage(workspaceId: string, pageId: string, versionId: string): pagesApi.PagePayload {
  return {
    id: pageId, workspace_id: workspaceId, version_id: versionId,
    title: "Feedback Target", slug: "feedback-target", body_md: "Exact candidate body",
    page_type: "guide", content_hash: "a".repeat(64), editing_revision: 1,
    created_at: "2026-05-09T00:00:00Z", updated_at: "2026-05-09T00:00:00Z",
    backlink_count: 0, children_count: 0, is_alternative: false, is_discarded: false,
    is_draft_head: true, is_public: false, is_published_head: false, is_visible_head: false,
    preserved_as_alternative: false, reconciliation_available: false,
    reconciliation_pending_count: 0, reconciliation_pending_count_truncated: false,
    superseded_review_count: 0,
  };
}

function NavigateButton({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate(to)}>{label}</button>;
}

function selectionPreview(items: ReviewItem[], overrides: Partial<ReviewSelection> = {}): ReviewSelection {
  return {
    selection_token: "fixed-selection-token", expires_at: "2026-10-21T23:15:00Z",
    total: items.length, limit: 5000, review_item_ids: items.map(item => item.id),
    eligible_count: items.length, items: items.map(item => ({
      id: item.id, eligible: true, will_auto_settle: false, reason_code: null, reason: null,
      auto_settled_review_item_ids: [],
      target_title: item.evidence?.operations[0]?.document?.title ?? null,
      target_version_no: item.evidence?.operations[0]?.content?.candidate_version_no ?? null,
    })), auto_settled_review_item_ids: [], ...overrides,
  };
}

function HistoryControls() {
  const navigate = useNavigate();
  return <><button type="button" onClick={() => navigate(-1)}>History back</button>
    <button type="button" onClick={() => navigate(1)}>History forward</button></>;
}

function AuthIdentity() {
  const auth = useOptionalAuth();
  return auth?.user ? <span>Authenticated actor: {auth.user.user_id}</span> : null;
}

function savedDecisionLabel() {
  return i18n.t("review:workflow.savedDecision", { version: "v1", decision: i18n.t("review:overview.decisionType.approve") });
}

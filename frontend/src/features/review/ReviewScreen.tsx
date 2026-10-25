import { inputTextError, textInputLimit } from "../../shared/lib/inputPolicy";
import {
  Check,
  ChevronDown,
  Copy,
  FileText,
  Inbox,
  Pencil,
  RotateCcw,
  X,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import {
  claimReview,
  decideReview,
  decideReviewsBulk,
  discardReviewReconciliationsBulk,
  getReview,
  getSettingsPolicies,
  getReviewCounts,
  listRecentReviewDecisions,
  listReviews,
} from "../../shared/api/operations";
import type {
  ReviewAudienceKind,
  ReviewBulkDecisionResult,
  ReviewCounts,
  ReviewDecisionRecentItem,
  ReviewDecisionType,
  ReviewEvidenceOperation,
  ReviewItem,
  ReviewReconciliationBulkDiscardResult,
  ReviewScope,
} from "../../shared/api/operations";
import { listWorkspaces } from "../../shared/api/workspace";
import { contextEditHref, useRouteWorkspaceId, useWorkspaceRoute, workspacePath, withWorkspaceQuery } from "../../shared/lib/workspaceRoutes";
import { ApiError } from "../../shared/api/client";
import { CountChip } from "../../shared/components/CountChip";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { Dialog } from "../../shared/components/Dialog";
import { FormField } from "../../shared/components/FormField";
import { ReaderTabs } from "../../shared/components/ReaderTabs";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { WorkspaceLayout } from "../../shared/components/WorkspaceLayout";
import { WorkspacePanelChrome } from "../../shared/components/WorkspacePanelChrome";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import {
  formatRelativeTimestamp,
  formatVersionTimestamp,
} from "../../shared/lib/datetime";
import { domainLabel, humanizeEnum } from "../../shared/lib/domainLabel";
import { NotFoundScreen } from "../errors/NotFoundScreen";

type ReviewState = {
  workspaceId: string;
  reviews: ReviewItem[];
  deferredReconciliationCount: number;
  recentDecisions: ReviewDecisionRecentItem[];
  selectedReviewId: string | null;
  nextCursor: string | null;
  canEditDirectly: boolean;
  counts: ReviewCounts;
  reviewPolicy: ReviewRiskPolicySummary;
};
type ReviewRiskPolicySummary = {
  autoApproveByRisk: Array<{
    risk: "low" | "medium" | "high";
    allowed: boolean;
    delayDays: number;
  }>;
  preset: string;
};
type ReviewLifecycleTab = "pending" | "changes_requested" | "resolved";
type ReviewRiskFilter = "all" | "high" | "medium" | "low";
// UI-14: within the Resolved tab, split approved from rejected. The server
// already accepts a concrete status (not just the "resolved" alias) in the
// same `status` query param -- _expand_review_statuses passes it through
// unchanged -- so no backend change is needed for this axis.
type ReviewResolvedStatusFilter = "all" | "approved" | "rejected";
type ReviewRiskLevel = "low" | "medium" | "high";
type ReviewRiskBreakdownValue = { high: number; medium: number; low: number };
type BulkApproveDialog = {
  mode: "selected" | "filter";
  count: number;
  preview: ReviewBulkDecisionResult;
  settlementCount: number | null;
};

const LIFECYCLE_TABS: ReviewLifecycleTab[] = [
  "pending",
  "changes_requested",
  "resolved",
];
const DEFAULT_EMPTY_FOR_STATUS: Record<ReviewLifecycleTab, string> = {
  pending: "No pending items",
  changes_requested: "No change-requested items",
  resolved: "No resolved items",
};
const RISK_FILTERS: ReviewRiskFilter[] = ["all", "high", "medium", "low"];
const RESOLVED_STATUS_FILTERS: ReviewResolvedStatusFilter[] = [
  "all",
  "approved",
  "rejected",
];
const SCOPE_FILTERS: ReviewScope[] = ["all", "to_me", "from_me"];
const EMPTY_RISK_BREAKDOWN = { high: 0, medium: 0, low: 0 };
const REVIEW_DECISION_SUMMARY_DEFAULTS = {
  context_draft_requires_review: "Context draft requires review",
  context_update_requires_review: "Context update draft requires review",
  context_update_workflow_sequence_incomplete:
    "Context update draft requires review because the workflow sequence is incomplete",
} as const;
type DecisionSummaryCarrier = {
  decision_summary?: string | null;
  decision_summary_code?: string | null;
};
const EMPTY_REVIEW_COUNTS: ReviewCounts = {
  pending: 0,
  changes_requested: 0,
  resolved: 0,
  to_me: 0,
  from_me: 0,
  auto_approve_candidate_count: 0,
  auto_approve_candidate_counts_by_risk: { high: 0, medium: 0, low: 0 },
  risk_counts: { high: 0, medium: 0, low: 0 },
  status_by_risk: {
    pending: { ...EMPTY_RISK_BREAKDOWN },
    changes_requested: { ...EMPTY_RISK_BREAKDOWN },
    resolved: { ...EMPTY_RISK_BREAKDOWN },
  },
};
const DEFAULT_REVIEW_POLICY_SUMMARY: ReviewRiskPolicySummary = {
  autoApproveByRisk: [
    { risk: "low", allowed: false, delayDays: 7 },
    { risk: "medium", allowed: false, delayDays: 14 },
    { risk: "high", allowed: false, delayDays: 28 },
  ],
  preset: "balanced",
};
// Workspace roles allowed to edit a change directly (claim + open the editor).
// The server hard-gates this regardless; the FE check is affordance-only.
const EDITOR_ROLES = new Set(["editor", "admin", "owner", "maintainer"]);

export function ReviewScreen() {
  const routeWorkspaceId = useRouteWorkspaceId();
  const route = useWorkspaceRoute();
  const { i18n, t } = useTranslation("review");
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [decisionReason, setDecisionReason] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const loadMoreSentinelRef = useRef<HTMLDivElement | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkDialog, setBulkDialog] = useState<BulkApproveDialog | null>(null);
  const [bulkResult, setBulkResult] =
    useState<ReviewBulkDecisionResult | null>(null);
  const [bulkDiscardResult, setBulkDiscardResult] =
    useState<ReviewReconciliationBulkDiscardResult | null>(null);
  const [bulkDiscardOpen, setBulkDiscardOpen] = useState(false);
  const [bulkDiscardReason, setBulkDiscardReason] = useState("");
  const [selectedBulkIds, setSelectedBulkIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [lifecycleTab, setLifecycleTab] =
    useState<ReviewLifecycleTab>("pending");
  const [riskFilter, setRiskFilter] = useState<ReviewRiskFilter>("all");
  const [resolvedStatusFilter, setResolvedStatusFilter] =
    useState<ReviewResolvedStatusFilter>("all");
  const [scopeFilter, setScopeFilter] = useState<ReviewScope>("all");
  const [includeDeferredReconciliations, setIncludeDeferredReconciliations] =
    useState(false);
  const [reviewTargetRetryToken, setReviewTargetRetryToken] = useState(0);
  const [reviewNotFoundKey, setReviewNotFoundKey] = useState<string | null>(null);
  useEffect(() => {
    setReviewNotFoundKey(null);
  }, [routeWorkspaceId, route?.suffix, route?.targetId]);
  const [railFiltersOpen, setRailFiltersOpen] = useState(false);
  const railFiltersBodyId = useId();
  const activeScopeFilter =
    lifecycleTab === "changes_requested" ? scopeFilter : "all";
  // Only meaningful within the Resolved tab; "resolved" itself is a
  // server-side alias for approved+rejected (_expand_review_statuses), and a
  // concrete status passes through unchanged.
  const activeReviewStatus =
    lifecycleTab === "resolved" && resolvedStatusFilter !== "all"
      ? resolvedStatusFilter
      : lifecycleTab;
  // Load-more pages are only appended to the queue they were requested for:
  // the generation advances on every filter change, so a page that resolves
  // after a tab/risk/scope switch (stale closure or in-flight request) is
  // dropped instead of contaminating the freshly loaded list.
  const loadMoreGenerationRef = useRef(0);
  useEffect(() => {
    loadMoreGenerationRef.current += 1;
  }, [
    activeReviewStatus,
    riskFilter,
    activeScopeFilter,
    includeDeferredReconciliations,
  ]);
  const {
    data: state,
    error,
    reload: reloadReviews,
    setData: setState,
    setError,
  } = useAsyncLoad<ReviewState>(loadReviews, [
    routeWorkspaceId,
    activeReviewStatus,
    riskFilter,
    activeScopeFilter,
    includeDeferredReconciliations,
  ], {
    errorMessage: t("errors.load", { defaultValue: "Failed to load Review" }),
    formatError: reviewErrorMessage,
    fallbackData: {
      workspaceId: "",
      reviews: [],
      deferredReconciliationCount: 0,
      recentDecisions: [],
      selectedReviewId: null,
      nextCursor: null,
      canEditDirectly: false,
      counts: EMPTY_REVIEW_COUNTS,
      reviewPolicy: DEFAULT_REVIEW_POLICY_SUMMARY,
    },
  });

  // Reset the typed decision reason on every selected-review change — covers row
  // clicks, the filter-change reload, and the post-submit reload (all set
  // selectedReviewId) — so a reject reason for one review cannot carry onto the
  // next and be submitted against it (S4, central reset replacing the
  // per-handler reset that missed the reload paths).
  useEffect(() => {
    setDecisionReason("");
  }, [route?.targetId, state?.selectedReviewId]);

  useEffect(() => {
    if (lifecycleTab !== "changes_requested" && scopeFilter !== "all") {
      setScopeFilter("all");
    }
  }, [lifecycleTab, scopeFilter]);

  useEffect(() => {
    setSelectedBulkIds(new Set());
  }, [
    routeWorkspaceId,
    lifecycleTab,
    riskFilter,
    activeScopeFilter,
    includeDeferredReconciliations,
  ]);

  // C5: the URL is the selection source of truth. Every parameter change gets
  // a fresh generation and aborts the request it superseded, so an older slow
  // detail response cannot overwrite a newer click.
  const reviewSelectionGenerationRef = useRef(0);
  const reviewParam = route?.targetId ?? null;
  const reviewRouteIdentity = `${routeWorkspaceId}:${reviewParam ?? ""}`;
  const reviewRouteIdentityRef = useRef(reviewRouteIdentity);
  useLayoutEffect(() => {
    reviewRouteIdentityRef.current = reviewRouteIdentity;
    return () => {
      reviewRouteIdentityRef.current = "";
    };
  }, [reviewRouteIdentity]);
  useEffect(() => {
    if (!state?.workspaceId) {
      return;
    }
    reviewSelectionGenerationRef.current += 1;
    const generation = reviewSelectionGenerationRef.current;
    const controller = new AbortController();
    const isCurrent = () =>
      !controller.signal.aborted &&
      reviewSelectionGenerationRef.current === generation;

    if (!reviewParam) {
      setState((current) =>
        current?.selectedReviewId
          ? { ...current, selectedReviewId: null }
          : current,
      );
      return () => controller.abort();
    }
    const existing = state.reviews.find((review) => review.id === reviewParam);
    if (existing) {
      setState((current) =>
        current && current.selectedReviewId !== reviewParam
          ? { ...current, selectedReviewId: reviewParam }
          : current,
      );
      return () => controller.abort();
    }

    const workspaceId = state.workspaceId;
    void getReview(workspaceId, reviewParam, controller.signal)
      .then((review) => {
        if (!isCurrent()) {
          return;
        }
        const targetTab = lifecycleTabForStatus(review.status);
        if (targetTab !== lifecycleTab) {
          // Do not inject a resolved row into the pending rail (or vice versa).
          // Retire the old query immediately; the next lifecycle load/effect
          // will select the URL target from the matching queue.
          setState((current) =>
            current && current.workspaceId === workspaceId
              ? {
                  ...current,
                  reviews: [],
                  selectedReviewId: null,
                  nextCursor: null,
                }
              : current,
          );
          setLifecycleTab(targetTab);
          return;
        }
        setState((current) => {
          if (!current || current.workspaceId !== workspaceId || !isCurrent()) {
            return current;
          }
          const exists = current.reviews.some((item) => item.id === review.id);
          return {
            ...current,
            reviews: exists ? current.reviews : [review, ...current.reviews],
            selectedReviewId: review.id,
          };
        });
      })
      .catch((exc) => {
        if (
          !isCurrent() ||
          (exc instanceof DOMException && exc.name === "AbortError")
        ) {
          return;
        }
        if (exc instanceof ApiError && exc.status === 404) {
          setReviewNotFoundKey(`${routeWorkspaceId}:${route?.suffix ?? "review"}`);
          return;
        }
        setError(
          reviewErrorMessage(exc, t("errors.restore")),
        );
      });

    return () => controller.abort();
  }, [
    routeWorkspaceId,
    lifecycleTab,
    reviewParam,
    reviewTargetRetryToken,
    route?.suffix,
    setError,
    setState,
    state?.reviews,
    state?.workspaceId,
    t,
  ]);

  // All selection commands update the parameter; the effect above owns state.
  function setReviewParam(reviewId: string | null) {
    navigate(withWorkspaceQuery(reviewId ? workspacePath(routeWorkspaceId, `review/${encodeURIComponent(reviewId)}`) : workspacePath(routeWorkspaceId, "review"), searchParams), { replace: true });
  }

  // R7: auto-load the next page when the rail list is scrolled near its end,
  // replacing the explicit "Load more" button with infinite scroll. The button
  // remains in the DOM (visually hidden) as a keyboard/screen-reader fallback.
  useEffect(() => {
    const sentinel = loadMoreSentinelRef.current;
    if (
      !sentinel ||
      !state?.nextCursor ||
      typeof IntersectionObserver === "undefined"
    ) {
      return;
    }
    let active = true;
    const observer = new IntersectionObserver(
      (entries) => {
        if (active && entries[0]?.isIntersecting && !loadingMore) {
          void loadMoreReviews();
        }
      },
      { rootMargin: "120px" },
    );
    observer.observe(sentinel);
    return () => {
      active = false;
      observer.disconnect();
    };
    // The inline command is intentionally represented by every value it reads
    // so a filter switch retires both the observer and any queued callback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    state?.workspaceId,
    state?.nextCursor,
    loadingMore,
    lifecycleTab,
    riskFilter,
    activeScopeFilter,
    includeDeferredReconciliations,
  ]);

  async function loadReviews(): Promise<ReviewState> {
    const currentWorkspaceId = routeWorkspaceId;
    const [
      reviewPage,
      counts,
      recentDecisionsPage,
      canEditDirectly,
      reviewPolicy,
    ] = await Promise.all([
      listReviews(currentWorkspaceId, reviewListFilters(activeScopeFilter)),
      getReviewCounts(currentWorkspaceId, reviewCountFilters()),
      listRecentReviewDecisions(currentWorkspaceId, { limit: 5 }),
      resolveCanEditDirectly(currentWorkspaceId),
      loadReviewPolicySummary(currentWorkspaceId),
    ]);
    return {
      workspaceId: currentWorkspaceId,
      reviews: reviewPage.items,
      deferredReconciliationCount: deferredReconciliationCount(reviewPage),
      recentDecisions: recentDecisionsPage,
      selectedReviewId: null,
      nextCursor: reviewPage.next_cursor,
      canEditDirectly,
      counts,
      reviewPolicy,
    };
  }

  async function loadMoreReviews() {
    if (!state?.workspaceId || !state.nextCursor || loadingMore) {
      return;
    }
    const generation = loadMoreGenerationRef.current;
    const cursor = state.nextCursor;
    setLoadingMore(true);
    try {
      const nextPage = await listReviews(state.workspaceId, {
        ...reviewListFilters(activeScopeFilter),
        cursor,
      });
      if (loadMoreGenerationRef.current !== generation) {
        return;
      }
      // The cursor must still match too: a decision/refresh reload replaces
      // the list without changing filters, and this page belongs to the old one.
      setState((current) =>
        current && current.nextCursor === cursor
          ? {
              ...current,
              reviews: [...current.reviews, ...nextPage.items],
              deferredReconciliationCount:
                deferredReconciliationCount(nextPage),
              selectedReviewId: current.selectedReviewId,
              nextCursor: nextPage.next_cursor,
            }
          : current,
      );
    } catch (exc) {
      if (loadMoreGenerationRef.current === generation) {
        setError(
          reviewErrorMessage(exc, t("errors.loadMore")),
        );
      }
    } finally {
      setLoadingMore(false);
    }
  }

  // Core decision flow: submits, reloads the current filtered queue, and clears
  // the reason. Throws on failure so the routing dialog can catch the
  // chat_owner_unresolvable 422; the inline Approve/Reject buttons wrap this and
  // route errors to the page banner instead.
  async function runDecision(
    decision: ReviewDecisionType,
    audienceKind?: ReviewAudienceKind,
    reasonOverride?: string,
  ) {
    const selectedReview = reviewParam
      ? state?.reviews.find((review) => review.id === reviewParam)
      : null;
    // The routing dialog manages its own reason field and passes it explicitly;
    // the inline Approve/Reject buttons fall back to the shared decisionReason.
    const reason = (reasonOverride ?? decisionReason).trim();
    const reasonError = inputTextError(reason, 2000, false);
    if (reasonError) throw new Error(reasonError);
    if (
      !state?.workspaceId ||
      !selectedReview ||
      (decision === "request_changes" && !reason)
    ) {
      return;
    }
    setBusyId(selectedReview.id);
    const operationRouteIdentity = reviewRouteIdentity;
    try {
      await decideReview(
        state.workspaceId,
        selectedReview.id,
        decision,
        reason || null,
        audienceKind ? { audience_kind: audienceKind } : {},
      );
      if (reviewRouteIdentityRef.current !== operationRouteIdentity) return;
      const [reviewPage, counts, recentDecisionsPage] = await Promise.all([
        listReviews(state.workspaceId, reviewListFilters(activeScopeFilter)),
        getReviewCounts(state.workspaceId, reviewCountFilters()),
        listRecentReviewDecisions(state.workspaceId, { limit: 5 }),
      ]);
      if (reviewRouteIdentityRef.current !== operationRouteIdentity) return;
      setReviewParam(null);
      setState((current) =>
        current
          ? {
              ...current,
              reviews: reviewPage.items,
              deferredReconciliationCount:
                deferredReconciliationCount(reviewPage),
              selectedReviewId: null,
              nextCursor: reviewPage.next_cursor,
              counts,
              recentDecisions: recentDecisionsPage,
            }
          : current,
      );
      setDecisionReason("");
    } finally {
      if (reviewRouteIdentityRef.current === operationRouteIdentity) setBusyId(null);
    }
  }

  function reviewListFilters(scope: ReviewScope) {
    return {
      status: activeReviewStatus,
      risk_level: riskFilter === "all" ? undefined : riskFilter,
      scope: scope === "all" ? undefined : scope,
      limit: 50,
      include_deferred_reconciliations:
        lifecycleTab !== "resolved" && includeDeferredReconciliations
          ? true
          : undefined,
    };
  }

  function reviewCountFilters() {
    return {
      risk_level: riskFilter === "all" ? undefined : riskFilter,
    };
  }

  function bulkFilterPayload() {
    return {
      status: lifecycleTab,
      risk_level: riskFilter === "all" ? undefined : riskFilter,
      scope: activeScopeFilter,
    };
  }

  function selectedDecisionCount() {
    if (!state || lifecycleTab === "resolved") {
      return 0;
    }
    return state.reviews.filter(
      (review) => selectedBulkIds.has(review.id) && review.can_decide,
    ).length;
  }

  function selectedReconciliationDiscardCount() {
    if (!state || lifecycleTab === "resolved") {
      return 0;
    }
    return state.reviews.filter(
      (review) =>
        selectedBulkIds.has(review.id)
        && review.can_discard_reconciliation === true,
    ).length;
  }

  function toggleBulkSelection(reviewId: string, selected: boolean) {
    setSelectedBulkIds((current) => {
      const next = new Set(current);
      if (selected) {
        next.add(reviewId);
      } else {
        next.delete(reviewId);
      }
      return next;
    });
  }

  // "Everything on screen" is exactly state.reviews: the risk/scope/lifecycle
  // filters are server query params, so the list holds no hidden rows. Loading
  // another cursor page grows it, which drops select-all back to indeterminate
  // on its own because these are derived, not stored.
  function selectableBulkReviews() {
    if (!state || lifecycleTab === "resolved" || !state.canEditDirectly) {
      return [];
    }
    return state.reviews.filter(
      (review) =>
        review.can_decide || review.can_discard_reconciliation === true,
    );
  }

  function toggleSelectAllVisible(selected: boolean) {
    const selectable = selectableBulkReviews();
    setSelectedBulkIds(
      selected ? new Set(selectable.map((review) => review.id)) : new Set(),
    );
  }

  async function refreshQueueAfterBulk() {
    if (!state?.workspaceId) {
      return;
    }
    const operationRouteIdentity = reviewRouteIdentity;
    const [reviewPage, counts, recentDecisionsPage] = await Promise.all([
      listReviews(state.workspaceId, reviewListFilters(activeScopeFilter)),
      getReviewCounts(state.workspaceId, reviewCountFilters()),
      listRecentReviewDecisions(state.workspaceId, { limit: 5 }),
    ]);
    if (reviewRouteIdentityRef.current !== operationRouteIdentity) return;
    setReviewParam(null);
    setState((current) =>
      current
        ? {
            ...current,
            reviews: reviewPage.items,
            deferredReconciliationCount:
              deferredReconciliationCount(reviewPage),
            recentDecisions: recentDecisionsPage,
            selectedReviewId: null,
            nextCursor: reviewPage.next_cursor,
            counts,
          }
        : current,
    );
  }

  async function refreshQueue() {
    if (refreshing) {
      return;
    }
    setRefreshing(true);
    setError(null);
    try {
      await refreshQueueAfterBulk();
    } catch (exc) {
      setError(reviewErrorMessage(exc, t("errors.refresh")));
    } finally {
      setRefreshing(false);
    }
  }

  async function openBulkApproveDialog(mode: "selected" | "filter") {
    if (!state?.workspaceId || bulkBusy) {
      return;
    }
    setBulkBusy(true);
    setError(null);
    try {
      const preview = await decideReviewsBulk(
        state.workspaceId,
        mode === "selected"
          ? {
              review_item_ids: state.reviews
                .filter(
                  (review) => selectedBulkIds.has(review.id) && review.can_decide,
                )
                .map((review) => review.id),
              filters: null,
              decision_type: "approve",
              dry_run: true,
            }
          : {
              review_item_ids: null,
              filters: bulkFilterPayload(),
              decision_type: "approve",
              dry_run: true,
            },
      );
      setBulkDialog({
        mode,
        count: preview.approved.length,
        preview,
        settlementCount:
          mode === "selected"
            ? bulkSettlementCount(state.reviews, new Set(preview.approved))
            : null,
      });
    } catch (exc) {
      setError(
        reviewErrorMessage(exc, t("errors.bulkPreview")),
      );
    } finally {
      setBulkBusy(false);
    }
  }

  async function runBulkApprove() {
    if (!state?.workspaceId || !bulkDialog || bulkBusy) {
      return;
    }
    setBulkBusy(true);
    setError(null);
    try {
      const result = await decideReviewsBulk(
        state.workspaceId,
        bulkDialog.mode === "selected"
          ? {
              review_item_ids: state.reviews
                .filter(
                  (review) => selectedBulkIds.has(review.id) && review.can_decide,
                )
                .map((review) => review.id),
              filters: null,
              decision_type: "approve",
            }
          : {
              review_item_ids: null,
              filters: bulkFilterPayload(),
              decision_type: "approve",
            },
      );
      setBulkResult(result);
      setSelectedBulkIds(new Set());
      setBulkDialog(null);
      await refreshQueueAfterBulk();
    } catch (exc) {
      setError(reviewErrorMessage(exc, t("errors.bulkApprove")));
    } finally {
      setBulkBusy(false);
    }
  }

  async function runBulkReconciliationDiscard() {
    if (
      !state?.workspaceId
      || bulkBusy
      || bulkDiscardReason.trim().length === 0
    ) {
      return;
    }
    const reviewItemIds = state.reviews
      .filter(
        (review) =>
          selectedBulkIds.has(review.id)
          && review.can_discard_reconciliation === true,
      )
      .map((review) => review.id);
    if (reviewItemIds.length === 0) {
      return;
    }
    setBulkBusy(true);
    setError(null);
    try {
      const result = await discardReviewReconciliationsBulk(
        state.workspaceId,
        {
          review_item_ids: reviewItemIds,
          reason: bulkDiscardReason.trim(),
        },
      );
      setBulkDiscardResult(result);
      setBulkDiscardOpen(false);
      setBulkDiscardReason("");
      setSelectedBulkIds(new Set(result.skipped.map((item) => item.review_item_id)));
      await refreshQueueAfterBulk();
    } catch (exc) {
      setError(
        reviewErrorMessage(
          exc,
          t("errors.bulkDiscard", {
            defaultValue: "Failed to discard reconciliation branches",
          }),
        ),
      );
    } finally {
      setBulkBusy(false);
    }
  }

  function closeBulkDialog() {
    if (!bulkBusy) {
      setBulkDialog(null);
    }
  }

  async function submitDecision(decision: ReviewDecisionType) {
    try {
      await runDecision(decision);
    } catch (exc) {
      setError(reviewErrorMessage(exc, t("errors.decide")));
    }
  }

  // Edit-directly: claim the item, then route to the target page's editor. The
  // server hard-gates the claim; we surface failures (incl. claim conflicts) on
  // the page banner.
  async function editDirectly(review: ReviewItem) {
    if (!state?.workspaceId) {
      return;
    }
    const primaryOperation = reviewPrimaryOperation(review);
    const targetPageId = primaryOperation?.target_type === "context_page"
      ? primaryOperation.target_id
      : null;
    if (!targetPageId) {
      setError(
        t("editDirectly.noTarget", {
          defaultValue: "This change has no editable page target.",
        }),
      );
      return;
    }
    setBusyId(review.id);
    const operationRouteIdentity = reviewRouteIdentity;
    try {
      await claimReview(state.workspaceId, review.id);
      if (reviewRouteIdentityRef.current !== operationRouteIdentity) return;
      navigate(`${contextEditHref(state.workspaceId, targetPageId)}?from=review&reviewItemId=${encodeURIComponent(review.id)}`);
    } catch (exc) {
      if (reviewRouteIdentityRef.current === operationRouteIdentity) {
        setError(reviewErrorMessage(exc, t("errors.claim")));
        setBusyId(null);
      }
    } finally {
      if (reviewRouteIdentityRef.current === operationRouteIdentity) setBusyId(null);
    }
  }

  if (reviewNotFoundKey === `${routeWorkspaceId}:${route?.suffix ?? "review"}`) {
    return <NotFoundScreen />;
  }

  if (!state && error) {
    return (
      <section className="feature-main">
        <StatusBanner
          tone="warning"
          action={<button className="icon-button" onClick={() => { setError(null); void reloadReviews(); setReviewTargetRetryToken((token) => token + 1); }} type="button">{t("workspaceResolution.retry", { ns: "shell", defaultValue: "Retry" })}</button>}
        >
          {error}
        </StatusBanner>
      </section>
    );
  }
  if (!state) {
    return <RouteLoading />;
  }

  const selectedReview = reviewParam
    ? state.reviews.find((review) => review.id === reviewParam) ?? null
    : null;
  const groupedReviews = lifecycleTab === "pending"
    ? groupPendingReviewsByPage(state.reviews, t)
    : groupReviewsByDate(state.reviews);
  // Once the rail has pulled every page there is nothing left to arrive, so a
  // group's size IS the page's total and the header can say so. While a cursor
  // is still outstanding a fourth draft for the same page may be on the next
  // page of results, and the count is only what is on screen.
  const railIsComplete = state.nextCursor === null;
  const reviewLocale = normalizeReviewLocale(i18n.language);
  // The server zeroes workspace-wide counts for sub-editor roles while the
  // rail still lists their personal queue, so the misleading "0 items" badges
  // are hidden for viewers (their real counts live in to_me/from_me).
  const showWorkspaceCounts = state.canEditDirectly;
  const lifecycleTabs = LIFECYCLE_TABS.map((tab) => {
    const count = state.counts[tab];
    return {
      key: tab,
      label: t(`filters.statusOptions.${tab}`, {
        defaultValue: defaultLifecycleLabel(tab),
      }),
      count: showWorkspaceCounts ? count : undefined,
      countLabel: t("filters.countLabel", {
        count,
        defaultValue: "{{count}} items",
      }),
      showZero: showWorkspaceCounts,
    };
  });
  const selectedBulkCount = selectedDecisionCount();
  const selectedReconciliationCount = selectedReconciliationDiscardCount();
  const activeLifecycleLabel = t(`filters.statusOptions.${lifecycleTab}`, {
    defaultValue: defaultLifecycleLabel(lifecycleTab),
  });
  const canBulkApproveSelected =
    state.canEditDirectly && lifecycleTab !== "resolved" && selectedBulkCount > 0;

  return (
    <>
      <WorkspaceLayout
        defaultInspectorOpen={false}
        inspector={null}
        layoutKey="review"
        variant="review"
        leftLabel={t("rail.ariaLabel", { defaultValue: "Review queue" })}
        leftRestoreLabel={t("rail.restore", { defaultValue: "Show Review queue" })}
        left={({ expandWidthAtMax, expandWidthLabel, onClose, onExpandWidth }) => (
          <aside
            className="review-rail"
            aria-label={t("rail.ariaLabel", { defaultValue: "Review queue" })}
          >
          <div className="review-rail-header">
            <div className="review-rail-heading">
              <p className="eyebrow">
                {t("header.scopedEyebrow", {
                  status: activeLifecycleLabel,
                  defaultValue: "Review queue · {{status}}",
                })}
              </p>
              <CountChip value={state.counts[lifecycleTab]} compact />
            </div>
            <div className="review-rail-actions">
              <WorkspacePanelChrome
                refresh={{
                  label: t("rail.refresh", { defaultValue: "Refresh reviews" }),
                  onRefresh: refreshQueue,
                  refreshing,
                }}
                expandWidth={
                  onExpandWidth
                    ? {
                        atMax: expandWidthAtMax,
                        label: expandWidthLabel,
                        onExpandWidth,
                      }
                    : undefined
                }
                close={{
                  label: t("rail.close", { defaultValue: "Close Review queue" }),
                  onClose,
                }}
              />
            </div>
          </div>
          <section
            className="review-rail-filter-section"
            aria-label={t("filters.title", { defaultValue: "Filters" })}
          >
            {/* The lifecycle tabs are the rail's primary navigation and the only
                in-rail route to the other queues, so they stay outside the
                collapsible body; only the secondary axes fold away. They also
                keep the results tabpanel's aria-labelledby target mounted. */}
            <ReaderTabs
              accent="review"
              active={lifecycleTab}
              ariaLabel={t("filters.lifecycleAriaLabel", {
                defaultValue: "Review lifecycle",
              })}
              idBase="review-lifecycle"
              onChange={(next) => {
                setReviewParam(null);
                setState((current) =>
                  current
                    ? {
                        ...current,
                        reviews: [],
                        selectedReviewId: null,
                        nextCursor: null,
                      }
                    : current,
                );
                setLifecycleTab(next);
                setScopeFilter("all");
              }}
              tabs={lifecycleTabs}
            />
            <button
              aria-controls={railFiltersBodyId}
              aria-expanded={railFiltersOpen}
              className="review-filter-toggle"
              onClick={() => setRailFiltersOpen((open) => !open)}
              type="button"
            >
              <span>{t("filters.title", { defaultValue: "Filters" })}</span>
              <ChevronDown
                aria-hidden="true"
                data-open={railFiltersOpen}
                size={14}
              />
            </button>
            {railFiltersOpen ? (
              <div className="review-rail-filters" id={railFiltersBodyId}>
                {lifecycleTab === "changes_requested" ? (
                  <ReviewFilterAxis
                    label={t("filters.scope", { defaultValue: "Audience" })}
                  >
                    <SegmentedControl
                      label={t("filters.audienceAriaLabel", {
                        defaultValue: "Review audience",
                      })}
                      options={SCOPE_FILTERS.map((scope) => {
                        // For sub-editor roles the server clamps scope=all to
                        // to_me and zeroes the workspace-wide count, so "All"
                        // shows the personal count it actually resolves to.
                        const count =
                          scope === "all"
                            ? showWorkspaceCounts
                              ? state.counts.changes_requested
                              : state.counts.to_me
                            : state.counts[scope];
                        return {
                          label: (
                            <span className="review-filter-option-label">
                              <span>
                                {t(`filters.scopeOptions.${scope}`, {
                                  defaultValue: humanizeEnum(scope),
                                })}
                              </span>
                              <span className="review-filter-count">{count}</span>
                            </span>
                          ),
                          key: scope,
                        };
                      })}
                      value={scopeFilter}
                      onChange={(next) => {
                        setReviewParam(null);
                        setScopeFilter(next);
                      }}
                    />
                  </ReviewFilterAxis>
                ) : null}
                <ReviewFilterAxis
                  label={t("filters.risk", { defaultValue: "Risk" })}
                >
                  <SegmentedControl
                    label={t("filters.risk", { defaultValue: "Risk" })}
                    options={RISK_FILTERS.map((risk) => ({
                      label: t(`filters.riskOptions.${risk}`, {
                        defaultValue: humanizeEnum(risk),
                      }),
                      key: risk,
                    }))}
                    value={riskFilter}
                    onChange={(next) => {
                      setReviewParam(null);
                      setRiskFilter(next);
                    }}
                  />
                </ReviewFilterAxis>
                {lifecycleTab !== "resolved"
                  && (state.deferredReconciliationCount > 0
                    || includeDeferredReconciliations) ? (
                  <ReviewFilterAxis
                    label={t("filters.olderReconciliations", {
                      defaultValue: "Older reconciliation branches",
                    })}
                  >
                    <label className="review-deferred-reconciliation-toggle">
                      <input
                        checked={includeDeferredReconciliations}
                        onChange={(event) => {
                          setReviewParam(null);
                          setIncludeDeferredReconciliations(
                            event.currentTarget.checked,
                          );
                        }}
                        type="checkbox"
                      />
                      <span>
                        {t("filters.showOlderReconciliations", {
                          count: state.deferredReconciliationCount,
                          defaultValue:
                            "Show {{count}} older reconciliation branches",
                        })}
                      </span>
                    </label>
                  </ReviewFilterAxis>
                ) : null}
                {lifecycleTab === "resolved" ? (
                  // UI-14: "Resolved" merges approved + rejected with no way
                  // to see only one. Reuses filters.statusOptions (already
                  // has approved/rejected/all) and RISK_FILTERS' segmented
                  // chip pattern, so no new locale keys or CSS.
                  <ReviewFilterAxis
                    label={t("filters.status", { defaultValue: "Status" })}
                  >
                    <SegmentedControl
                      label={t("filters.status", { defaultValue: "Status" })}
                      options={RESOLVED_STATUS_FILTERS.map((status) => ({
                        label: t(`filters.statusOptions.${status}`, {
                          defaultValue: humanizeEnum(status),
                        }),
                        key: status,
                      }))}
                      value={resolvedStatusFilter}
                      onChange={(next) => {
                        setReviewParam(null);
                        setResolvedStatusFilter(next);
                      }}
                    />
                  </ReviewFilterAxis>
                ) : null}
              </div>
            ) : null}
          </section>
          {/* The bar now shows whenever anything is selectable, because it
              hosts the select-all control -- previously it only appeared once a
              selection existed, so there was no way to start one in bulk. */}
          {selectableBulkReviews().length > 0 ? (
            <div className="review-bulk-bar">
              <label className="review-bulk-select-all">
                <SelectAllCheckbox
                  ariaLabel={t("bulk.selectAllVisible", {
                    count: selectableBulkReviews().length,
                    defaultValue: "Select all {{count}} shown",
                  })}
                  checked={
                    selectedBulkIds.size > 0 &&
                    selectableBulkReviews().every((review) =>
                      selectedBulkIds.has(review.id),
                    )
                  }
                  indeterminate={
                    selectedBulkIds.size > 0 &&
                    !selectableBulkReviews().every((review) =>
                      selectedBulkIds.has(review.id),
                    )
                  }
                  onChange={toggleSelectAllVisible}
                />
                <span role="status" aria-live="polite">
                  {selectedBulkIds.size > 0
                    ? t("bulk.selected", {
                        count: selectedBulkIds.size,
                        defaultValue: "{{count}} selected",
                      })
                    : t("bulk.selectAllVisible", {
                        count: selectableBulkReviews().length,
                        defaultValue: "Select all {{count}} shown",
                      })}
                </span>
              </label>
              {selectedBulkIds.size > 0 ? (
                <div className="review-bulk-actions">
                  {selectedBulkCount > 0 ? (
                    <button
                      className="icon-button success"
                      disabled={!canBulkApproveSelected}
                      onClick={() => void openBulkApproveDialog("selected")}
                      type="button"
                    >
                      <Check size={14} aria-hidden="true" />
                      <span>
                        {t("bulk.approveSelected", {
                          count: selectedBulkCount,
                          defaultValue: "Approve selected",
                        })}
                      </span>
                    </button>
                  ) : null}
                  {selectedReconciliationCount > 0 ? (
                    <button
                      className="icon-button danger"
                      disabled={bulkBusy}
                      onClick={() => {
                        setBulkDiscardReason("");
                        setBulkDiscardOpen(true);
                      }}
                      type="button"
                    >
                      <X size={14} aria-hidden="true" />
                      <span>
                        {t("bulk.discardReconciliations", {
                          count: selectedReconciliationCount,
                          defaultValue: "Discard selected branches",
                        })}
                      </span>
                    </button>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
          <div
            aria-labelledby={`review-lifecycle-tab-${lifecycleTab}`}
            className="review-rail-results"
            id={`review-lifecycle-panel-${lifecycleTab}`}
            role="tabpanel"
          >
            {state.reviews.length === 0 ? (
              <div className="review-rail-empty">
                <Inbox size={18} aria-hidden="true" />
                <p>
                  {/* One phrase per lane rather than an interpolated tab label:
                      the labels are capitalised and "Changes requested" cannot
                      be spliced into a sentence in either locale. */}
                  {t(`rail.emptyForStatus_${lifecycleTab}`, {
                    defaultValue: DEFAULT_EMPTY_FOR_STATUS[lifecycleTab],
                  })}
                </p>
                {lifecycleTab !== "pending" ? (
                  <button className="inline-action" onClick={() => {
                    setReviewParam(null);
                    setLifecycleTab("pending");
                    setScopeFilter("all");
                    setRiskFilter("all");
                  }} type="button">
                    {t("rail.showPending", { defaultValue: "Show pending" })}
                  </button>
                ) : railFiltersOpen && riskFilter !== "all" ? (
                  <button className="inline-action" onClick={() => {
                    setReviewParam(null);
                    setRiskFilter("all");
                  }} type="button">
                    {t("rail.clearFilters", { defaultValue: "Clear filters" })}
                  </button>
                ) : null}
              </div>
            ) : (
              <div className="review-rail-list">
                {groupedReviews.map((group) => (
                  <div
                    aria-label={
                      group.pageTitle && group.reviews.length > 1
                        ? pageGroupLabel(t, group, railIsComplete)
                        : undefined
                    }
                    className={`review-date-group${
                      group.pageTitle && group.reviews.length > 1
                        ? " review-page-group"
                        : ""
                    }`}
                    key={group.key}
                    role={group.pageTitle && group.reviews.length > 1 ? "group" : undefined}
                  >
                    <div className="review-date-divider">
                      {group.pageTitle && group.reviews.length > 1
                        ? pageGroupLabel(t, group, railIsComplete)
                        : formatReviewDate(group.activityAt, reviewLocale)}
                    </div>
                    {group.reviews.map((review) => (
                      <ReviewRow
                        active={review.id === selectedReview?.id}
                        bulkDisabled={
                          lifecycleTab === "resolved" ||
                          !state.canEditDirectly ||
                          (!review.can_decide
                            && review.can_discard_reconciliation !== true)
                        }
                        bulkSelectable={
                          lifecycleTab !== "resolved"
                          && (review.can_decide
                            || review.can_discard_reconciliation === true)
                        }
                        bulkSelected={selectedBulkIds.has(review.id)}
                        key={review.id}
                        onSelect={(reviewId) => {
                          // decisionReason is reset centrally by the effect keyed on
                          // selectedReviewId (covers this + the reload paths).
                          setReviewParam(reviewId);
                        }}
                        onToggleBulkSelected={toggleBulkSelection}
                        review={review}
                        // Every row in a lifecycle-scoped queue carries the
                        // tab's own status, so the pill only earns its place
                        // where the lane mixes approved and rejected.
                        showStatusPill={lifecycleTab === "resolved"}
                      />
                    ))}
                  </div>
                ))}
                {state.nextCursor ? (
                  <div
                    aria-busy={loadingMore}
                    aria-live="polite"
                    className="load-more-row"
                    ref={loadMoreSentinelRef}
                  >
                    {loadingMore ? (
                      <span className="review-rail-loading-more">
                        {t("pending.loadingMore", { defaultValue: "Loading" })}
                      </span>
                    ) : null}
                    <button
                      className="visually-hidden"
                      disabled={loadingMore}
                      onClick={() => void loadMoreReviews()}
                      type="button"
                    >
                      {t("pending.loadMore", { defaultValue: "Load more" })}
                    </button>
                  </div>
                ) : null}
              </div>
            )}
          </div>
          </aside>
        )}
        main={
          <section className="feature-main review-main">
            <Breadcrumbs
              items={
                selectedReview
                  ? [
                      {
                        label: t("breadcrumb", { defaultValue: "Review" }),
                        onClick: () => setReviewParam(null),
                      },
                      {
                        label: reviewTargetTitle(selectedReview, t),
                      },
                    ]
                  : [{ label: t("breadcrumb", { defaultValue: "Review" }) }]
              }
            />
            {error ? <StatusBanner tone="warning" action={<button className="icon-button" onClick={() => { setError(null); void reloadReviews(); setReviewTargetRetryToken((token) => token + 1); }} type="button">{t("workspaceResolution.retry", { ns: "shell", defaultValue: "Retry" })}</button>}>{error}</StatusBanner> : null}
            {bulkResult ? (
              <StatusBanner tone="info">
                <span>
                  {t("bulk.result", {
                    approved: bulkResult.approved.length,
                    skipped: bulkResult.skipped.length,
                    defaultValue:
                      "Approved {{approved}} reviews. Skipped {{skipped}}.",
                  })}
                </span>
                {bulkResult.skipped.length > 0 ? (
                  <ul className="review-bulk-skip-reasons">
                    <li>
                      {t("bulk.couldNotApprove", {
                        count: bulkResult.skipped.length,
                        defaultValue: "{{count}} item(s) could not be approved",
                      })}
                    </li>
                  </ul>
                ) : null}
              </StatusBanner>
            ) : null}
            {bulkDiscardResult ? (
              <StatusBanner tone="info">
                <span>
                  {t("bulk.discardResult", {
                    discarded:
                      bulkDiscardResult.discarded_review_item_ids.length,
                    skipped: bulkDiscardResult.skipped.length,
                    defaultValue:
                      "Discarded {{discarded}} branches. Skipped {{skipped}}.",
                  })}
                </span>
                {bulkDiscardResult.skipped.length > 0 ? (
                  <ul className="review-bulk-skip-reasons">
                    {bulkDiscardResult.skipped.map((item) => (
                      <li key={item.review_item_id}>
                        {shortIdentifier(item.review_item_id)}: {item.error_code}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </StatusBanner>
            ) : null}
            {reviewParam && !selectedReview ? (
              error ? (
                <StatusBanner
                  tone="warning"
                  action={<button className="icon-button" onClick={() => { setError(null); void reloadReviews(); setReviewTargetRetryToken((token) => token + 1); }} type="button">{t("workspaceResolution.retry", { ns: "shell", defaultValue: "Retry" })}</button>}
                >
                  {error}
                </StatusBanner>
              ) : <RouteLoading />
            ) : selectedReview ? (
              <>
                <ReviewDetail
                  busy={busyId === selectedReview.id}
                  canEditDirectly={state.canEditDirectly}
                  decisionReason={decisionReason}
                  onDecision={submitDecision}
                  onEditDirectly={() => void editDirectly(selectedReview)}
                  onReasonChange={setDecisionReason}
                  onRouteChanges={(audienceKind, reason) =>
                    runDecision("request_changes", audienceKind, reason)
                  }
                  review={selectedReview}
                />
              </>
            ) : (
              <ReviewOverview
                counts={state.counts}
                onOpenRiskPolicy={(risk) =>
                  navigate(`${workspacePath(routeWorkspaceId, "settings")}?section=review#auto-approve-${risk}`)
                }
                onSelectLifecycle={(status) => setLifecycleTab(status)}
                onSelectLifecycleRisk={(status, risk) => {
                  setLifecycleTab(status);
                  setRiskFilter(risk);
                }}
                recentDecisions={state.recentDecisions}
                reviewPolicy={state.reviewPolicy}
              />
            )}
          </section>
        }
      />
      {bulkDialog ? (
        <Dialog
          className="review-bulk-dialog"
          dismissible={!bulkBusy}
          labelledBy="review-bulk-dialog-title"
          onClose={closeBulkDialog}
        >
          <div className="dialog-form">
            <h2 id="review-bulk-dialog-title">
              {t("bulk.confirmTitle", { defaultValue: "Review bulk approval" })}
            </h2>
            <p>
              {t("bulk.confirmBody", {
                count: bulkDialog.count,
                defaultValue:
                  "{{count}} reviews are eligible to approve under the current filters. Items that can no longer be approved are skipped and listed in the result.",
              })}
            </p>
            <div className="review-bulk-preview" role="status">
              <span>
                {t("bulk.previewEligible", {
                  count: bulkDialog.preview.approved.length,
                  defaultValue: "{{count}} eligible",
                })}
              </span>
              <span>
                {t("bulk.previewSkipped", {
                  count: bulkDialog.preview.skipped.length,
                  defaultValue: "{{count}} skipped",
                })}
              </span>
            </div>
            {bulkDialog.settlementCount != null
            && bulkDialog.settlementCount > 0 ? (
              <StatusBanner tone="warning">
                {t("bulk.approvalImpact", {
                  count: bulkDialog.settlementCount,
                  defaultValue:
                    "Approving the selected page versions will also settle {{count}} earlier pending reviews.",
                })}
              </StatusBanner>
            ) : null}
            {bulkDialog.preview.skipped.length > 0 ? (
              <ul className="review-bulk-skip-reasons">
                <li>
                  {t("bulk.couldNotApprove", {
                    count: bulkDialog.preview.skipped.length,
                    defaultValue: "{{count}} item(s) could not be approved",
                  })}
                </li>
              </ul>
            ) : null}
            <div className="dialog-actions">
              <button
                className="icon-button"
                disabled={bulkBusy}
                onClick={closeBulkDialog}
                type="button"
              >
                {t("actions.cancel", { defaultValue: "Cancel" })}
              </button>
              <button
                className="icon-button success"
                disabled={bulkBusy || bulkDialog.count === 0}
                onClick={() => void runBulkApprove()}
                type="button"
              >
                {bulkBusy
                  ? t("bulk.approving", { defaultValue: "Approving" })
                  : t("bulk.confirmApproveCount", {
                      count: bulkDialog.count,
                      defaultValue: "Approve {{count}}",
                    })}
              </button>
            </div>
          </div>
        </Dialog>
      ) : null}
      {bulkDiscardOpen ? (
        <Dialog
          className="review-bulk-dialog"
          dismissible={!bulkBusy}
          labelledBy="review-bulk-discard-title"
          onClose={() => {
            if (!bulkBusy) {
              setBulkDiscardOpen(false);
              setBulkDiscardReason("");
            }
          }}
        >
          <div className="dialog-form">
            <h2 id="review-bulk-discard-title">
              {t("bulk.discardTitle", {
                defaultValue: "Discard selected reconciliation branches?",
              })}
            </h2>
            <p>
              {t("bulk.discardBody", {
                count: selectedReconciliationCount,
                defaultValue:
                  "Only explicitly eligible branches will be discarded. Ineligible or stale items are returned with an error code.",
              })}
            </p>
            <label>
              <span>
                {t("bulk.discardReason", { defaultValue: "Discard reason" })}
              </span>
              <textarea
              {...textInputLimit(2000, false)}
                autoFocus
                required
                value={bulkDiscardReason}
                onChange={(event) =>
                  setBulkDiscardReason(event.currentTarget.value)
                }
              />
            </label>
            <div className="dialog-actions">
              <button
                className="icon-button text-button"
                disabled={bulkBusy}
                onClick={() => setBulkDiscardOpen(false)}
                type="button"
              >
                {t("bulk.cancel", { defaultValue: "Cancel" })}
              </button>
              <button
                className="danger"
                disabled={
                  bulkBusy
                  || bulkDiscardReason.trim().length === 0
                  || selectedReconciliationCount === 0
                }
                onClick={() => void runBulkReconciliationDiscard()}
                type="button"
              >
                {bulkBusy
                  ? t("bulk.discarding", { defaultValue: "Discarding" })
                  : t("bulk.confirmDiscard", {
                      count: selectedReconciliationCount,
                      defaultValue: "Discard selected branches",
                    })}
              </button>
            </div>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function QueueStatusCard({
  status,
  total,
  breakdown,
  onSelectStatus,
  onSelectStatusRisk,
}: {
  status: ReviewLifecycleTab;
  total: number;
  breakdown: ReviewRiskBreakdownValue | null;
  onSelectStatus: (status: ReviewLifecycleTab) => void;
  onSelectStatusRisk: (
    status: ReviewLifecycleTab,
    risk: ReviewRiskLevel,
  ) => void;
}) {
  const { t } = useTranslation("review");
  const label = t(`filters.statusOptions.${status}`, {
    defaultValue: humanizeEnum(status),
  });
  return (
    <div className="review-queue-card">
      <button
        type="button"
        className="review-queue-card__header"
        onClick={() => onSelectStatus(status)}
      >
        <span className="review-queue-card__label">{label}</span>
        <span className="review-queue-card__value">{total}</span>
      </button>
      {breakdown ? (
        <div className="review-queue-card__risks">
          {(["high", "medium", "low"] as const).map((risk) => (
            <button
              type="button"
              key={risk}
              className={`review-queue-risk-chip review-queue-risk-chip--${risk}`}
              onClick={() => onSelectStatusRisk(status, risk)}
              aria-label={t("overview.queueRiskAria", {
                status: label,
                risk: t(`policy.riskValue.${risk}`, {
                  defaultValue: humanizeEnum(risk),
                }),
                count: breakdown[risk],
                defaultValue: `${label} ${risk}: ${breakdown[risk]}`,
              })}
            >
              <span>
                {t(`policy.riskValue.${risk}`, { defaultValue: humanizeEnum(risk) })}
              </span>
              <strong>{breakdown[risk]}</strong>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SelectAllCheckbox({
  ariaLabel,
  checked,
  indeterminate,
  onChange,
}: {
  ariaLabel: string;
  checked: boolean;
  indeterminate: boolean;
  onChange: (selected: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (ref.current) {
      // `indeterminate` has no HTML attribute — it is DOM-only state.
      ref.current.indeterminate = indeterminate;
    }
  }, [indeterminate]);
  return (
    <input
      aria-label={ariaLabel}
      checked={checked}
      onChange={(event) => onChange(event.currentTarget.checked)}
      ref={ref}
      type="checkbox"
    />
  );
}

function ReviewFilterAxis({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="review-filter-axis" role="group" aria-label={label}>
      <span className="eyebrow">{label}</span>
      {children}
    </div>
  );
}

function ReviewOverview({
  counts,
  onOpenRiskPolicy,
  onSelectLifecycle,
  onSelectLifecycleRisk,
  recentDecisions,
  reviewPolicy,
}: {
  counts: ReviewCounts;
  onOpenRiskPolicy: (risk: ReviewRiskLevel) => void;
  onSelectLifecycle: (status: ReviewLifecycleTab) => void;
  onSelectLifecycleRisk: (
    status: ReviewLifecycleTab,
    risk: ReviewRiskLevel,
  ) => void;
  recentDecisions: ReviewDecisionRecentItem[];
  reviewPolicy: ReviewRiskPolicySummary;
}) {
  const { i18n, t } = useTranslation("review");
  const routeWorkspaceId = useRouteWorkspaceId();
  return (
    <section className="review-overview" aria-labelledby="review-overview-title">
      <div className="feature-header">
        <div>
          <p className="eyebrow">
            {t("overview.eyebrow", { defaultValue: "Queue overview" })}
          </p>
          <h1 id="review-overview-title">
            {t("overview.title", { defaultValue: "Review operations" })}
          </h1>
          <p className="feature-subtitle">
            {t("overview.description", {
              defaultValue:
                "Track open review load, approve low-friction queues, and inspect recent decisions.",
            })}
          </p>
        </div>
      </div>
      {/* RO-1: per-status queue cards with risk chips. Resolved shows the total
          only; the separate Risk distribution block was folded in here. */}
      <div className="review-queue-grid">
        <QueueStatusCard
          status="pending"
          total={counts.pending}
          breakdown={counts.status_by_risk?.pending ?? EMPTY_RISK_BREAKDOWN}
          onSelectStatus={onSelectLifecycle}
          onSelectStatusRisk={onSelectLifecycleRisk}
        />
        <QueueStatusCard
          status="changes_requested"
          total={counts.changes_requested}
          breakdown={
            counts.status_by_risk?.changes_requested ?? EMPTY_RISK_BREAKDOWN
          }
          onSelectStatus={onSelectLifecycle}
          onSelectStatusRisk={onSelectLifecycleRisk}
        />
        <QueueStatusCard
          status="resolved"
          total={counts.resolved}
          breakdown={null}
          onSelectStatus={onSelectLifecycle}
          onSelectStatusRisk={onSelectLifecycleRisk}
        />
      </div>
      <div className="feature-section review-policy-card">
        <div className="panel-header review-recent-header">
          <span>{t("policy.title", { defaultValue: "Review risk policy" })}</span>
          <Link className="inline-action" to={`${workspacePath(routeWorkspaceId, "settings")}?section=review`}>
            {t("policy.settingsLink", { defaultValue: "Open Settings" })}
          </Link>
        </div>
        <p>
          {reviewPolicy.autoApproveByRisk.some((row) => row.allowed)
            ? t("policy.enabledDescription", {
                defaultValue:
                  "Auto-approval uses separate Low and Medium delay rules. High-risk items always require manual review.",
              })
            : t("policy.disabledDescription", {
                defaultValue:
                  "Auto-approval is off. Review items stay in the queue until an editor decides.",
              })}
        </p>
        <div className="detail-grid">
          <DetailMetric
            label={t("policy.preset", { defaultValue: "Preset" })}
            value={t(`policy.presetValue.${reviewPolicy.preset}`, {
              defaultValue: humanizeEnum(reviewPolicy.preset),
            })}
          />
          <DetailMetric
            label={t("policy.candidates", { defaultValue: "Eligible now" })}
            value={counts.auto_approve_candidate_count}
          />
        </div>
        <div className="review-auto-approve-matrix">
          {/* The rows are dense value cells; without a header row the second and
              fourth columns read as unlabelled fragments. */}
          <div className="review-auto-approve-head">
            <span>{t("policy.columnRisk", { defaultValue: "Risk" })}</span>
            <span>{t("policy.columnMode", { defaultValue: "Mode" })}</span>
            <span>{t("policy.columnDelay", { defaultValue: "Delay" })}</span>
            <span>{t("policy.columnEligible", { defaultValue: "Eligible" })}</span>
          </div>
          {reviewPolicy.autoApproveByRisk.map((row) => {
            const eligible =
              counts.auto_approve_candidate_counts_by_risk?.[row.risk] ?? 0;
            return (
              // RO-4: each policy row deep-links to its Settings auto-approve row.
              <button
                type="button"
                key={row.risk}
                className="review-auto-approve-row"
                onClick={() => onOpenRiskPolicy(row.risk)}
                aria-label={t("policy.openSettingsAria", {
                  risk: t(`policy.riskValue.${row.risk}`, {
                    defaultValue: humanizeEnum(row.risk),
                  }),
                  defaultValue: `Open ${row.risk} risk auto-approval settings`,
                })}
              >
                <strong>
                  {t(`policy.riskValue.${row.risk}`, {
                    defaultValue: humanizeEnum(row.risk),
                  })}
                </strong>
                <span>
                  {row.allowed
                    ? t("policy.autoApproveLabel", {
                        defaultValue: "Auto-approve",
                      })
                    : t("policy.manualLabel", { defaultValue: "Manual" })}
                </span>
                <span className={row.allowed || row.risk === "high" ? undefined : "muted"}>
                  {row.risk === "high"
                    ? t("policy.highManualReason", {
                        defaultValue: "High risk is locked to manual review",
                      })
                    : row.allowed
                      ? row.delayDays === 0
                        ? t("policy.delayNotApplied", {
                            defaultValue: "No auto-approval delay",
                          })
                        : t("policy.delayDays", {
                            count: row.delayDays,
                            defaultValue: "{{count}} days",
                          })
                      : t("policy.delayNotApplied", {
                          defaultValue: "No auto-approval delay",
                        })}
                </span>
                {/* L-10 (260908 UI audit): the column header directly above
                    this cell already reads "Eligible" -- repeating the word
                    in every row's value doubled it ("0 eligible" under
                    "ELIGIBLE"). The other columns show a bare value the same
                    way (RISK: "High", not "Risk: High"). */}
                <span>{eligible}</span>
              </button>
            );
          })}
        </div>
      </div>
      <div className="feature-section review-operations-card review-recent-card">
        <div className="panel-header review-recent-header">
          <span>
            {t("overview.recentDecisions", {
              defaultValue: "Recent decisions",
            })}
          </span>
          <Link
            className="inline-action"
            to={`${workspacePath(routeWorkspaceId, "activity")}?kind=workspace&subtype=review`}
          >
            {t("actions.moreActivities", { defaultValue: "View all activity" })}
          </Link>
        </div>
        {recentDecisions.length === 0 ? (
          <div className="empty-state empty-state-rich">
            <h2>
              {t("overview.noRecentDecisionsTitle", {
                defaultValue: "No decisions yet",
              })}
            </h2>
            <p>
              {t("overview.noRecentDecisionsBody", {
                defaultValue:
                  "Approve, reject, or request changes and the decisions appear here.",
              })}
            </p>
          </div>
        ) : (
          <div className="history-list">
            {recentDecisions.map((decision) => (
              <Link
                className="history-row review-recent-decision-link"
                key={decision.decision_id}
                to={workspacePath(routeWorkspaceId, `review/${encodeURIComponent(decision.review_item_id)}`)}
              >
                <div>
                  <strong>
                    {recentDecisionTargetTitle(decision, t)}
                  </strong>
                  <p>
                    {decision.actor_display_name ??
                      t("overview.unknownActor", { defaultValue: "Unknown" })}
                    {" · "}
                    {t(`overview.decisionType.${decision.decision_type}`, {
                      defaultValue: decision.decision_type,
                    })}
                    {decision.decision_reason
                      ? ` · ${decision.decision_reason}`
                      : ""}
                  </p>
                </div>
                <time
                  className="meta-line"
                  dateTime={decision.created_at}
                  title={formatVersionTimestamp(decision.created_at, "long")}
                >
                  {formatRelativeTimestamp(decision.created_at, i18n.language)}
                </time>
              </Link>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function ReviewRow({
  review,
  active,
  bulkDisabled,
  bulkSelectable,
  bulkSelected,
  onSelect,
  onToggleBulkSelected,
  showStatusPill,
}: {
  review: ReviewItem;
  active: boolean;
  bulkDisabled: boolean;
  bulkSelectable: boolean;
  bulkSelected: boolean;
  onSelect: (reviewItemId: string) => void;
  onToggleBulkSelected: (reviewItemId: string, selected: boolean) => void;
  showStatusPill: boolean;
}) {
  const routeWorkspaceId = useRouteWorkspaceId();
  const { t } = useTranslation("review");
  // The rail sorts and groups by activity (resolution date for resolved
  // items), so the visible timestamp must follow the same axis or rows look
  // misfiled under their date divider.
  const activityAt = reviewActivityAt(review);
  const target = reviewPrimaryOperation(review);
  const rawTargetTitle = reviewTargetTitle(review, t);
  const targetTitle = rawTargetTitle;
  const targetVersionNo = reviewTargetVersionNo(review);
  const reviewType = domainLabel("review", "reviewType", review.review_type, t);
  const decisionSummary = localizedDecisionSummary(review, t);
  const secondarySummary =
    decisionSummary && decisionSummary !== rawTargetTitle
      ? decisionSummary
      : null;
  const isReconciliation = review.review_type === "context_reconciliation";
  const reconciliationChatHref =
    isReconciliation && review.reconciliation_chat_session_id
      ? workspacePath(routeWorkspaceId, `chats/${encodeURIComponent(review.reconciliation_chat_session_id)}`)
      : null;
  return (
    <div
      className={`review-row-shell${bulkSelectable ? " has-bulk-select" : ""}`}
    >
      {bulkSelectable ? (
        <label className="review-row-select">
          <input
            aria-label={t("bulk.selectReview", {
              title: targetTitle,
              defaultValue: "Select {{title}}",
            })}
            checked={bulkSelected}
            disabled={bulkDisabled}
            onChange={(event) =>
              onToggleBulkSelected(review.id, event.currentTarget.checked)
            }
            type="checkbox"
          />
        </label>
      ) : null}
      <button
        className={`review-row selectable-row${active ? " active" : ""}`}
        type="button"
        onClick={() => onSelect(review.id)}
      >
        <div className="review-row-main">
          <strong>{targetTitle}</strong>
          <p>
            <span className="review-row-type">{reviewType}</span>
            {secondarySummary ? <span>{secondarySummary}</span> : null}
          </p>
          {target ? (
            <span className="review-row-target-meta">
              <FileText size={13} aria-hidden="true" />
              <span>{humanizeEnum(target.target_type)}</span>
              {target.target_slug ? <code>{target.target_slug}</code> : null}
            </span>
          ) : null}
          {isReconciliation ? <ReconciliationProvenanceMeta review={review} /> : null}
        </div>
        <div className="review-row-meta">
          <time
            className="meta-line"
            dateTime={activityAt}
            title={formatVersionTimestamp(activityAt, "long")}
          >
            {formatVersionTimestamp(activityAt)}
          </time>
          <span className="review-row-badges">
            {targetVersionNo != null ? (
              <span className="review-version-badge">v{targetVersionNo}</span>
            ) : null}
            <StatusPill status={review.risk_level} compact />
            {showStatusPill ? (
              <StatusPill
                status={review.status}
                label={statusPillLabel(review.status, t)}
                compact
              />
            ) : null}
            {review.current_audience_kind ? (
              <StatusPill
                status={audienceChipLabel(review.current_audience_kind, t)}
                compact
              />
            ) : null}
            {review.claimed_by ? (
              <StatusPill
                status={t("audience.claimed", { defaultValue: "Claimed" })}
                compact
              />
            ) : null}
          </span>
        </div>
      </button>
      {reconciliationChatHref ? (
        <Link
          aria-label={t("detail.openReconciliationConversationFor", {
            defaultValue: "Open conversation for {{title}}",
            title: targetTitle,
          })}
          className="review-reconciliation-chat-link text-button"
          to={reconciliationChatHref}
        >
          {t("detail.openReconciliationConversation", {
            defaultValue: "Open source conversation",
          })}
        </Link>
      ) : null}
    </div>
  );
}

function ReconciliationProvenanceMeta({ review }: { review: ReviewItem }) {
  const { t } = useTranslation("review");
  const actorKind = review.reconciliation_branch_actor_kind;
  const actorName = review.reconciliation_branch_actor_display_name;
  const hasMetadata =
    review.reconciliation_base_version_no != null
    || review.reconciliation_head_advance_count != null
    || review.reconciliation_conflict_count != null
    || Boolean(actorKind);
  if (!hasMetadata) {
    return null;
  }
  return (
    <span className="review-reconciliation-meta">
      {review.reconciliation_base_version_no != null ? (
        <span>
          {t("detail.reconciliationBase", {
            defaultValue: "Base v{{version}}",
            version: review.reconciliation_base_version_no,
          })}
        </span>
      ) : null}
      {review.reconciliation_head_advance_count != null ? (
        <span>
          {t("detail.reconciliationHeadAdvances", {
            count: review.reconciliation_head_advance_count,
            defaultValue: "HEAD advanced {{count}} times",
          })}
        </span>
      ) : null}
      {review.reconciliation_conflict_count != null ? (
        <span>
          {t("detail.reconciliationConflicts", {
            count: review.reconciliation_conflict_count,
            defaultValue: "{{count}} conflicts",
          })}
        </span>
      ) : null}
      {actorKind ? (
        <span className="review-reconciliation-actor" data-actor-kind={actorKind}>
          {actorName
            ? t("detail.reconciliationActorWithName", {
                defaultValue: "Author: {{kind}} · {{name}}",
                kind: actorKind,
                name: actorName,
              })
            : t("detail.reconciliationActorKind", {
                defaultValue: "Author: {{kind}}",
                kind: actorKind,
              })}
        </span>
      ) : null}
    </span>
  );
}

function ReviewDetail({
  review,
  decisionReason,
  busy,
  canEditDirectly,
  onReasonChange,
  onDecision,
  onEditDirectly,
  onRouteChanges,
}: {
  review: ReviewItem;
  decisionReason: string;
  busy: boolean;
  canEditDirectly: boolean;
  onReasonChange: (reason: string) => void;
  onDecision: (decision: ReviewDecisionType) => void;
  onEditDirectly: () => void;
  onRouteChanges: (
    audienceKind: ReviewAudienceKind,
    reason: string,
  ) => Promise<void>;
}) {
  const routeWorkspaceId = useRouteWorkspaceId();
  const isResolved = lifecycleTabForStatus(review.status) === "resolved";
  const isReconciliation = review.review_type === "context_reconciliation";
  const reconciliationHref =
    review.reconciliation_available === true
    && review.reconciliation_page_id
    && review.reconciliation_version_id
      ? `${workspacePath(routeWorkspaceId, `context/pages/${encodeURIComponent(review.reconciliation_page_id)}`)}?${new URLSearchParams({
          tab: "history",
          reconcile: review.reconciliation_version_id,
        }).toString()}`
      : null;
  const reconciliationChatHref =
    isReconciliation && review.reconciliation_chat_session_id
      ? workspacePath(routeWorkspaceId, `chats/${encodeURIComponent(review.reconciliation_chat_session_id)}`)
      : null;
  const hasReconciliationProvenance =
    review.reconciliation_base_version_no != null
    || review.reconciliation_head_advance_count != null
    || review.reconciliation_conflict_count != null
    || Boolean(review.reconciliation_branch_actor_kind);
  const canApprove = !isResolved && review.can_decide && !busy;
  const canRequestChanges = !isResolved && review.can_decide && !busy;
  const canReject = !isResolved && review.can_decide && !busy;
  const canEditDirectlyAction =
    canEditDirectly && review.status === "changes_requested" && review.can_claim;
  const { t } = useTranslation("review");
  const decisionReasonId = useId();
  const decisionReasonHintId = useId();
  const routingTitleId = useId();
  const rejectTitleId = useId();
  const reasonTextareaRef = useRef<HTMLTextAreaElement>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [routingOpen, setRoutingOpen] = useState(false);
  const [routingAudience, setRoutingAudience] =
    useState<ReviewAudienceKind>("requester");
  const [routingReason, setRoutingReason] = useState("");
  const [routingError, setRoutingError] = useState<string | null>(null);
  // The flag is optional until the backend ships it; treat undefined as
  // available and let the submit-time 422 guard catch a stale-true case.
  const chatOwnerAvailable = review.chat_owner_available !== false;
  const decisionReasonHint = t("detail.reasonHint", {
    defaultValue: "Required for Request changes. Optional for Approve and Reject.",
  });
  const targetTitle = reviewTargetTitle(review, t);
  const decisionSummary = localizedDecisionSummary(review, t);
  const riskInsight = reviewRiskInsight(review);

  function handleReject() {
    if (!review.can_decide || busy) {
      return;
    }
    // Reject is terminal (it removes the item from the queue), so gate it behind
    // an explicit confirm Dialog instead of submitting on the first click. The
    // decision reason stays optional — only the confirm is required.
    setRejectOpen(true);
  }

  // Stable identities: Dialog's focus-management effect keys on onClose, so an
  // inline arrow (new each render) would re-run it on every keystroke and steal
  // focus from the dialog's reason field. useCallback keeps focus put.
  const closeRouting = useCallback(() => {
    if (!busy) {
      setRoutingOpen(false);
    }
  }, [busy]);
  const closeReject = useCallback(() => {
    if (!busy) {
      setRejectOpen(false);
    }
  }, [busy]);

  async function confirmReject() {
    // The busy re-entry guard mirrors the routing dialog: the inline button is
    // disabled while submitting, but a stale double-submit is still bounced.
    if (busy) {
      return;
    }
    await onDecision("reject");
    setRejectOpen(false);
  }

  function openRoutingDialog() {
    setRoutingError(null);
    // Seed the dialog's reason from any reason already typed in the detail.
    setRoutingReason(decisionReason);
    // Default the radio to a resolvable option.
    setRoutingAudience(chatOwnerAvailable ? "chat_owner" : "requester");
    setRoutingOpen(true);
  }

  async function submitRouting() {
    if (busy) {
      return;
    }
    setRoutingError(null);
    // Decision reason is REQUIRED for Request changes (optional for Approve/Reject).
    if (routingReason.trim().length === 0) {
      setRoutingError(
        t("routing.reasonRequired", {
          defaultValue: "Enter a decision reason before requesting changes.",
        }),
      );
      return;
    }
    try {
      await onRouteChanges(routingAudience, routingReason);
      setRoutingOpen(false);
    } catch (exc) {
      // Defense-in-depth: surface a chat_owner_unresolvable 422 inline and keep
      // the dialog open so the reviewer can pick another audience. Read the
      // canonical error_code from ApiError; all other failures use authored
      // route copy so request/debug details cannot leak into the dialog.
      setRoutingError(
        isChatOwnerUnresolvable(exc)
          ? t("routing.chatOwnerUnresolvable", {
              defaultValue:
                "This change has no resolvable chat owner — route to you or all editors instead.",
            })
          : reviewErrorMessage(exc, t("errors.decide")),
      );
    }
  }

  return (
    <section
      aria-labelledby="review-detail-title"
      className="feature-section review-detail-card"
    >
      <header className="review-detail-header">
        <div className="review-detail-heading">
          <p className="eyebrow">
            {t("detail.eyebrow", { defaultValue: "Review detail" })} /{" "}
            {domainLabel("review", "reviewType", review.review_type, t)}
          </p>
          <h2 id="review-detail-title">{targetTitle}</h2>
          {/* Risk and status describe the review, so they belong with its
              metadata. Keeping them in the action cluster made that cluster
              wide enough that a long title pushed the decision buttons off
              the heading row entirely. */}
          <div className="review-detail-badges">
            <StatusPill status={review.risk_level} compact />
            <StatusPill
              status={review.status}
              label={statusPillLabel(review.status, t)}
              compact
            />
          </div>
          <div className="meta-line">
            {t("detail.changeSet", { defaultValue: "Change set" })}{" "}
            <ChangeSetValue changeSetId={review.change_set_id} /> /{" "}
            {t("detail.updated", { defaultValue: "Updated" })}{" "}
            {formatVersionTimestamp(review.updated_at)}
          </div>
        </div>
        {/* One page-level action cluster in the heading, like every other
            detail screen: decisions only. */}
        {!isResolved && !isReconciliation ? (
          <div
            className="detail-heading-actions review-action-bar"
            aria-label={t("detail.actionsAriaLabel", {
              defaultValue: "Review actions",
            })}
          >
          <button
            className="icon-button primary"
            disabled={!canApprove}
            onClick={() => onDecision("approve")}
            title={
              busy
                ? t("detail.actionBusy", {
                    defaultValue: "Review action is already being submitted.",
                  })
                : undefined
            }
            type="button"
          >
            <Check size={15} aria-hidden="true" />
            <span>{t("actions.approve", { defaultValue: "Approve" })}</span>
          </button>
          <button
            className="icon-button warning"
            disabled={!canRequestChanges}
            onClick={openRoutingDialog}
            title={
              busy
                ? t("detail.actionBusy", {
                    defaultValue: "Review action is already being submitted.",
                  })
                : undefined
            }
            type="button"
          >
            <RotateCcw size={15} aria-hidden="true" />
            <span>
              {t("actions.requestChanges", { defaultValue: "Request changes" })}
            </span>
          </button>
          {canEditDirectlyAction ? (
            <button
              className="icon-button"
              disabled={!review.can_decide || busy}
              onClick={onEditDirectly}
              title={
                busy
                  ? t("editDirectly.claiming", { defaultValue: "Claiming" })
                  : undefined
              }
              type="button"
            >
              <Pencil size={15} aria-hidden="true" />
              <span>{t("editDirectly.action", { defaultValue: "Edit directly" })}</span>
            </button>
          ) : null}
          <button
            className="icon-button danger"
            disabled={!canReject}
            onClick={handleReject}
            title={
              busy
                ? t("detail.actionBusy", {
                    defaultValue: "Review action is already being submitted.",
                  })
                : undefined
            }
            type="button"
          >
            <X size={15} aria-hidden="true" />
            <span>{t("actions.reject", { defaultValue: "Reject" })}</span>
          </button>
          </div>
        ) : null}
      </header>
      {!isResolved && isReconciliation ? (
        <StatusBanner
          tone="warning"
          action={
            reconciliationHref ? (
              <Link className="text-button" to={reconciliationHref}>
                {t("detail.openReconciliation", {
                  defaultValue: "Open reconciliation",
                })}
              </Link>
            ) : null
          }
        >
          {t("detail.reconciliationActionRequired", {
            defaultValue:
              "This branch stays pending until it is reconciled or explicitly discarded. Generic review decisions are unavailable.",
          })}
        </StatusBanner>
      ) : null}
      {isReconciliation && (hasReconciliationProvenance || reconciliationChatHref) ? (
        <section className="review-reconciliation-provenance">
          <h3>
            {t("detail.reconciliationProvenance", {
              defaultValue: "Reconciliation provenance",
            })}
          </h3>
          <ReconciliationProvenanceMeta review={review} />
          {reconciliationChatHref ? (
            <Link className="text-button" to={reconciliationChatHref}>
              {t("detail.openReconciliationConversation", {
                defaultValue: "Open source conversation",
              })}
            </Link>
          ) : null}
        </section>
      ) : null}
      {!isResolved && !isReconciliation && review.superseded_review_count > 0 ? (
        <StatusBanner tone="warning">
          {t("detail.approvalImpact", {
            count: review.superseded_review_count,
            defaultValue:
              "Approving will publish this version. {{count}} earlier pending reviews will be settled with it.",
          })}
        </StatusBanner>
      ) : null}
      <div className="detail-grid">
        {review.evidence ? (
          <>
            <DetailMetric
              label={t("detail.trigger", { defaultValue: "Trigger" })}
              value={domainLabel("review", "triggerType", review.evidence.trigger_type, t)}
            />
            <DetailMetric
              label={t("detail.citationCheck", { defaultValue: "Citation check" })}
              value={
                <span
                  className={
                    review.evidence.cite_call_skipped
                      ? "status-fail"
                      : "status-pass"
                  }
                >
                  {review.evidence.cite_call_skipped
                    ? t("detail.skipped", { defaultValue: "Skipped" })
                    : t("detail.passed", { defaultValue: "Passed" })}
                </span>
              }
            />
          </>
        ) : null}
      </div>
      <div className="evidence-box review-risk-explanation">
        <strong>{t("detail.whyRisk", { defaultValue: "Why this risk?" })}</strong>
        <p>{riskInsight.summary}</p>
        <div className="review-risk-meta">
          <StatusPill status={humanizeEnum(riskInsight.preset)} compact />
          {riskInsight.version ? (
            <span className="meta-line">
              {t("detail.policyVersion", { defaultValue: "Policy" })}:{" "}
              {riskInsight.version}
            </span>
          ) : null}
        </div>
        {riskInsight.reasonCodes.length > 0 ? (
          <div className="review-risk-reasons">
            {riskInsight.reasonCodes.map((reason) => (
              <span className="code-chip" key={reason}>
                {reason}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      {!isResolved && !isReconciliation ? (
        <div className="review-decision-block">
          <FormField
            className="review-reason-field"
            hint={decisionReasonHint}
            hintId={decisionReasonHintId}
            htmlFor={decisionReasonId}
            label={t("detail.reasonLabel", { defaultValue: "Decision reason" })}
          >
          {/* No error= here: the reason is optional for Approve and Reject and
              required only for Request changes (enforced in the routing dialog),
              so this inline field never shows a "required" alert. */}
            <textarea
              {...textInputLimit(2000, false)}
              aria-describedby={decisionReasonHintId}
              id={decisionReasonId}
              placeholder={t("detail.reasonPlaceholder", {
                defaultValue: "Add context for this decision.",
              })}
              ref={reasonTextareaRef}
              value={decisionReason}
              onChange={(event) => onReasonChange(event.currentTarget.value)}
            />
          </FormField>
        </div>
      ) : null}
      {decisionSummary ? (
        <div className="evidence-box">
          <strong>{t("detail.summary", { defaultValue: "Summary" })}</strong>
          <p>{decisionSummary}</p>
        </div>
      ) : null}
      {review.evidence ? <ReviewEvidence review={review} /> : null}
      <div className="evidence-box review-history">
        <strong>
          {t("detail.decisionHistory", { defaultValue: "Decision history" })}
        </strong>
        <div className="history-list">
          <div className="history-row">
            <div>
              <strong>{t("detail.submitted", { defaultValue: "Submitted" })}</strong>
              <p>
                {t("detail.enteredQueue", {
                  defaultValue: "Entered the decision queue",
                })}
              </p>
            </div>
            <time
              className="meta-line"
              dateTime={review.created_at}
              title={formatVersionTimestamp(review.created_at, "long")}
            >
              {formatVersionTimestamp(review.created_at)}
            </time>
          </div>
          {/* Full ledger oldest→newest (decisions arrive newest-first). */}
          {[...review.decisions].reverse().map((decision) => (
            <div className="history-row" key={decision.decision_id}>
              <div>
                <strong>{domainLabel("review", "decisionType", decision.decision_type, t)}</strong>
                <p>
                  {decision.actor_display_name
                    ?? domainLabel("review", "actorType", decision.actor_type, t)}
                  {decision.decision_reason ? ` - ${decision.decision_reason}` : ""}
                  {` -> ${humanizeEnum(decision.resulting_review_status)}`}
                  {decision.resulting_change_set_status
                    ? ` / ${domainLabel("review", "changeSetStatus", decision.resulting_change_set_status, t)}`
                    : ""}
                </p>
              </div>
              <time
                className="meta-line"
                dateTime={decision.created_at}
                title={formatVersionTimestamp(decision.created_at, "long")}
              >
                {formatVersionTimestamp(decision.created_at)}
              </time>
            </div>
          ))}
        </div>
      </div>
      {!isResolved && rejectOpen ? (
        <Dialog
          className="review-reject-dialog"
          dismissible={!busy}
          labelledBy={rejectTitleId}
          onClose={closeReject}
        >
          <div className="dialog-form">
            <h2 id={rejectTitleId}>
              {t("reject.confirmTitle", { defaultValue: "Reject this review?" })}
            </h2>
            <p>
              {t("reject.confirmBody", {
                defaultValue:
                  "Rejecting is final and removes the item from the queue.",
              })}
            </p>
            <div className="dialog-actions">
              <button
                className="icon-button"
                disabled={busy}
                onClick={closeReject}
                type="button"
              >
                {t("reject.cancel", { defaultValue: "Cancel" })}
              </button>
              <button
                className="icon-button danger"
                disabled={busy}
                onClick={() => void confirmReject()}
                type="button"
              >
                {busy
                  ? t("reject.confirming", { defaultValue: "Rejecting" })
                  : t("actions.reject", { defaultValue: "Reject" })}
              </button>
            </div>
          </div>
        </Dialog>
      ) : null}
      {!isResolved && routingOpen ? (
        <Dialog
          className="review-routing-dialog"
          dismissible={!busy}
          labelledBy={routingTitleId}
          onClose={closeRouting}
        >
          <div className="dialog-form">
            <h2 id={routingTitleId}>
              {t("routing.title", { defaultValue: "Request changes" })}
            </h2>
            <p>
              {t("routing.intro", {
                defaultValue: "Choose who should act on these changes.",
              })}
            </p>
            <fieldset className="review-routing-options">
              <legend className="visually-hidden">
                {t("routing.legend", {
                  defaultValue: "Who should act on these changes",
                })}
              </legend>
              <RoutingRadio
                checked={routingAudience === "chat_owner"}
                disabled={!chatOwnerAvailable}
                hint={
                  chatOwnerAvailable
                    ? t("routing.optionHints.chat_owner", {
                        defaultValue:
                          "Routes to the chat owner and their AI for revision.",
                      })
                    : t("routing.chatOwnerUnavailable", {
                        defaultValue:
                          "This change has no resolvable chat owner.",
                      })
                }
                label={t("routing.options.chat_owner", {
                  defaultValue: "Chat owner",
                })}
                onSelect={() => setRoutingAudience("chat_owner")}
                value="chat_owner"
              />
              <RoutingRadio
                checked={routingAudience === "requester"}
                hint={t("routing.optionHints.requester", {
                  defaultValue: "Routes to you for direct revision.",
                })}
                label={t("routing.options.requester", { defaultValue: "Me" })}
                onSelect={() => setRoutingAudience("requester")}
                value="requester"
              />
              <RoutingRadio
                checked={routingAudience === "workspace_editors"}
                hint={t("routing.optionHints.workspace_editors", {
                  defaultValue: "Routes to all workspace editors.",
                })}
                label={t("routing.options.workspace_editors", {
                  defaultValue: "All editors",
                })}
                onSelect={() => setRoutingAudience("workspace_editors")}
                value="workspace_editors"
              />
            </fieldset>
            <FormField
              className="review-reason-field"
              hint={decisionReasonHint}
              htmlFor={`${routingTitleId}-reason`}
              label={t("detail.reasonLabel", { defaultValue: "Decision reason" })}
            >
              <textarea
              {...textInputLimit(2000, false)}
                id={`${routingTitleId}-reason`}
                placeholder={t("detail.reasonPlaceholder", {
                  defaultValue:
                "Add context for this decision. Required for Request changes.",
                })}
                value={routingReason}
                onChange={(event) => setRoutingReason(event.currentTarget.value)}
              />
            </FormField>
            {routingError ? (
              <StatusBanner tone="warning">{routingError}</StatusBanner>
            ) : null}
            <div className="dialog-actions">
              <button
                className="icon-button"
                disabled={busy}
                onClick={closeRouting}
                type="button"
              >
                {t("routing.cancel", { defaultValue: "Cancel" })}
              </button>
              <button
                className="icon-button primary"
                disabled={busy || routingReason.trim().length === 0}
                onClick={() => void submitRouting()}
                type="button"
              >
                {t("routing.submit", { defaultValue: "Request changes" })}
              </button>
            </div>
          </div>
        </Dialog>
      ) : null}
    </section>
  );
}

function RoutingRadio({
  checked,
  disabled,
  hint,
  label,
  onSelect,
  value,
}: {
  checked: boolean;
  disabled?: boolean;
  hint?: string;
  label: string;
  onSelect: () => void;
  value: ReviewAudienceKind;
}) {
  return (
    <label className={`review-routing-option${disabled ? " is-disabled" : ""}`}>
      <input
        aria-label={label}
        checked={checked}
        disabled={disabled}
        name="review-routing-audience"
        onChange={onSelect}
        type="radio"
        value={value}
      />
      <span className="review-routing-option-label">{label}</span>
      {hint ? <span className="review-routing-option-hint">{hint}</span> : null}
    </label>
  );
}

function ReviewEvidence({ review }: { review: ReviewItem }) {
  const { t } = useTranslation("review");
  const evidence = review.evidence;
  if (!evidence) {
    return null;
  }
  return (
    <div className="evidence-box">
      <strong>{t("evidence.title", { defaultValue: "Change evidence" })}</strong>
      <dl className="detail-list inline">
        <div>
          <dt>{t("evidence.changeStatus", { defaultValue: "Change status" })}</dt>
          <dd>{domainLabel("review", "changeSetStatus", evidence.change_set_status, t)}</dd>
        </div>
        <div>
          <dt>{t("evidence.reason", { defaultValue: "Reason" })}</dt>
          <dd>{evidence.reason ?? t("evidence.none", { defaultValue: "none" })}</dd>
        </div>
      </dl>
      <div className="history-list">
        {evidence.operations.map((operation) => (
          <ReviewOperation operation={operation} key={operation.operation_id} />
        ))}
      </div>
    </div>
  );
}

function ReviewOperation({
  operation,
}: {
  operation: ReviewEvidenceOperation;
}) {
  return (
    <div className="history-row review-operation-row">
      <div>
        <strong>{operation.target_title ?? humanizeEnum(operation.target_type)}</strong>
        <p>
          {domainLabel("review", "operation", operation.operation)}
          {operation.target_slug ? ` - ${operation.target_slug}` : ""}
          {operation.target_version_no ? ` - v${operation.target_version_no}` : ""}
        </p>
      </div>
      <span>
        {operation.diff_summary
          ? operation.diff_summary
          : operation.diff_ref ?? shortIdentifier(operation.target_id)}
      </span>
    </div>
  );
}

function DetailMetric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="metric-card">
      <div className="metric-card-title">
        <span>{label}</span>
      </div>
      <strong>{value}</strong>
    </div>
  );
}

function ChangeSetValue({ changeSetId }: { changeSetId: string }) {
  const { t } = useTranslation("review");
  const shortId = shortIdentifier(changeSetId);
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle");
  const copied = copyStatus === "copied";
  const copyFailed = copyStatus === "failed";
  const copyLabel = copied
    ? t("detail.changeSetIdCopied", { defaultValue: "Change Set ID copied" })
    : copyFailed
      ? t("detail.changeSetIdCopyFailed", {
          defaultValue: "Copy Change Set ID failed",
        })
      : t("detail.copyFullChangeSetId", {
          defaultValue: "Copy full Change Set ID",
        });

  async function copyFullId() {
    if (!navigator.clipboard?.writeText) {
      setCopyStatus("failed");
      return;
    }
    try {
      await navigator.clipboard.writeText(changeSetId);
      setCopyStatus("copied");
      window.setTimeout(() => setCopyStatus("idle"), 1600);
    } catch {
      setCopyStatus("failed");
    }
  }

  return (
    <span className="copyable-id">
      <code className="uuid" title={changeSetId}>
        {shortId}
      </code>
      <button
        aria-label={copyLabel}
        className="icon-button compact-icon-button"
        onClick={() => void copyFullId()}
        title={copyLabel}
        type="button"
      >
        {copied ? (
          <Check size={12} aria-hidden="true" />
        ) : (
          <Copy size={12} aria-hidden="true" />
        )}
      </button>
    </span>
  );
}

function localizedDecisionSummary(
  item: DecisionSummaryCarrier,
  t: TFunction,
): string | null {
  const code = item.decision_summary_code;
  if (
    code
    && Object.prototype.hasOwnProperty.call(REVIEW_DECISION_SUMMARY_DEFAULTS, code)
  ) {
    const knownCode = code as keyof typeof REVIEW_DECISION_SUMMARY_DEFAULTS;
    return t(`decisionSummary.${knownCode}`, {
      defaultValue: REVIEW_DECISION_SUMMARY_DEFAULTS[knownCode],
    });
  }
  // Legacy rows, caller-authored free text, and future server codes unknown to
  // this client keep the stored prose instead of exposing an i18n key.
  return item.decision_summary ?? null;
}

function recentDecisionTargetTitle(
  decision: ReviewDecisionRecentItem,
  t: TFunction,
): string {
  return (
    decision.target_title
    ?? decision.review_target_title
    ?? localizedDecisionSummary(decision, t)
    ?? t("overview.untitledTarget", { defaultValue: "Untitled review" })
  );
}

function reviewTargetTitle(review: ReviewItem, t: TFunction) {
  const operationTitle = reviewPrimaryOperation(review)?.target_title;
  return (
    operationTitle
    ?? localizedDecisionSummary(review, t)
    ?? domainLabel("review", "reviewType", review.review_type)
  );
}

function reviewTargetVersionNo(review: ReviewItem) {
  return reviewPrimaryOperation(review)?.target_version_no ?? null;
}

function reviewPrimaryOperation(review: ReviewItem): ReviewEvidenceOperation | null {
  const operations = review.evidence?.operations ?? [];
  return (
    operations.find(
      (operation) =>
        operation.target_type === "context_page" && operation.target_id,
    )
    ?? operations.find((operation) => operation.target_title)
    ?? operations[0]
    ?? null
  );
}

// Editor+ may edit a change directly. Role is read from the current workspace
// summary; the server hard-gates the claim regardless of this affordance.
async function resolveCanEditDirectly(workspaceId: string): Promise<boolean> {
  try {
    const response = await listWorkspaces();
    const workspace = response.data.workspaces.find(
      (candidate) => candidate.id === workspaceId,
    );
    const role = workspace?.role?.toLowerCase() ?? "";
    return EDITOR_ROLES.has(role);
  } catch {
    return false;
  }
}

async function loadReviewPolicySummary(
  workspaceId: string,
): Promise<ReviewRiskPolicySummary> {
  try {
    const policies = await getSettingsPolicies(workspaceId);
    return reviewPolicySummary(policies.review?.policy);
  } catch {
    return DEFAULT_REVIEW_POLICY_SUMMARY;
  }
}

function reviewPolicySummary(
  policy: Record<string, unknown> | undefined,
): ReviewRiskPolicySummary {
  const automation = asRecord(policy?.automation);
  const byRisk = asRecord(automation.auto_approve_by_risk);
  return {
    autoApproveByRisk: (["low", "medium", "high"] as const).map((risk) => {
      const row = asRecord(byRisk[risk]);
      const rawAllowed = row.allowed !== undefined ? row.allowed : row.enabled;
      return {
        risk,
        allowed: risk !== "high" && rawAllowed === true,
        delayDays:
          typeof row.delay_days === "number" && Number.isFinite(row.delay_days)
             ? Math.min(30, Math.max(0, Math.floor(row.delay_days)))
             : risk === "low" ? 7 : risk === "medium" ? 14 : 28,
      };
    }),
    preset: typeof policy?.preset === "string" ? policy.preset : "balanced",
  };
}

function reviewRiskInsight(review: ReviewItem) {
  const snapshot = asRecord(review.risk_policy_snapshot);
  const signals = asRecord(review.risk_signals);
  const reasonCodes = Array.isArray(signals.reason_codes)
    ? signals.reason_codes.map(String)
    : [];
  return {
    summary:
      review.risk_resolution_summary ??
      `This item is classified as ${humanizeEnum(review.risk_level)} risk by the active review policy.`,
    preset: typeof snapshot.preset === "string" ? snapshot.preset : "legacy policy",
    version: review.risk_policy_version ?? stringOrNull(snapshot.policy_version),
    reasonCodes,
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return value as Record<string, unknown>;
}

function stringOrNull(value: unknown) {
  return typeof value === "string" && value ? value : null;
}

function audienceChipLabel(
  kind: ReviewAudienceKind,
  t: (key: string, options: { defaultValue: string }) => string,
) {
  if (kind === "requester") {
    return t("audience.requester", { defaultValue: "To requester" });
  }
  if (kind === "chat_owner") {
    return t("audience.chat_owner", { defaultValue: "To chat owner" });
  }
  return t("audience.workspace_editors", { defaultValue: "To editors" });
}

// Most review statuses read fine via StatusPill's snake_case-to-space default,
// but "changes_requested" needs the authored/translated copy ("Changes
// requested" / "변경 요청"); return undefined elsewhere so the pill keeps its
// default label.
function statusPillLabel(
  status: string,
  t: (key: string, options: { defaultValue: string }) => string,
): string | undefined {
  if (status === "changes_requested") {
    return t("status.changes_requested", { defaultValue: "Changes requested" });
  }
  return undefined;
}

// The backend signals an unroutable chat-owner request via the canonical
// error_code on its 422 envelope; fall back to message text for safety.
function isChatOwnerUnresolvable(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.body.error_code === "chat_owner_unresolvable";
  }
  return error instanceof Error && /chat_owner_unresolvable/i.test(error.message);
}

function reviewErrorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.displayMessage : fallback;
}

type ReviewLocale = "en" | "ko";

function normalizeReviewLocale(language: string | null | undefined): ReviewLocale {
  return language === "ko" ? "ko" : "en";
}

function deferredReconciliationCount(page: unknown) {
  const count = asRecord(page).deferred_reconciliation_count;
  return typeof count === "number" && Number.isSafeInteger(count) && count >= 0
    ? count
    : 0;
}

function lifecycleTabForStatus(status: string): ReviewLifecycleTab {
  if (status === "pending" || status === "changes_requested") {
    return status;
  }
  return "resolved";
}

function reviewActivityAt(review: ReviewItem) {
  if (review.status === "approved" || review.status === "rejected") {
    return review.updated_at;
  }
  return review.created_at;
}

function sortReviewsByActivity(reviews: ReviewItem[]) {
  return [...reviews].sort((left, right) => {
    const rightTime = new Date(reviewActivityAt(right)).getTime();
    const leftTime = new Date(reviewActivityAt(left)).getTime();
    if (rightTime !== leftTime) {
      return rightTime - leftTime;
    }
    return right.id.localeCompare(left.id);
  });
}

type ReviewRailGroup = {
  key: string;
  activityAt: string;
  reviews: ReviewItem[];
  pageTitle: string | null;
};

function pendingPageGroupKey(review: ReviewItem) {
  if (review.review_type !== "context_update") {
    return null;
  }
  const operation = reviewPrimaryOperation(review);
  return operation?.target_type === "context_page" && operation.target_id
    ? `context-page:${operation.target_id}`
    : null;
}

function pageGroupLabel(
  t: TFunction,
  group: ReviewRailGroup,
  complete: boolean,
): string {
  return complete
    ? t("rail.pageDraftGroupTotal", {
        title: group.pageTitle,
        count: group.reviews.length,
        defaultValue: "{{title}} · {{count}} pending drafts",
      })
    : t("rail.pageDraftGroup", {
        title: group.pageTitle,
        count: group.reviews.length,
        defaultValue: "{{title}} · {{count}} pending drafts shown",
      });
}

function groupPendingReviewsByPage(
  reviews: ReviewItem[],
  t: TFunction,
): ReviewRailGroup[] {
  const groups: ReviewRailGroup[] = [];
  const pageGroups = new Map<string, ReviewRailGroup>();
  for (const review of sortReviewsByActivity(reviews)) {
    const pageKey = pendingPageGroupKey(review);
    if (!pageKey) {
      groups.push({
        key: `review:${review.id}`,
        activityAt: reviewActivityAt(review),
        reviews: [review],
        pageTitle: null,
      });
      continue;
    }
    const existing = pageGroups.get(pageKey);
    if (existing) {
      existing.reviews.push(review);
      continue;
    }
    const group = {
      key: pageKey,
      activityAt: reviewActivityAt(review),
      reviews: [review],
      pageTitle: reviewTargetTitle(review, t),
    };
    pageGroups.set(pageKey, group);
    groups.push(group);
  }
  for (const group of groups) {
    if (group.reviews.length < 2) {
      continue;
    }
    group.reviews.sort((left, right) => {
      const leftVersion = reviewTargetVersionNo(left);
      const rightVersion = reviewTargetVersionNo(right);
      if (leftVersion != null || rightVersion != null) {
        if (leftVersion == null) return 1;
        if (rightVersion == null) return -1;
        if (leftVersion !== rightVersion) return rightVersion - leftVersion;
      }
      const activityDifference =
        new Date(reviewActivityAt(right)).getTime()
        - new Date(reviewActivityAt(left)).getTime();
      return activityDifference || right.id.localeCompare(left.id);
    });
  }
  return groups;
}

function groupReviewsByDate(reviews: ReviewItem[]) {
  const groups: ReviewRailGroup[] = [];
  for (const review of sortReviewsByActivity(reviews)) {
    const activityAt = reviewActivityAt(review);
    const dateKey = reviewDateKey(activityAt);
    const current = groups.at(-1);
    if (current?.key === `date:${dateKey}`) {
      current.reviews.push(review);
      continue;
    }
    groups.push({
      key: `date:${dateKey}`,
      activityAt,
      reviews: [review],
      pageTitle: null,
    });
  }
  return groups;
}

function bulkSettlementCount(
  reviews: ReviewItem[],
  previewApprovedIds: Set<string>,
) {
  const countByPage = new Map<string, number>();
  for (const review of reviews) {
    if (!previewApprovedIds.has(review.id)) {
      continue;
    }
    const pageKey = pendingPageGroupKey(review);
    const count = Math.max(0, review.superseded_review_count ?? 0);
    if (pageKey && count > (countByPage.get(pageKey) ?? 0)) {
      countByPage.set(pageKey, count);
    }
  }
  return [...countByPage.values()].reduce((total, count) => total + count, 0);
}

function reviewDateKey(value: string) {
  const date = new Date(value);
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}

function reviewLocaleCode(locale: ReviewLocale) {
  return locale === "ko" ? "ko-KR" : "en-US";
}

function formatReviewDate(value: string, locale: ReviewLocale) {
  return new Intl.DateTimeFormat(reviewLocaleCode(locale), {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(value));
}

function defaultLifecycleLabel(tab: ReviewLifecycleTab) {
  if (tab === "pending") {
    return "Pending";
  }
  if (tab === "changes_requested") {
    return "Changes requested";
  }
  return "Resolved";
}

function shortIdentifier(value: string) {
  return value.length > 14 ? `${value.slice(0, 8)}...${value.slice(-4)}` : value;
}

import { inputTextError, textInputLimit } from "../../shared/lib/inputPolicy";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  Inbox,
  Pencil,
  RotateCcw,
  X,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import {
  claimReview,
  createReviewSelection,
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
  ReviewSelection,
} from "../../shared/api/operations";
import { getCurrentWorkspaceId, listWorkspaces } from "../../shared/api/workspace";
import { ApiError } from "../../shared/api/client";
import { useOptionalAuth } from "../../shared/auth/AuthContext";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
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
import { readReviewDraft, reviewDraftKey, writeReviewDraft } from "../../shared/lib/reviewDraftStorage";

import { ReviewEvidencePanel } from "./ReviewEvidencePanel";

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
  selection: ReviewSelection;
  settlementCount: number;
};

type PendingReviewDecision = {
  decisionId: string;
  audienceKind?: ReviewAudienceKind;
  workspaceId: string;
  reviewId: string;
  decision: ReviewDecisionType;
  reason: string | null;
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
  const { i18n, t } = useTranslation("review");
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const auth = useOptionalAuth();
  const workspaceContext = useOptionalWorkspaceContext();
  const [reasonDrafts, setReasonDrafts] = useState<Record<string, string>>({});
  const searchParamsRef = useRef(searchParams);
  useEffect(() => { searchParamsRef.current = searchParams; }, [searchParams]);
  const writeReviewQuery = useCallback((updates: Record<string, string | null>, replace = false) => {
    const next = new URLSearchParams(searchParamsRef.current);
    for (const [key, value] of Object.entries(updates)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    searchParamsRef.current = next;
    setSearchParams(next, { replace });
  }, [setSearchParams]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const loadMoreSentinelRef = useRef<HTMLDivElement | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const bulkOperationRef = useRef(0);
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
  const lifecycleTab = reviewFilterValue(searchParams.get("status"), LIFECYCLE_TABS, "pending");
  const riskFilter = reviewFilterValue(searchParams.get("risk"), RISK_FILTERS, "all");
  const resolvedStatusFilter = reviewFilterValue(searchParams.get("outcome"), RESOLVED_STATUS_FILTERS, "all");
  const scopeFilter = reviewFilterValue(searchParams.get("scope"), SCOPE_FILTERS, "all");
  const includeDeferredReconciliations = searchParams.get("older") === "1";
  const setLifecycleTab = useCallback((value: ReviewLifecycleTab) => writeReviewQuery({ status: value === "pending" ? null : value }), [writeReviewQuery]);
  const setRiskFilter = (value: ReviewRiskFilter) => writeReviewQuery({ risk: value === "all" ? null : value });
  const setScopeFilter = useCallback((value: ReviewScope) => writeReviewQuery({ scope: value === "all" ? null : value }), [writeReviewQuery]);
  const setResolvedStatusFilter = (value: ReviewResolvedStatusFilter) => writeReviewQuery({ outcome: value === "all" ? null : value });
  const setIncludeDeferredReconciliations = (value: boolean) => writeReviewQuery({ older: value ? "1" : null });
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(() => new Set());
  const [groupBusyKey, setGroupBusyKey] = useState<string | null>(null);
  const groupSelectionGenerationRef = useRef(0);
  const lastExpandedReviewRef = useRef<string | null>(null);
  const [groupSelectionIds, setGroupSelectionIds] = useState<Record<string, string[]>>({});
  const [groupSnapshotTotals, setGroupSnapshotTotals] = useState<Record<string, number>>({});
  const [settledReviewStates, setSettledReviewStates] = useState<Record<string, string>>({});
  const [completedReview, setCompletedReview] = useState<ReviewItem | null>(null);
  const [detachedReview, setDetachedReview] = useState<ReviewItem | null>(null);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [uncertainDecisions, setUncertainDecisions] = useState<Record<string, PendingReviewDecision | null>>({});
  const focusReviewRef = useRef<string | null>(null);
  const [nextReviewOrder, setNextReviewOrder] = useState<{ after: string[]; before: string[] }>({ after: [], before: [] });
  const reviewMainRef = useRef<HTMLElement | null>(null);
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
    workspaceContext?.selectedId,
  ]);
  const {
    data: state,
    error,
    setData: setState,
    setError,
  } = useAsyncLoad<ReviewState>(loadReviews, [
    activeReviewStatus,
    riskFilter,
    activeScopeFilter,
    includeDeferredReconciliations,
    workspaceContext?.selectedId,
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
  const workspaceIsCurrent = Boolean(state?.workspaceId)
    && (!workspaceContext || state?.workspaceId === workspaceContext.selectedId);
  const reviewParam = searchParams.get("review");
  const selectedDraftId = workspaceIsCurrent ? reviewParam : null;
  const draftKey = state?.workspaceId && selectedDraftId
    ? reviewDraftKey(auth?.user?.user_id ?? "preview", state.workspaceId, selectedDraftId)
    : null;
  const decisionReason = draftKey
    ? reasonDrafts[draftKey] ?? (auth?.user ? readReviewDraft(draftKey) : "")
    : "";
  const uncertainDecision = draftKey
    ? uncertainDecisions[draftKey] === undefined ? readPendingReviewDecision(draftKey) : uncertainDecisions[draftKey]
    : null;
  function setDecisionReason(value: string) {
    if (!draftKey) return;
    setReasonDrafts((current) => ({ ...current, [draftKey]: value }));
    if (auth?.user) writeReviewDraft(draftKey, value);
  }

  useEffect(() => {
    setCollapsedGroups(new Set());
    setGroupSelectionIds({});
    setGroupSnapshotTotals({});
    setSettledReviewStates({});
    bulkOperationRef.current += 1;
    setBulkBusy(false);
    groupSelectionGenerationRef.current += 1;
    lastExpandedReviewRef.current = null;
    setGroupBusyKey(null);
    setBulkDialog(null);
    setBulkResult(null);
    setCompletedReview(null);
    setDetachedReview(null);
    setRefreshFailed(false);
  }, [workspaceContext?.selectedId, activeReviewStatus, riskFilter, activeScopeFilter, includeDeferredReconciliations]);

  useEffect(() => {
    setBulkDiscardResult(null);
    setBulkDiscardOpen(false);
    setBulkDiscardReason("");
  }, [workspaceContext?.selectedId]);

  useEffect(() => {
    const normalized: Record<string, string | null> = {
      status: lifecycleTab === "pending" ? null : lifecycleTab,
      risk: riskFilter === "all" ? null : riskFilter,
      scope: lifecycleTab === "changes_requested" && scopeFilter !== "all" ? scopeFilter : null,
      outcome: lifecycleTab === "resolved" && resolvedStatusFilter !== "all" ? resolvedStatusFilter : null,
      older: lifecycleTab !== "resolved" && includeDeferredReconciliations ? "1" : null,
    };
    if (Object.entries(normalized).some(([key, value]) => searchParams.get(key) !== value)) {
      writeReviewQuery(normalized, true);
    }
  }, [lifecycleTab, riskFilter, scopeFilter, resolvedStatusFilter, includeDeferredReconciliations, searchParams, writeReviewQuery]);

  useEffect(() => {
    setSelectedBulkIds(new Set());
  }, [
    lifecycleTab,
    riskFilter,
    activeScopeFilter,
    includeDeferredReconciliations,
    workspaceContext?.selectedId,
  ]);

  // C5: the URL is the selection source of truth. Every parameter change gets
  // a fresh generation and aborts the request it superseded, so an older slow
  // detail response cannot overwrite a newer click.
  const reviewSelectionGenerationRef = useRef(0);
  useEffect(() => {
    if (!state?.workspaceId || !workspaceIsCurrent) {
      return;
    }
    reviewSelectionGenerationRef.current += 1;
    const generation = reviewSelectionGenerationRef.current;
    const controller = new AbortController();
    const isCurrent = () =>
      !controller.signal.aborted &&
      reviewSelectionGenerationRef.current === generation;

    if (reviewParam && completedReview?.id === reviewParam) {
      return () => controller.abort();
    }

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
      setDetachedReview(null);
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
        // A direct link can be outside this queue's filters. Keep it separate;
        // adding it to the filtered rail would misstate both its scope and count.
        setDetachedReview(review);
        setState((current) => {
          if (!current || current.workspaceId !== workspaceId || !isCurrent()) {
            return current;
          }
          return {
            ...current,
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
        setSearchParams(
          (current) => {
            const next = new URLSearchParams(current);
            if (next.get("review") === reviewParam) {
              next.delete("review");
            }
            return next;
          },
          { replace: true },
        );
        setError(
          reviewErrorMessage(exc, t("errors.restore")),
        );
      });

    return () => controller.abort();
  }, [
    lifecycleTab,
    completedReview,
    reviewParam,
    setError,
    setSearchParams,
    setState,
    state?.reviews,
    state?.workspaceId,
    workspaceIsCurrent,
    t,
  ]);

  // All selection commands update the parameter; the effect above owns state.
  function setReviewParam(reviewId: string | null) {
    focusReviewRef.current = reviewId;
    writeReviewQuery({ review: reviewId });
  }

  useEffect(() => {
    if (!reviewParam || focusReviewRef.current !== reviewParam || state?.selectedReviewId !== reviewParam) return;
    // Let the mobile drawer finish returning focus before moving it into the
    // selected review. The direct-link load does not steal initial focus.
    const frame = requestAnimationFrame(() => requestAnimationFrame(() => {
      const main = reviewMainRef.current;
      const title = main?.querySelector<HTMLElement>(`#review-detail-title`);
      if (title && searchParamsRef.current.get("review") === reviewParam) {
        // The user may have reached a reason field or another detail control
        // while the drawer's delayed focus is pending. Preserve that choice.
        if (!main?.contains(document.activeElement) || document.activeElement === title) {
          title.focus({ preventScroll: true });
        }
        focusReviewRef.current = null;
      }
    }));
    return () => cancelAnimationFrame(frame);
  }, [reviewParam, state?.selectedReviewId]);

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

  useEffect(() => {
    if (!reviewParam || lastExpandedReviewRef.current === reviewParam || !state) return;
    const review = state.reviews.find((item) => item.id === reviewParam);
    if (!review) return;
    const key = pendingPageGroupKey(review);
    if (key) setCollapsedGroups((current) => {
      if (!current.has(key)) return current;
      const next = new Set(current); next.delete(key); return next;
    });
    lastExpandedReviewRef.current = reviewParam;
  }, [reviewParam, state]);

  async function loadReviews(): Promise<ReviewState> {
    const currentWorkspaceId = workspaceContext?.selectedId || await getCurrentWorkspaceId();
    if (!currentWorkspaceId) {
      throw new Error("Workspace not found");
    }
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
    if (!workspaceIsCurrent || !state?.workspaceId || !state.nextCursor || loadingMore) {
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
        current && current.workspaceId === state.workspaceId && current.nextCursor === cursor
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

  // Persist the mutation receipt before fetching any derived queue state. A
  // failed GET must never turn a saved decision into a retryable decision POST.
  function setPendingDecision(key: string, pending: PendingReviewDecision | null) {
    setUncertainDecisions((current) => ({ ...current, [key]: pending }));
    if (auth?.user) writeReviewDraft(`${key}.pending`, pending ? JSON.stringify(pending) : "");
  }

  function storeDecisionReceipt(receipt: ReviewItem, key: string | null, generation: number) {
    if (key) {
      setReasonDrafts((current) => ({ ...current, [key]: "" }));
      writeReviewDraft(key, "");
      setPendingDecision(key, null);
    }
    if (loadMoreGenerationRef.current !== generation) return;
    setCompletedReview(receipt);
    setDetachedReview(null);
    setRefreshFailed(true);
    setState((current) => current && current.workspaceId === receipt.workspace_id ? {
      ...current, reviews: current.reviews.filter((review) => review.id !== receipt.id),
    } : current);
  }

  async function recoverDecision(pending: PendingReviewDecision, key: string, generation: number): Promise<boolean> {
    const fresh = await getReview(pending.workspaceId, pending.reviewId);
    if (generation !== loadMoreGenerationRef.current) return false;
    const receipt = fresh.decisions.some((item) => item.decision_id === pending.decisionId
      && item.decision_type === pending.decision
      && item.actor_id === auth?.user?.user_id
      && (item.decision_reason ?? null) === pending.reason);
    if (!receipt) {
      setRefreshFailed(true);
      setError(t("workflow.decisionOutcomeUnknown"));
      return false;
    }
    storeDecisionReceipt(fresh, key, generation);
    return true;
  }

  async function retryPendingDecision() {
    if (!uncertainDecision || !draftKey || busyId || bulkBusy || !workspaceIsCurrent) return;
    const generation = loadMoreGenerationRef.current;
    const pending = uncertainDecision;
    const key = draftKey;
    setBusyId(pending.reviewId);
    setError(null);
    try {
      // Read first. A retry uses the original ID even when the first POST is
      // still in flight, so the server serializes it into one decision receipt.
      if (!await recoverDecision(pending, key, generation)) {
        if (generation !== loadMoreGenerationRef.current) return;
        const receipt = await decideReview(pending.workspaceId, pending.reviewId,
          pending.decision, pending.reason, { decision_id: pending.decisionId,
            ...(pending.audienceKind ? { audience_kind: pending.audienceKind } : {}) });
        storeDecisionReceipt(receipt, key, generation);
      }
      if (generation !== loadMoreGenerationRef.current) return;
      setError(null);
      try { await refreshQueueAfterBulk(); }
      catch { if (generation === loadMoreGenerationRef.current) setError(t("workflow.savedRefreshFailed")); }
    } catch (exc) {
      if (generation === loadMoreGenerationRef.current) {
        if (exc instanceof ApiError && exc.status >= 400 && exc.status < 500) {
          setPendingDecision(key, null);
          setError(reviewErrorMessage(exc, t("errors.decide")));
          try { await refreshQueueAfterBulk(); } catch { /* Keep the stale-queue lock. */ }
        } else setError(t("workflow.decisionOutcomeUnknown"));
      }
    } finally { setBusyId(null); }
  }

  async function runDecision(
    decision: ReviewDecisionType,
    audienceKind?: ReviewAudienceKind,
    reasonOverride?: string,
  ): Promise<boolean> {
    const selected = reviewForCurrentParam();
    const reason = (reasonOverride ?? decisionReason).trim();
    const reasonError = inputTextError(reason, 2000, false);
    if (reasonError) throw new Error(reasonError);
    if (!state?.workspaceId || (workspaceContext?.selectedId && workspaceContext.selectedId !== state.workspaceId)
        || !selected || busyId || bulkBusy || refreshFailed || uncertainDecision
        || (decision === "request_changes" && !reason)) return false;
    const workspaceId = state.workspaceId;
    const generation = loadMoreGenerationRef.current;
    const order = (lifecycleTab === "pending"
      ? groupPendingReviewsByPage(state.reviews, t)
      : groupReviewsByDate(state.reviews)).flatMap((group) => group.reviews.map((review) => review.id));
    const at = order.indexOf(selected.id);
    setNextReviewOrder({ after: order.slice(at + 1), before: order.slice(0, Math.max(at, 0)) });
    const pending: PendingReviewDecision = { decisionId: crypto.randomUUID(), audienceKind, workspaceId, reviewId: selected.id, decision,
      reason: reason || null };
    const key = draftKey;
    setBusyId(selected.id);
    setError(null);
    try {
      let receipt: ReviewItem;
      try {
        // Save before POST so navigation/reload cannot enable a duplicate POST
        // when the response is lost after the server commits.
        if (key) setPendingDecision(key, pending);
        receipt = await decideReview(workspaceId, selected.id, decision, reason || null,
          { decision_id: pending.decisionId, ...(audienceKind ? { audience_kind: audienceKind } : {}) });
      } catch (exc) {
        if (exc instanceof ApiError && exc.status >= 400 && exc.status < 500) {
          if (key) setPendingDecision(key, null);
          throw exc;
        }
        if (generation !== loadMoreGenerationRef.current) return false;
        setRefreshFailed(true);
        setError(t("workflow.decisionOutcomeUnknown"));
        if (!key) return false;
        try {
          if (!await recoverDecision(pending, key, generation)) return false;
        } catch {
          return false;
        }
        try { await refreshQueueAfterBulk(); }
        catch {
          if (generation === loadMoreGenerationRef.current) setError(t("workflow.savedRefreshFailed"));
        }
        return true;
      }
      storeDecisionReceipt(receipt, key, generation);
      if (loadMoreGenerationRef.current !== generation) return true;
      try {
        await refreshQueueAfterBulk();
      } catch (exc) {
        if (generation === loadMoreGenerationRef.current) {
          setRefreshFailed(true);
          setError(reviewErrorMessage(exc, t("workflow.savedRefreshFailed")));
        }
      }
      return true;
    } finally {
      setBusyId(null);
    }
  }

  async function openNextReview() {
    if (!workspaceIsCurrent || !state?.workspaceId || refreshing || refreshFailed || busyId || bulkBusy) return;
    setRefreshing(true);
    const generation = loadMoreGenerationRef.current;
    try {
      let reviews = state.reviews;
      let cursor = state.nextCursor;
      const order = nextReviewOrder;
      let nextId = order.after.find((id) => reviews.some((review) => review.id === id));
      // Finish the following cursor pages before wrapping to earlier reviews.
      // A review arriving on the first refreshed page must not jump this order.
      const initialIds = new Set(reviews.map((review) => review.id));
      while (!nextId && cursor) {
        const page = await listReviews(state.workspaceId, { ...reviewListFilters(activeScopeFilter), cursor });
        if (generation !== loadMoreGenerationRef.current) return;
        const known = new Set(reviews.map((review) => review.id));
        reviews = [...reviews, ...page.items.filter((review) => !known.has(review.id))];
        cursor = page.next_cursor;
        nextId = order.after.find((id) => reviews.some((review) => review.id === id))
          ?? page.items.find((review) => !initialIds.has(review.id))?.id;
      }
      nextId ??= order.before.find((id) => reviews.some((review) => review.id === id));
      nextId ??= reviews[0]?.id;
      setState((current) => current?.workspaceId === state.workspaceId ? { ...current, reviews, nextCursor: cursor } : current);
      if (nextId) {
        setReviewParam(nextId);
        reviewMainRef.current?.scrollTo({ top: 0 });
      }
    } catch (exc) {
      if (generation === loadMoreGenerationRef.current) {
        setError(reviewErrorMessage(exc, t("errors.loadMore")));
      }
    } finally {
      setRefreshing(false);
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

  function knownGroupIds(groupKey: string, loaded?: ReviewItem[]) {
    const groupReviews = loaded ?? state?.reviews.filter(
      (review) => pendingPageGroupKey(review) === groupKey,
    ) ?? [];
    return [...new Set([
      ...(groupSelectionIds[groupKey] ?? []),
      ...groupReviews.map((review) => review.id),
    ])];
  }

  function settledReviewView(review: ReviewItem): ReviewItem {
    const status = settledReviewStates[review.id];
    return status ? { ...review, status, can_decide: false, can_claim: false } : review;
  }

  function reviewForCurrentParam(): ReviewItem | null {
    if (!workspaceIsCurrent || !state || !reviewParam) return null;
    const review = completedReview?.id === reviewParam
      ? completedReview
      : state.reviews.find((item) => item.id === reviewParam)
        ?? (detachedReview?.id === reviewParam ? detachedReview : null);
    return review?.workspace_id === state.workspaceId ? settledReviewView(review) : null;
  }

  function selectedDecisionCount() {
    if (!state || lifecycleTab === "resolved") return 0;
    return [...selectedBulkIds].filter((id) => !state.reviews.some((review) =>
      review.id === id && review.can_discard_reconciliation === true)).length;
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
    if (!workspaceIsCurrent || busyId || bulkBusy || refreshFailed || uncertainDecision) return;
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
    if (!workspaceIsCurrent || !state || lifecycleTab === "resolved" || !state.canEditDirectly) {
      return [];
    }
    return state.reviews.filter(
      (review) =>
        review.can_decide || review.can_discard_reconciliation === true,
    );
  }

  function toggleSelectAllVisible(selected: boolean) {
    if (!workspaceIsCurrent || busyId || bulkBusy || refreshFailed || uncertainDecision) return;
    const selectable = selectableBulkReviews();
    setSelectedBulkIds((current) => {
      const next = new Set(current);
      selectable.forEach((review) => selected ? next.add(review.id) : next.delete(review.id));
      return next;
    });
  }

  async function toggleGroupSelection(group: ReviewRailGroup, selected: boolean) {
    if (!workspaceIsCurrent || !state?.workspaceId || groupBusyKey || bulkBusy || refreshFailed || busyId || uncertainDecision) return;
    if (!selected) {
      const groupIds = knownGroupIds(group.key, group.reviews);
      setSelectedBulkIds((current) => {
        const next = new Set(current);
        groupIds.forEach((id) => next.delete(id));
        return next;
      });
      return;
    }
    const pageId = pendingGroupPageId(group);
    if (!pageId) return;
    const generation = ++groupSelectionGenerationRef.current;
    setGroupBusyKey(group.key);
    setError(null);
    try {
      const selection = await createReviewSelection(state.workspaceId, {
        filters: { ...bulkFilterPayload(), status: "pending", target_page_id: pageId },
      });
      if (generation !== groupSelectionGenerationRef.current) return;
      setGroupSelectionIds((current) => ({ ...current, [group.key]: selection.review_item_ids }));
      setGroupSnapshotTotals((current) => ({ ...current, [group.key]: selection.total }));
      setSelectedBulkIds((current) => new Set([...current, ...selection.review_item_ids]));
    } catch (exc) {
      if (generation === groupSelectionGenerationRef.current) setError(reviewSelectionError(exc, t));
    } finally {
      if (generation === groupSelectionGenerationRef.current) setGroupBusyKey(null);
    }
  }

  function toggleGroupCollapsed(key: string) {
    setCollapsedGroups((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function refreshQueueAfterBulk() {
    if (!workspaceIsCurrent || !state?.workspaceId) return;
    const workspaceId = state.workspaceId;
    const generation = loadMoreGenerationRef.current;
    const [reviewPage, counts, recentDecisionsPage] = await Promise.all([
      listReviews(workspaceId, reviewListFilters(activeScopeFilter)),
      getReviewCounts(workspaceId, reviewCountFilters()),
      listRecentReviewDecisions(workspaceId, { limit: 5 }),
    ]);
    if (generation !== loadMoreGenerationRef.current) return;
    setRefreshFailed(false);
    setGroupSnapshotTotals({});
    setState((current) => current && current.workspaceId === workspaceId ? {
      ...current,
      reviews: reviewPage.items,
      deferredReconciliationCount: deferredReconciliationCount(reviewPage),
      recentDecisions: recentDecisionsPage,
      selectedReviewId: searchParamsRef.current.get("review"),
      nextCursor: reviewPage.next_cursor,
      counts,
    } : current);
  }

  async function refreshQueue() {
    if (refreshing) {
      return;
    }
    setRefreshing(true);
    setError(null);
    try {
      const generation = loadMoreGenerationRef.current;
      if (uncertainDecision && draftKey && !await recoverDecision(uncertainDecision, draftKey, generation)) return;
      await refreshQueueAfterBulk();
    } catch (exc) {
      setError(reviewErrorMessage(exc, t("errors.refresh")));
    } finally {
      setRefreshing(false);
    }
  }

  async function openBulkApproveDialog(mode: "selected" | "filter", groupKey?: string) {
    if (!workspaceIsCurrent || !state?.workspaceId || bulkBusy || refreshFailed || busyId || uncertainDecision) return;
    const generation = loadMoreGenerationRef.current;
    const operation = ++bulkOperationRef.current;
    setBulkBusy(true);
    setError(null);
    try {
      const groupIds = groupKey
        ? knownGroupIds(groupKey)
        : null;
      const selection = await createReviewSelection(state.workspaceId,
        mode === "selected" ? {
          review_item_ids: [...selectedBulkIds].filter((id) => !groupIds || groupIds.includes(id)),
        } : { filters: bulkFilterPayload() });
      if (generation !== loadMoreGenerationRef.current) return;
      setBulkDialog({ mode, count: selection.eligible_count, selection,
        settlementCount: selection.auto_settled_review_item_ids?.length ?? 0 });
    } catch (exc) {
      if (generation === loadMoreGenerationRef.current) setError(reviewSelectionError(exc, t));
    } finally {
      if (operation === bulkOperationRef.current) setBulkBusy(false);
    }
  }

  async function runBulkApprove() {
    if (!workspaceIsCurrent || !state?.workspaceId || !bulkDialog || bulkBusy || busyId || refreshFailed || uncertainDecision) return;
    const workspaceId = state.workspaceId;
    const generation = loadMoreGenerationRef.current;
    const operation = ++bulkOperationRef.current;
    let token: string | null = bulkDialog.selection.selection_token;
    let aggregate: ReviewBulkDecisionResult = {
      approved: [], skipped: [], outcomes: [], auto_settled_review_item_ids: [],
      processed_count: 0, total: bulkDialog.selection.total,
      remaining_count: bulkDialog.selection.total, selection_complete: false,
      requires_reconfirmation: false,
    };
    setBulkBusy(true);
    setError(null);
    try {
      while (token) {
        if (generation !== loadMoreGenerationRef.current) return;
        const result = await decideReviewsBulk(workspaceId, {
          selection_token: token, decision_type: "approve", batch_limit: 500,
        });
        if (generation !== loadMoreGenerationRef.current) return;
        aggregate = {
          ...result,
          approved: [...new Set([...aggregate.approved, ...result.approved])],
          skipped: [...new Map([...(aggregate.skipped ?? []), ...result.skipped].map((item) => [item.id, item])).values()].filter((item) => !result.approved.includes(item.id) && !(result.auto_settled_review_item_ids ?? []).includes(item.id)),
          outcomes: [...new Map([...(aggregate.outcomes ?? []), ...(result.outcomes ?? [])].map((item) => [item.id, item])).values()],
          auto_settled_review_item_ids: [...new Set([...(aggregate.auto_settled_review_item_ids ?? []), ...(result.auto_settled_review_item_ids ?? [])])],
          processed_count: (result.total ?? aggregate.total ?? 0) - (result.remaining_count ?? 0),
        };
        setBulkResult(aggregate);
        setRefreshFailed(true);
        setSettledReviewStates((current) => {
          const next = { ...current };
          aggregate.approved.forEach((id) => { next[id] = "approved"; });
          aggregate.auto_settled_review_item_ids?.forEach((id) => { next[id] = "superseded"; });
          return next;
        });
        const completedIds = new Set([...aggregate.approved, ...(aggregate.auto_settled_review_item_ids ?? [])]);
        setSelectedBulkIds((current) => new Set([...current].filter((id) => !completedIds.has(id))));
        setGroupSelectionIds((current) => Object.fromEntries(Object.entries(current).map(
          ([key, ids]) => [key, ids.filter((id) => !completedIds.has(id))],
        )));
        setGroupSnapshotTotals({});
        if (result.requires_reconfirmation) {
          setError(t("workflow.bulkChanged"));
          break;
        }
        token = result.next_selection_token ?? null;
      }
    } catch (exc) {
      if (generation === loadMoreGenerationRef.current) {
        setError(reviewErrorMessage(exc, aggregate.approved.length
          ? t("workflow.bulkInterrupted") : t("workflow.decisionOutcomeUnknown")));
      }
    } finally {
      if (generation === loadMoreGenerationRef.current) {
        setBulkDialog(null);
        setRefreshFailed(true);
        try {
          await refreshQueueAfterBulk();
        } catch (exc) {
          if (generation === loadMoreGenerationRef.current) {
            setError(reviewErrorMessage(exc, t("workflow.savedRefreshFailed")));
          }
        }
      }
      if (operation === bulkOperationRef.current) setBulkBusy(false);
    }
  }

  async function runBulkReconciliationDiscard() {
    if (
      !workspaceIsCurrent || !state?.workspaceId
      || bulkBusy
      || busyId || refreshFailed || uncertainDecision
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
    const generation = loadMoreGenerationRef.current;
    const operation = ++bulkOperationRef.current;
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
      if (generation !== loadMoreGenerationRef.current) return;
      setBulkDiscardResult(result);
      setBulkDiscardOpen(false);
      setBulkDiscardReason("");
      setSelectedBulkIds(new Set(result.skipped.map((item) => item.review_item_id)));
      await refreshQueueAfterBulk();
    } catch (exc) {
      if (generation === loadMoreGenerationRef.current) setError(
        reviewErrorMessage(
          exc,
          t("errors.bulkDiscard", {
            defaultValue: "Failed to discard reconciliation branches",
          }),
        ),
      );
    } finally {
      if (operation === bulkOperationRef.current) setBulkBusy(false);
    }
  }

  function closeBulkDialog() {
    if (!bulkBusy) {
      setBulkDialog(null);
    }
  }

  async function submitDecision(decision: ReviewDecisionType): Promise<boolean> {
    try {
      return await runDecision(decision);
    } catch (exc) {
      setError(reviewErrorMessage(exc, t("errors.decide")));
      return false;
    }
  }

  // Edit-directly: claim the item, then route to the target page's editor. The
  // server hard-gates the claim; we surface failures (incl. claim conflicts) on
  // the page banner.
  async function editDirectly(review: ReviewItem) {
    if (!workspaceIsCurrent || !state?.workspaceId || busyId || bulkBusy || refreshFailed || uncertainDecision) {
      return;
    }
    const slug = editableTargetSlug(review);
    if (!slug) {
      setError(
        t("editDirectly.noTarget", {
          defaultValue: "This change has no editable page target.",
        }),
      );
      return;
    }
    setBusyId(review.id);
    const generation = loadMoreGenerationRef.current;
    try {
      await claimReview(state.workspaceId, review.id);
      if (generation !== loadMoreGenerationRef.current) {
        setBusyId(null);
        return;
      }
      navigate(`/context/${slug}/edit?from=review&reviewItemId=${review.id}`);
    } catch (exc) {
      if (generation === loadMoreGenerationRef.current) {
        setError(reviewErrorMessage(exc, t("errors.claim")));
      }
      setBusyId(null);
    }
  }

  if (!state || (state.workspaceId && !workspaceIsCurrent)) {
    return <RouteLoading />;
  }

  const selectedReview = reviewForCurrentParam();
  const nextQueuedId = nextReviewOrder.after.find((id) => state.reviews.some((review) => review.id === id))
    ?? (!state.nextCursor ? nextReviewOrder.before.find((id) => state.reviews.some((review) => review.id === id)) : undefined);
  const nextQueuedReview = state.reviews.find((review) => review.id === nextQueuedId);
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
  const showWorkspaceCounts = state.canEditDirectly && !refreshFailed;
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
    workspaceIsCurrent && state.canEditDirectly && lifecycleTab !== "resolved" && selectedBulkCount > 0 && !busyId && !bulkBusy && !refreshFailed && !groupBusyKey && !uncertainDecision;

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
              <CountChip value={refreshFailed ? "—" : state.counts[lifecycleTab]} compact />
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
            {riskFilter !== "all" || activeScopeFilter !== "all" || resolvedStatusFilter !== "all" || includeDeferredReconciliations ? (
              <div className="review-active-filters" role="status">
                {riskFilter !== "all" ? <StatusPill status={riskFilter} compact /> : null}
                {activeScopeFilter !== "all" ? <span>{t(`filters.scopeOptions.${activeScopeFilter}`)}</span> : null}
                {resolvedStatusFilter !== "all" ? <StatusPill status={resolvedStatusFilter} compact /> : null}
                {includeDeferredReconciliations ? <span>{t("filters.olderReconciliations")}</span> : null}
                <button className="text-button" onClick={() => writeReviewQuery({ risk: null, scope: null, outcome: null, older: null, review: null })} type="button">{t("rail.clearFilters")}</button>
              </div>
            ) : null}
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
            <div className="review-bulk-bar" aria-busy={bulkBusy || Boolean(groupBusyKey)}>
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
                  disabled={Boolean(busyId) || bulkBusy || refreshFailed || Boolean(groupBusyKey) || Boolean(uncertainDecision)}
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
                  {riskFilter !== "all" || activeScopeFilter !== "all" || resolvedStatusFilter !== "all" || includeDeferredReconciliations ? t("workflow.noFilterMatches") : t(`rail.emptyForStatus_${lifecycleTab}`, {
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
                ) : riskFilter !== "all" || includeDeferredReconciliations ? (
                  <button className="inline-action" onClick={() => {
                    writeReviewQuery({ review: null, risk: null, scope: null, outcome: null, older: null });
                  }} type="button">
                    {t("rail.clearFilters", { defaultValue: "Clear filters" })}
                  </button>
                ) : null}
              </div>
            ) : (
              <div className="review-rail-list">
                {lifecycleTab === "pending" && groupedReviews.some((group) => group.pageTitle) ? (
                  <div className="review-group-controls">
                    <button className="text-button" onClick={() => setCollapsedGroups(new Set(groupedReviews.filter((group) => group.pageTitle).map((group) => group.key)))} type="button">{t("workflow.collapseAll")}</button>
                    <button className="text-button" onClick={() => setCollapsedGroups(new Set())} type="button">{t("workflow.expandAll")}</button>
                  </div>
                ) : null}
                {groupedReviews.map((group) => {
                  const pendingGroup = lifecycleTab === "pending";
                  const knownIds = knownGroupIds(group.key, group.reviews);
                  const snapshotIds = groupSelectionIds[group.key];
                  const hasCurrentSnapshot = groupSnapshotTotals[group.key] !== undefined
                    && group.reviews.every((review) => snapshotIds?.includes(review.id));
                  const groupIsComplete = railIsComplete || hasCurrentSnapshot;
                  const selectedCount = knownIds.filter((id) => selectedBulkIds.has(id)).length;
                  const rows = group.reviews.map((review, index) => (
                    <ReviewRow
                      active={review.id === selectedReview?.id}
                      bulkDisabled={Boolean(busyId) || bulkBusy || refreshFailed || Boolean(groupBusyKey) || Boolean(uncertainDecision) || lifecycleTab === "resolved" || !state.canEditDirectly || (!review.can_decide && review.can_discard_reconciliation !== true)}
                      bulkSelectable={lifecycleTab !== "resolved" && (review.can_decide || review.can_discard_reconciliation === true)}
                      bulkSelected={selectedBulkIds.has(review.id)}
                      grouped={pendingGroup}
                      ordinal={index + 1}
                      key={review.id}
                      onSelect={(reviewId) => {
                        setReviewParam(reviewId);
                        if (window.matchMedia("(max-width: 1024px)").matches) {
                          document.querySelector<HTMLButtonElement>(".review-screen .workspace-mobile-drawer-scrim")?.click();
                        }
                        reviewMainRef.current?.scrollTo({ top: 0 });
                      }}
                      onToggleBulkSelected={toggleBulkSelection}
                      review={settledReviewView(review)}
                      showStatusPill={lifecycleTab === "resolved" || Boolean(settledReviewStates[review.id])}
                    />
                  ));
                  return pendingGroup ? (
                    <ReviewDocumentGroup
                      busy={groupBusyKey === group.key}
                      canCollapse={Boolean(group.pageTitle)}
                      canSelect={state.canEditDirectly && Boolean(pendingGroupPageId(group))}
                      collapsed={collapsedGroups.has(group.key)}
                      complete={groupIsComplete}
                      current={group.reviews.some((review) => review.id === selectedReview?.id)}
                      disabled={Boolean(busyId) || bulkBusy || refreshFailed || Boolean(groupBusyKey) || Boolean(uncertainDecision)}
                      key={group.key}
                      kind={reviewDocumentKind(group.reviews[0])}
                      loadedCount={hasCurrentSnapshot ? knownIds.length : group.reviews.length}
                      onApprove={() => void openBulkApproveDialog("selected", group.key)}
                      onSelect={(selected) => void toggleGroupSelection(group, selected)}
                      onToggle={() => toggleGroupCollapsed(group.key)}
                      selectedCount={selectedCount}
                      allSelected={groupIsComplete && knownIds.length > 0 && knownIds.every((id) => selectedBulkIds.has(id))}
                      title={group.pageTitle ?? reviewTargetTitle(group.reviews[0], t)}
                    >{rows}</ReviewDocumentGroup>
                  ) : (
                    <div className="review-date-group" key={group.key}>
                      <div className="review-date-divider">{formatReviewDate(group.activityAt, reviewLocale)}</div>
                      {rows}
                    </div>
                  );
                })}
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
          <section className="feature-main review-main" ref={reviewMainRef}>
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
            {error || uncertainDecision ? <StatusBanner tone="warning" action={refreshFailed || uncertainDecision ? (
              <div className="review-recovery-actions">
                <button className="icon-button" disabled={refreshing || Boolean(busyId)} onClick={() => void refreshQueue()} type="button">
                  {t("workflow.refreshQueue")}
                </button>
                {uncertainDecision ? <button className="icon-button" disabled={refreshing || Boolean(busyId)} onClick={() => void retryPendingDecision()} type="button">
                  {t("workflow.retrySameDecision")}
                </button> : null}
              </div>
            ) : undefined}>{error || t("workflow.decisionOutcomeUnknown")}</StatusBanner> : null}
            {detachedReview?.id === selectedReview?.id && completedReview?.id !== selectedReview?.id ? (
              <StatusBanner tone="info">{t("workflow.outsideFilters")}</StatusBanner>
            ) : null}
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
                <ReviewBulkOutcomes result={bulkResult} onOpen={setReviewParam} />
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
            {selectedReview ? (
              <>
                <ReviewDetail
                  key={reviewContentKey(selectedReview)}
                  workspaceId={state.workspaceId}
                  decisionSaved={completedReview?.id === selectedReview.id}
                  nextDisabled={refreshFailed || refreshing || Boolean(busyId) || bulkBusy || Boolean(uncertainDecision) || (state.reviews.length === 0 && state.nextCursor === null)}
                  nextLabel={nextQueuedReview ? `${reviewTargetTitle(nextQueuedReview, t)}${reviewTargetVersionNo(nextQueuedReview) != null ? ` · v${reviewTargetVersionNo(nextQueuedReview)}` : ""}` : null}
                  noNextReview={!refreshFailed && state.reviews.length === 0 && state.nextCursor === null}
                  onNextReview={() => void openNextReview()}
                  busy={Boolean(busyId) || bulkBusy}
                  decisionsBlocked={refreshFailed || Boolean(uncertainDecision)}
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
            ) : reviewParam ? <RouteLoading /> : (
              <ReviewOverview
                counts={state.counts}
                onOpenRiskPolicy={(risk) =>
                  navigate(`/settings?section=review#auto-approve-${risk}`)
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
              {t(bulkDialog.mode === "selected" ? "bulk.confirmSelectedBody" : "bulk.confirmBody", {
                count: bulkDialog.count,
                total: bulkDialog.selection.total,
                defaultValue:
                  "{{count}} reviews are eligible to approve under the current filters. Items that can no longer be approved are skipped and listed in the result.",
              })}
            </p>
            <div className="review-bulk-preview" role="status">
              <span>
                {t("bulk.previewEligible", {
                  count: bulkDialog.selection.eligible_count,
                  defaultValue: "{{count}} eligible",
                })}
              </span>
              <span>
                {t("bulk.previewSkipped", {
                  count: bulkDialog.selection.items.filter((item) => !item.eligible && !item.will_auto_settle).length,
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
            <p className="field-hint">{t("workflow.fixedSelection", { count: bulkDialog.selection.total, expires: formatVersionTimestamp(bulkDialog.selection.expires_at) })}</p>
            <ul className="review-bulk-preview-items">
              {bulkDialog.selection.items.map((item) => (
                <li key={item.id}>
                  <strong>{item.target_title ?? t("workflow.unavailableReview")}{item.target_version_no != null ? ` · v${item.target_version_no}` : ""}</strong>
                  <span>{item.will_auto_settle ? t("workflow.autoSettled") : item.eligible ? t("workflow.eligible") : reviewOutcomeReason(item.reason_code, item.reason, t)}</span>
                  {item.eligible && !item.will_auto_settle && (item.auto_settled_review_item_ids?.length ?? 0) > 0 ? (
                    <span className="review-bulk-item-impact">
                      {t("bulk.approvalImpact", { count: item.auto_settled_review_item_ids?.length ?? 0 })}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
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
  id,
  disabled = false,
  checked,
  indeterminate,
  onChange,
}: {
  ariaLabel: string;
  id?: string;
  disabled?: boolean;
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
      id={id}
      aria-label={ariaLabel}
      checked={checked}
      disabled={disabled}
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
          <Link className="inline-action" to="/settings?section=review">
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
            to="/activity?kind=workspace&subtype=review"
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
                to={`/review?review=${encodeURIComponent(decision.review_item_id)}`}
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
  ordinal,
  grouped = false,
  active,
  bulkDisabled,
  bulkSelectable,
  bulkSelected,
  onSelect,
  onToggleBulkSelected,
  showStatusPill,
}: {
  review: ReviewItem;
  ordinal: number;
  grouped?: boolean;
  active: boolean;
  bulkDisabled: boolean;
  bulkSelectable: boolean;
  bulkSelected: boolean;
  onSelect: (reviewItemId: string) => void;
  onToggleBulkSelected: (reviewItemId: string, selected: boolean) => void;
  showStatusPill: boolean;
}) {
  const { t } = useTranslation("review");
  // The rail sorts and groups by activity (resolution date for resolved
  // items), so the visible timestamp must follow the same axis or rows look
  // misfiled under their date divider.
  const activityAt = reviewActivityAt(review);
  const target = reviewPrimaryOperation(review);
  const rawTargetTitle = reviewTargetTitle(review, t);
  const targetTitle = rawTargetTitle;
  const targetVersionNo = reviewTargetVersionNo(review);
  const candidateState = target?.content?.is_current_candidate;
  const reviewType = domainLabel("review", "reviewType", review.review_type, t);
  const decisionSummary = localizedDecisionSummary(review, t);
  const secondarySummary =
    decisionSummary && decisionSummary !== rawTargetTitle
      ? decisionSummary
      : null;
  const isReconciliation = review.review_type === "context_reconciliation";
  const reconciliationChatHref =
    isReconciliation && review.reconciliation_chat_session_id
      ? `/chats/${encodeURIComponent(review.reconciliation_chat_session_id)}`
      : null;
  return (
    <div
      className={`review-row-shell${bulkSelectable ? " has-bulk-select" : ""}`}
    >
      {bulkSelectable ? (
        <label className="review-row-select">
          <input
            aria-label={t("bulk.selectReview", {
              title: `${targetTitle}${targetVersionNo != null ? ` · v${targetVersionNo}` : ""} · ${formatVersionTimestamp(activityAt)} · ${t("workflow.reviewOrdinal", { number: ordinal })} · ${t(`filters.riskOptions.${review.risk_level}`, { defaultValue: humanizeEnum(review.risk_level) })}`,
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
        aria-current={active ? "true" : undefined}
        aria-label={`${targetTitle}${targetVersionNo != null ? ` · v${targetVersionNo}` : ""}`}
        className={`review-row selectable-row${active ? " active" : ""}${grouped ? " review-row-grouped" : ""}`}
        type="button"
        onClick={() => onSelect(review.id)}
      >
        <div className="review-row-main">
          <div className="review-row-identity">
            <strong>{grouped && targetVersionNo != null ? `v${targetVersionNo}` : targetTitle}</strong>
            {grouped ? <StatusPill status={review.risk_level} compact /> : null}
            {grouped && review.status === "pending" && typeof candidateState === "boolean" ? <span className="review-candidate-label">{t(candidateState ? "workflow.currentCandidate" : "workflow.olderCandidate")}</span> : null}
            {active ? <span className="review-current-label">{t("workflow.viewing")}</span> : null}
          </div>
          <p>
            <span className="review-row-type">{reviewType}</span>
            {secondarySummary ? <span>{secondarySummary}</span> : null}
          </p>
          {target && !grouped ? (
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
            {!grouped && targetVersionNo != null ? (
              <span className="review-version-badge">v{targetVersionNo}</span>
            ) : null}
            {!grouped ? <StatusPill status={review.risk_level} compact /> : null}
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
  workspaceId,
  decisionSaved,
  nextDisabled,
  nextLabel,
  noNextReview,
  onNextReview,
  decisionReason,
  busy,
  decisionsBlocked,
  canEditDirectly,
  onReasonChange,
  onDecision,
  onEditDirectly,
  onRouteChanges,
}: {
  review: ReviewItem;
  workspaceId: string;
  decisionSaved: boolean;
  nextDisabled: boolean;
  nextLabel: string | null;
  noNextReview: boolean;
  onNextReview: () => void;
  decisionReason: string;
  busy: boolean;
  decisionsBlocked: boolean;
  canEditDirectly: boolean;
  onReasonChange: (reason: string) => void;
  onDecision: (decision: ReviewDecisionType) => Promise<boolean>;
  onEditDirectly: () => void;
  onRouteChanges: (
    audienceKind: ReviewAudienceKind,
    reason: string,
  ) => Promise<boolean>;
}) {
  const isResolved = lifecycleTabForStatus(review.status) === "resolved";
  const isReadOnly = isResolved || decisionSaved;
  const [contentState, setContentState] = useState<"loading" | "ready" | "unavailable">("loading");
  const isReconciliation = review.review_type === "context_reconciliation";
  const reconciliationHref =
    review.reconciliation_available === true
    && review.reconciliation_page_id
    && review.reconciliation_version_id
      ? `/context?${new URLSearchParams({
          target: review.reconciliation_page_id,
          tab: "history",
          reconcile: review.reconciliation_version_id,
        }).toString()}`
      : null;
  const reconciliationChatHref =
    isReconciliation && review.reconciliation_chat_session_id
      ? `/chats/${encodeURIComponent(review.reconciliation_chat_session_id)}`
      : null;
  const hasReconciliationProvenance =
    review.reconciliation_base_version_no != null
    || review.reconciliation_head_advance_count != null
    || review.reconciliation_conflict_count != null
    || Boolean(review.reconciliation_branch_actor_kind);
  const requiresExactBody = review.review_type === "context_update" && !review.evidence?.operations.length
    || (review.evidence?.operations.some((operation) =>
      (operation.document?.document_kind === "context" || ["context_page", "context_page_version"].includes(operation.target_type))
      && operation.content?.mode !== "metadata_only") ?? false);
  const actionBusy = busy || decisionsBlocked;
  const canApprove = !isReadOnly && (review.actions?.approve.allowed ?? review.can_decide) && !actionBusy && (!requiresExactBody || contentState === "ready");
  const canRequestChanges = !isReadOnly && (review.actions?.request_changes.allowed ?? review.can_decide) && !actionBusy;
  const canReject = !isReadOnly && (review.actions?.reject.allowed ?? review.can_decide) && !actionBusy;
  const canEditDirectlyAction =
    canEditDirectly && review.status === "changes_requested" && (review.actions?.edit_directly.allowed ?? review.can_claim);
  const { t } = useTranslation("review");
  const decisionReasonId = useId();
  const decisionReasonHintId = useId();
  const routingTitleId = useId();
  const rejectTitleId = useId();
  const reasonTextareaRef = useRef<HTMLTextAreaElement>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectError, setRejectError] = useState<string | null>(null);
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
    if (!canReject) {
      return;
    }
    // Reject is terminal (it removes the item from the queue), so gate it behind
    // an explicit confirm Dialog instead of submitting on the first click. The
    // decision reason stays optional — only the confirm is required.
    setRejectError(null);
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
    const succeeded = await onDecision("reject");
    if (succeeded) setRejectOpen(false);
    else setRejectError(t("workflow.decisionFailedKeepDraft"));
  }

  function openRoutingDialog() {
    setRoutingError(null);
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
      if (await onRouteChanges(routingAudience, routingReason)) setRoutingOpen(false);
      else setRoutingError(t("workflow.decisionFailedKeepDraft"));
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
          <h2 id="review-detail-title" tabIndex={-1}>{targetTitle}</h2>
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
          <div className="review-detail-meta">
            {reviewTargetVersionNo(review) != null ? <span>v{reviewTargetVersionNo(review)}</span> : null}
            <time dateTime={review.updated_at}>{formatVersionTimestamp(review.updated_at)}</time>
          </div>
        </div>
        {/* One page-level action cluster in the heading, like every other
            detail screen: decisions only. */}
        {!isReadOnly && !isReconciliation ? (
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
              disabled={actionBusy}
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
        {decisionSaved ? (
          <div className="detail-heading-actions review-action-bar" role="status">
            <span className="reader-action-status">{t("workflow.savedDecision", { version: reviewTargetVersionNo(review) != null ? `v${reviewTargetVersionNo(review)}` : "", decision: t(`overview.decisionType.${review.decisions[0]?.decision_type ?? "approve"}`) })}</span>
            {noNextReview ? <span>{t("workflow.queueComplete")}</span> : <div className="review-next-action">
              <button className="icon-button primary" disabled={nextDisabled} onClick={onNextReview} type="button">
                {t("workflow.nextReview")}<ChevronRight size={15} aria-hidden="true" />
              </button>
              {nextLabel ? <span>{nextLabel}</span> : null}
            </div>}
            <a className="icon-button" href="#review-decision-history" onClick={(event) => {
              event.preventDefault();
              const history = document.getElementById("review-decision-history");
              history?.scrollIntoView({ block: "start" }); history?.focus({ preventScroll: true });
            }}>{t("detail.decisionHistory")}</a>
          </div>
        ) : null}
      </header>
      {!isReadOnly && isReconciliation ? (
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
      {!isReadOnly && !isReconciliation && review.superseded_review_count > 0 ? (
        <StatusBanner tone="warning">
          {t("detail.approvalImpact", {
            count: review.superseded_review_count,
            defaultValue:
              "Approving will publish this version. {{count}} earlier pending reviews will be settled with it.",
          })}
        </StatusBanner>
      ) : null}
      {!isReadOnly && review.actions?.approve.reason_code ? <p className="review-action-unavailable" role="status">{reviewOutcomeReason(review.actions.approve.reason_code, null, t)}</p> : null}
      <ReviewEvidencePanel workspaceId={workspaceId} review={review} onContentStateChange={({ reviewId, status }) => { if (reviewId === review.id) setContentState(status); }} />
      <details className="review-risk-explanation">
        <summary>{t("detail.whyRisk", { defaultValue: "Why this risk?" })}: {riskInsight.summary}</summary>
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
      </details>
      {!isReadOnly && !isReconciliation ? (
        <details className="review-decision-block">
          <summary>{t("workflow.optionalReason")}</summary>
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
        </details>
      ) : null}
      {decisionSummary ? (
        <div className="evidence-box">
          <strong>{t("detail.summary", { defaultValue: "Summary" })}</strong>
          <p>{decisionSummary}</p>
        </div>
      ) : null}
      <details className="review-diagnostics">
        <summary>{t("workflow.reviewInformation")}</summary>
        <p className="meta-line"><ChangeSetValue changeSetId={review.change_set_id} /></p>
        {review.evidence ? (
          <p>{t("detail.trigger")}: {domainLabel("review", "triggerType", review.evidence.trigger_type, t)} · {t("workflow.citationSkipped")}: {review.evidence.cite_call_skipped ? t("workflow.yes") : t("workflow.no")}</p>
        ) : null}
        {review.evidence ? <ReviewEvidence review={review} /> : null}
      </details>
      <div className="evidence-box review-history" id="review-decision-history" tabIndex={-1}>
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
      {!isReadOnly && rejectOpen ? (
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
                  "Confirm the target version and its effect before rejecting.",
              })}
            </p>
            <ReviewRejectImpact review={review} />
            <FormField htmlFor={`${rejectTitleId}-reason`} label={t("detail.reasonLabel")} hint={decisionReasonHint}>
              <textarea {...textInputLimit(2000, false)} id={`${rejectTitleId}-reason`} value={decisionReason} onChange={(event) => onReasonChange(event.currentTarget.value)} />
            </FormField>
            {rejectError ? <StatusBanner tone="warning">{rejectError}</StatusBanner> : null}
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
                disabled={actionBusy}
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
      {!isReadOnly && routingOpen ? (
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
                onChange={(event) => {
                  setRoutingReason(event.currentTarget.value);
                  onReasonChange(event.currentTarget.value);
                }}
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
                className="icon-button warning"
                disabled={actionBusy || routingReason.trim().length === 0}
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

// First page-targeted operation's slug, used to route the editor. Section/claim
// operations carry the parent page slug on target_slug as well.
function editableTargetSlug(review: ReviewItem): string | null {
  const operation = review.evidence?.operations.find(
    (candidate) => candidate.target_slug,
  );
  return operation?.target_slug ?? null;
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

function reviewFilterValue<T extends string>(raw: string | null, allowed: readonly T[], fallback: T): T {
  return raw && allowed.includes(raw as T) ? raw as T : fallback;
}

function ReviewDocumentGroup({
  title, kind, children, canCollapse, collapsed, complete, current,
  canSelect, disabled, busy, selectedCount, allSelected, loadedCount,
  onSelect, onToggle, onApprove,
}: {
  title: string;
  kind: ReviewDocumentKind;
  children: ReactNode;
  canCollapse: boolean;
  collapsed: boolean;
  complete: boolean;
  current: boolean;
  canSelect: boolean;
  disabled: boolean;
  busy: boolean;
  selectedCount: number;
  allSelected: boolean;
  loadedCount: number;
  onSelect: (selected: boolean) => void;
  onToggle: () => void;
  onApprove: () => void;
}) {
  const { t } = useTranslation("review");
  const bodyId = useId();
  const selectionId = useId();
  const hidden = canCollapse && collapsed;
  return (
    <section className="review-document-group" aria-label={title} aria-busy={busy}>
      <header className="review-document-group-header">
        {canSelect ? (
          <label className="review-document-group-check" htmlFor={selectionId}>
            <SelectAllCheckbox
              id={selectionId}
              ariaLabel={t("workflow.selectDocumentGroup", { title })}
              checked={allSelected}
              disabled={disabled}
              indeterminate={selectedCount > 0 && !allSelected}
              onChange={onSelect}
            />
          </label>
        ) : <span />}
        {canCollapse ? (
          <button className="review-group-collapse" type="button" onClick={onToggle}
            aria-controls={bodyId} aria-expanded={!hidden}
            aria-label={t(hidden ? "workflow.expandGroup" : "workflow.collapseGroup", { title })}>
            {hidden ? <ChevronRight size={15} aria-hidden="true" /> : <ChevronDown size={15} aria-hidden="true" />}
          </button>
        ) : <span />}
        <div className="review-document-group-title">
          <span className="review-document-kind">
            {kind === "context" || kind === "agent" || kind === "library" ? <span className={`tree-row-state-dot lc-dot-${kind}`} aria-hidden="true" /> : <FileText size={13} aria-hidden="true" />}
            {t(`workflow.documentKinds.${kind}`)}
          </span>
          {canCollapse ? <button className="review-group-title-toggle" aria-controls={bodyId} aria-expanded={!hidden} onClick={onToggle} type="button">{title}</button> : <strong>{title}</strong>}
          {hidden && current ? <span className="review-current-label">{t("workflow.viewing")}</span> : null}
        </div>
        <CountChip value={loadedCount} approxMore={!complete} compact />
      </header>
      <div className="review-document-group-body" hidden={hidden} id={bodyId}>{children}</div>
      {canSelect && (selectedCount > 0 || busy) ? (
        <div className="review-document-group-footer">
          <span role="status">{busy ? t("workflow.selectingGroup") : t("workflow.groupSelected", { count: selectedCount })}</span>
          {selectedCount > 0 ? <button className="icon-button primary" disabled={disabled} onClick={onApprove} type="button">{t("workflow.approveGroupSelection")}</button> : null}
        </div>
      ) : null}
    </section>
  );
}

type ReviewDocumentKind = "context" | "library" | "agent" | "multiple" | "unknown";

function reviewDocumentKind(review: ReviewItem): ReviewDocumentKind {
  const kinds = new Set<ReviewDocumentKind>();
  for (const operation of review.evidence?.operations ?? []) {
    if (operation.document?.document_kind) kinds.add(operation.document.document_kind);
    else if (["context_page", "context_page_version"].includes(operation.target_type)) kinds.add("context");
    else if (["agent_page", "agent_page_version"].includes(operation.target_type)) kinds.add("agent");
    else if (operation.target_type === "library_item") kinds.add("library");
    else kinds.add("unknown");
  }
  return kinds.size > 1 ? "multiple" : kinds.values().next().value ?? "unknown";
}

function pendingGroupPageId(group: ReviewRailGroup): string | null {
  return group.key.startsWith("context-page:") ? group.key.slice("context-page:".length) : null;
}

function ReviewRejectImpact({ review }: { review: ReviewItem }) {
  const { t } = useTranslation("review");
  const effect = review.reject_effect;
  return (
    <div className="evidence-box review-reject-impact">
      <strong>{reviewTargetTitle(review, t)}</strong>
      <p>{effect?.candidate_version_no != null ? t("workflow.rejectCandidate", { version: effect.candidate_version_no }) : t("workflow.rejectReviewOnly")}</p>
      <p>{effect ? t(`workflow.rejectEffects.${effect.effect}`, { version: effect.published_version_no ?? "—" }) : t("workflow.rejectEffectUnavailable")}</p>
      {effect && effect.visible_sibling_count > 0 ? <p>{t("workflow.rejectSiblingImpact", { count: effect.visible_sibling_count })}</p> : null}
    </div>
  );
}

function ReviewBulkOutcomes({ result, onOpen }: { result: ReviewBulkDecisionResult; onOpen: (id: string) => void }) {
  const { t } = useTranslation("review");
  const [visible, setVisible] = useState(50);
  const outcomes = result.outcomes?.length ? result.outcomes : result.skipped.map((item) => ({
    id: item.id, outcome: "excluded" as const, reason: item.reason,
    reason_code: item.reason_code, target_title: null, target_version_no: null,
  }));
  return (
    <details className="review-bulk-outcomes" open={result.skipped.length > 0 || Boolean(result.requires_reconfirmation)}>
      <summary>{t("workflow.itemResults", { processed: result.processed_count ?? result.approved.length, total: result.total ?? result.approved.length + result.skipped.length })}</summary>
      <ul className="review-bulk-outcome-list">
        {outcomes.slice(0, visible).map((item) => (
          <li key={item.id}>
            <StatusPill compact status={item.outcome === "approved" ? "approved" : item.outcome === "failed" ? "failed" : "superseded"} label={t(`workflow.outcomes.${item.outcome}`)} />
            <div>
              <strong>{item.target_title ?? t("workflow.unavailableReview")}{item.target_version_no != null ? ` · v${item.target_version_no}` : ""}</strong>
              {item.reason_code || item.reason ? <p>{reviewOutcomeReason(item.reason_code, item.reason, t)}</p> : null}
            </div>
            <button className="icon-button" onClick={() => onOpen(item.id)} type="button">{t("workflow.openReview")}</button>
          </li>
        ))}
      </ul>
      {outcomes.length > visible ? <button className="icon-button" onClick={() => setVisible((count) => count + 50)} type="button">{t("workflow.moreResults", { count: outcomes.length - visible })}</button> : null}
    </details>
  );
}

function reviewOutcomeReason(code: string | null | undefined, message: string | null | undefined, t: TFunction): string {
  return code ? t(`workflow.reasons.${code}`, { defaultValue: message || t("workflow.unavailableReview") }) : message || t("workflow.unavailableReview");
}

function reviewSelectionError(error: unknown, t: TFunction): string {
  if (error instanceof ApiError) {
    const details = Reflect.get(error.body, "details");
    if (error.body.error_code === "review_selection_limit_exceeded" && details && typeof details === "object") {
      return t("workflow.selectionLimit", { count: Reflect.get(details, "total"), limit: Reflect.get(details, "limit") });
    }
    return reviewOutcomeReason(error.body.error_code, error.displayMessage, t);
  }
  return reviewErrorMessage(error, t("errors.bulkPreview"));
}

function readPendingReviewDecision(key: string): PendingReviewDecision | null {
  try {
    const value: unknown = JSON.parse(readReviewDraft(`${key}.pending`) || "null");
    if (!value || typeof value !== "object") return null;
    const pending = value as PendingReviewDecision;
    return typeof pending.decisionId === "string" && typeof pending.workspaceId === "string" && typeof pending.reviewId === "string"
      && ["approve", "reject", "request_changes"].includes(pending.decision)
      ? pending : null;
  } catch { return null; }
}

function DetailMetric({ label, value }: { label: string; value: ReactNode }) {
  return <div className="detail-metric"><span>{label}</span><strong>{value}</strong></div>;
}

function reviewContentKey(review: ReviewItem): string {
  return JSON.stringify([review.id, review.updated_at, review.evidence?.operations.map((operation) => [operation.document, operation.content])]);
}

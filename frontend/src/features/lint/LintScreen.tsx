import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ClipboardCheck,
  ExternalLink,
  Info,
  Play,
  Wrench,
  X,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { apiErrorMessage } from "../../shared/api/client";
import {
  createLintRun,
  decideLintFinding,
  decideLintFindingsBatch,
  getLintRun,
  listCoverageGaps,
  listLintFindings,
} from "../../shared/api/operations";
import type { CoverageGap, LintFinding } from "../../shared/api/operations";
import { listTreeData } from "../../shared/api/workspace";
import type { TreeNodePayload } from "../../shared/api/workspace";
import { ActionMenu } from "../../shared/components/ActionMenu";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { Dialog } from "../../shared/components/Dialog";
import { NumericPagination } from "../../shared/components/NumericPagination";
import { ReaderTabs } from "../../shared/components/ReaderTabs";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { searchResultHref } from "../../shared/lib/searchResultHref";
import { treeNodeHref, useRouteWorkspaceId, useWorkspaceRoute, withWorkspaceQuery, workspacePath } from "../../shared/lib/workspaceRoutes";
import { isEditorRole } from "../../shared/workspace/permissions";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { NotFoundScreen } from "../errors/NotFoundScreen";
import {
  lintFindingMessage,
  lintMessageIsSpecific,
  lintRuleLabel,
} from "./lintRuleCatalog";

const SEVERITIES = ["all", "error", "warning", "info"] as const;
const LAYERS = ["all", "format", "knowledge", "nav", "maintenance"] as const;
const STATUSES = ["open", "resolved"] as const;
const VIEWS = ["findings", "coverage"] as const;
const LINT_PAGE_SIZE = 20;
const LINT_BATCH_MAX_IDS = 100;

type SeverityFilter = (typeof SEVERITIES)[number];
type LayerFilter = (typeof LAYERS)[number];
type StatusFilter = (typeof STATUSES)[number];
type ViewFilter = (typeof VIEWS)[number];

type LintState = {
  workspaceId: string;
  loadedFindingId: string | null;
  findings: LintFinding[];
  treeTargets: Record<string, TreeNodePayload>;
  findingsTotal: number;
  gaps: CoverageGap[];
  gapsHasMore: boolean;
  page: number;
  pageSize: number;
  pageCount: number;
  hasPrevious: boolean;
  hasNext: boolean;
};

class LintFindingNotFoundError extends Error {}

function enumParam<T extends string>(
  params: URLSearchParams,
  key: string,
  values: readonly T[],
  fallback: T,
): T {
  const value = params.get(key) as T | null;
  return value && values.includes(value) ? value : fallback;
}

function positiveIntegerParam(value: string | null, fallback: number) {
  if (!value || !/^\d+$/.test(value)) return fallback;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export function LintScreen() {
  const routeWorkspaceId = useRouteWorkspaceId();
  const route = useWorkspaceRoute();
  const navigate = useNavigate();
  const { t } = useTranslation("lint");
  const selectAllCheckboxRef = useRef<HTMLInputElement>(null);
  const findingsHeadingRef = useRef<HTMLSpanElement>(null);
  const workspaceContext = useOptionalWorkspaceContext();
  const selectedWorkspaceId = routeWorkspaceId;
  const canEdit = isEditorRole(workspaceContext?.selectedWorkspace?.role);
  const roleKnown = Boolean(workspaceContext?.selectedWorkspace);
  const [searchParams, setSearchParams] = useSearchParams();
  // Collapsed by default at every width now that the view switch has moved out
  // to tabs: four bordered filter boxes used to push the findings themselves
  // 170px below the fold.
  const [filtersOpen, setFiltersOpen] = useState(false);
  const linkedFindingId = route?.targetId ?? null;
  const view = linkedFindingId
    ? "findings"
    : enumParam(searchParams, "view", VIEWS, "findings");
  const status = enumParam(searchParams, "status", STATUSES, "open");
  const layer = enumParam(searchParams, "layer", LAYERS, "all");
  const severity = enumParam(searchParams, "severity", SEVERITIES, "all");
  const page = positiveIntegerParam(searchParams.get("page"), 1);
  const pageSize = LINT_PAGE_SIZE;
  const activeFilterCount = [
    status !== "open",
    layer !== "all",
    severity !== "all",
  ].filter(Boolean).length;
  const pollGeneration = useRef(0);
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [dismissFindingId, setDismissFindingId] = useState<string | null>(null);
  const [batchDismiss, setBatchDismiss] = useState(false);
  const [dismissReason, setDismissReason] = useState("");
  const [fixFindingId, setFixFindingId] = useState<string | null>(null);
  const [mutationAnnouncement, setMutationAnnouncement] = useState("");
  const [selectedFindingIds, setSelectedFindingIds] = useState<Set<string>>(
    new Set(),
  );
  const [busyFindingIds, setBusyFindingIds] = useState<Set<string>>(new Set());
  const [findingNotFoundKey, setFindingNotFoundKey] = useState<string | null>(null);
  useEffect(() => {
    setFindingNotFoundKey(null);
  }, [routeWorkspaceId, linkedFindingId]);
  const {
    data: state,
    error,
    reload: reloadLatest,
    setData: setState,
    setError,
  } = useAsyncLoad<LintState>(
    () =>
      loadState(
        { severity, layer, status, view, page, pageSize },
        selectedWorkspaceId,
        linkedFindingId,
      ),
    [
      severity,
      layer,
      status,
      view,
      page,
      pageSize,
      selectedWorkspaceId,
      linkedFindingId,
    ],
    {
      errorMessage: t("errors.load"),
      formatError: (exc, fallback) => {
        if (exc instanceof LintFindingNotFoundError && linkedFindingId) {
          setFindingNotFoundKey(`${routeWorkspaceId}:${linkedFindingId}`);
          return t("errors.findingNotFound", { defaultValue: "Lint finding not found" });
        }
        return apiErrorMessage(exc, fallback);
      },
      fallbackData: {
        workspaceId: "",
        loadedFindingId: null,
        findings: [],
        treeTargets: {},
        findingsTotal: 0,
        gaps: [],
        gapsHasMore: false,
        page: 1,
        pageSize: 20,
        pageCount: 0,
        hasPrevious: false,
        hasNext: false,
      },
    },
  );

  const selectionContextKey = [
    selectedWorkspaceId ?? state?.workspaceId ?? "",
    view,
    status,
    layer,
    severity,
    page,
    pageSize,
    linkedFindingId ?? "",
  ].join("\u001f");
  const latestSelectionContextRef = useRef(selectionContextKey);
  latestSelectionContextRef.current = selectionContextKey;
  const reconciledSelectionContextRef = useRef(selectionContextKey);
  const selectionContextIsCurrent =
    reconciledSelectionContextRef.current === selectionContextKey;
  const stateWorkspaceIsCurrent = Boolean(
    state?.workspaceId &&
      (!selectedWorkspaceId || state.workspaceId === selectedWorkspaceId),
  );
  const linkedFindingMatchesState = !linkedFindingId || Boolean(
    state?.loadedFindingId === linkedFindingId,
  );
  const mutationContextReady =
    stateWorkspaceIsCurrent && selectionContextIsCurrent;
  const actionableFindingIds = useMemo(
    () =>
      new Set(
        (state?.findings ?? [])
          .filter(
            (finding) =>
              finding.status === "open" || finding.status === "acknowledged",
          )
          .map((finding) => finding.id),
      ),
    [state?.findings],
  );
  const activeSelectedFindingIds = useMemo(() => {
    if (!mutationContextReady) return new Set<string>();
    return new Set(
      Array.from(selectedFindingIds).filter((id) =>
        actionableFindingIds.has(id),
      ),
    );
  }, [actionableFindingIds, mutationContextReady, selectedFindingIds]);

  useEffect(() => {
    if (!searchParams.has("page_size")) return;
    const next = new URLSearchParams(searchParams);
    next.delete("page_size");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  useEffect(() => {
    if (reconciledSelectionContextRef.current === selectionContextKey) return;
    reconciledSelectionContextRef.current = selectionContextKey;
    setSelectedFindingIds(new Set());
    setBatchDismiss(false);
    setDismissFindingId(null);
    setFixFindingId(null);
    setDismissReason("");
    setMutationAnnouncement("");
  }, [selectionContextKey]);

  useEffect(() => {
    if (!mutationContextReady) return;
    setSelectedFindingIds((previous) => {
      const next = new Set(
        Array.from(previous).filter((id) => actionableFindingIds.has(id)),
      );
      if (
        next.size === previous.size &&
        Array.from(next).every((id) => previous.has(id))
      ) {
        return previous;
      }
      return next;
    });
  }, [actionableFindingIds, mutationContextReady]);

  useEffect(() => {
    if (
      !linkedFindingId ||
      !state?.findings.some((item) => item.id === linkedFindingId)
    )
      return;
    const frame = window.requestAnimationFrame(() => {
      document
        .getElementById(`lint-finding-${linkedFindingId}`)
        ?.scrollIntoView({
          block: "center",
        });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [linkedFindingId, state?.findings]);

  useEffect(
    () => () => {
      pollGeneration.current += 1;
    },
    [],
  );

  async function loadState(
    filters: {
      severity: SeverityFilter;
      layer: LayerFilter;
      status: StatusFilter;
      view: ViewFilter;
      page: number;
      pageSize: number;
    },
    workspaceIdHint?: string,
    findingId?: string | null,
  ): Promise<LintState> {
    const workspaceId = workspaceIdHint ?? routeWorkspaceId;
    if (!workspaceId) throw new Error(t("errors.workspaceNotFound"));
    if (filters.view === "coverage" && !findingId) {
      const data = await listCoverageGaps(workspaceId, {
        status: filters.status === "open" ? "open" : "resolved",
      });
      return {
        workspaceId,
        loadedFindingId: null,
        findings: [],
        treeTargets: {},
        findingsTotal: 0,
        gaps: data.items,
        gapsHasMore: data.has_more,
        page: filters.page,
        pageSize: filters.pageSize,
        pageCount: 0,
        hasPrevious: false,
        hasNext: false,
      };
    }
    const requestedPage = findingId ? 1 : filters.page;
    const result = await listLintFindings(
      workspaceId,
      findingId
        ? {
            finding_id: findingId,
            page: requestedPage,
            page_size: filters.pageSize,
          }
        : {
            status: filters.status,
            ...(filters.severity !== "all"
              ? { severity: filters.severity }
              : {}),
            ...(filters.layer !== "all" ? { layer: filters.layer } : {}),
            page: requestedPage,
            page_size: filters.pageSize,
          },
    );
    if (findingId && result.findings.length === 0) {
      throw new LintFindingNotFoundError();
    }
    const treeTargets: Record<string, TreeNodePayload> = {};
    const treeTargetIds = new Set(result.findings.flatMap((finding) =>
      finding.target_type === "workspace_tree_node" && finding.target_id
        ? [finding.target_id]
        : [],
    ));
    if (treeTargetIds.size > 0) {
      try {
        const treeData = await listTreeData(workspaceId, undefined, {
          includeArchived: true,
        });
        for (const node of treeData.nodes) {
          if (treeTargetIds.has(node.id)) treeTargets[node.id] = node;
        }
      } catch {
        // An inaccessible or deleted node has no safe section to link to.
      }
    }
    const resultPageSize = result.page_size ?? filters.pageSize;
    const total = result.total ?? result.findings.length;
    const pageCount =
      result.page_count ??
      (total === 0 ? 0 : Math.ceil(total / resultPageSize));
    return {
      workspaceId,
      loadedFindingId: findingId ?? null,
      findings: result.findings,
      treeTargets,
      findingsTotal: total,
      gaps: [],
      gapsHasMore: false,
      page: result.page ?? requestedPage,
      pageSize: resultPageSize,
      pageCount,
      hasPrevious:
        result.has_previous ?? (requestedPage > 1 && pageCount > 0),
      hasNext: result.has_next ?? requestedPage < pageCount,
    };
  }

  async function reload(
    workspaceIdHint?: string,
    expectedContextKey?: string,
  ): Promise<LintState> {
    const nextState = await loadState(
      { severity, layer, status, view, page, pageSize },
      workspaceIdHint ?? selectedWorkspaceId ?? state?.workspaceId,
      linkedFindingId,
    );
    if (
      !expectedContextKey ||
      latestSelectionContextRef.current === expectedContextKey
    ) {
      setState(nextState);
    }
    return nextState;
  }

  useEffect(() => {
    if (
      linkedFindingId ||
      view !== "findings" ||
      !stateWorkspaceIsCurrent ||
      state?.page !== page
    ) {
      return;
    }
    const lastAvailablePage = Math.max(state.pageCount, 1);
    if (page <= lastAvailablePage) return;
    const next = new URLSearchParams(searchParams);
    if (lastAvailablePage === 1) next.delete("page");
    else next.set("page", String(lastAvailablePage));
    setSearchParams(next, { replace: true });
  }, [
    linkedFindingId,
    page,
    searchParams,
    setSearchParams,
    state?.page,
    state?.pageCount,
    stateWorkspaceIsCurrent,
    view,
  ]);

  function schedulePostMutationFocus(
    nextState: LintState,
    fallbackIndex: number,
    preferredIds: string[] = [],
  ) {
    const visibleIds = new Set(nextState.findings.map((finding) => finding.id));
    const preferredId = preferredIds.find((id) => visibleIds.has(id));
    const fallbackFinding =
      nextState.findings[
        Math.min(Math.max(fallbackIndex, 0), nextState.findings.length - 1)
      ];
    const focusId = preferredId ?? fallbackFinding?.id;
    window.requestAnimationFrame(() => {
      // The card itself is the focus target: the details disclosure that used
      // to carry `lint-finding-focus-*` is gone, and the card is the only
      // element guaranteed to exist for every finding (a viewer sees no
      // checkbox and no action menu).
      const focusTarget = focusId
        ? document.getElementById(`lint-finding-${focusId}`)
        : null;
      if (focusTarget instanceof HTMLElement) {
        focusTarget.focus();
      } else {
        findingsHeadingRef.current?.focus();
      }
    });
  }

  async function triggerRun() {
    if (!state?.workspaceId || !mutationContextReady || actionBusy) return;
    const workspaceId = state.workspaceId;
    const operationContextKey = selectionContextKey;
    setActionBusy(true);
    setError(null);
    try {
      const run = await createLintRun(workspaceId, {});
      if (latestSelectionContextRef.current !== operationContextKey) return;
      setRunStatus(run.status);
      await reload(workspaceId, operationContextKey);
      const generation = ++pollGeneration.current;
      void pollRun(workspaceId, run.id, generation);
    } catch (exc) {
      setError(apiErrorMessage(exc, t("errors.run")));
    } finally {
      setActionBusy(false);
    }
  }

  async function pollRun(
    workspaceId: string,
    runId: string,
    generation: number,
  ) {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1_000));
      if (pollGeneration.current !== generation) return;
      try {
        const run = await getLintRun(workspaceId, runId);
        if (run.status === "completed" || run.status === "failed") {
          // The run is over: the findings list below is the result, so the
          // progress banner has nothing left to say — unless it failed, which
          // otherwise disappeared silently and left the old findings standing.
          setRunStatus(null);
          if (run.status === "failed") {
            setError(t("errors.run"));
          }
          reloadLatest();
          return;
        }
        setRunStatus(run.status);
      } catch {
        continue;
      }
    }
    if (pollGeneration.current === generation) reloadLatest();
  }

  function setFilterParam(
    key: "view" | "status" | "layer" | "severity",
    value: string,
  ) {
    const next = new URLSearchParams(searchParams);
    const defaults = {
      view: "findings",
      status: "open",
      layer: "all",
      severity: "all",
    } as const;
    if (value === defaults[key]) next.delete(key);
    else next.set(key, value);
    if (linkedFindingId && key !== "view") next.delete("view");
    next.delete("finding");
    next.delete("page");
    setSelectedFindingIds(new Set());
    setSearchParams(next);
  }

  function clearLinkedFinding() {
    if (!linkedFindingId) return;
    const next = new URLSearchParams(searchParams);
    setSelectedFindingIds(new Set());
    navigate(withWorkspaceQuery(workspacePath(routeWorkspaceId, "lint"), next));
  }

  function resetFilters() {
    const next = new URLSearchParams(searchParams);
    // `view` is a tab, not a filter — resetting the filter panel must not
    // yank the reader back out of Coverage gaps.
    for (const key of ["status", "layer", "severity", "page"]) {
      next.delete(key);
    }
    setSelectedFindingIds(new Set());
    setSearchParams(next);
  }

  function goToPage(nextPage: number) {
    if (nextPage < 1 || nextPage === page) return;
    const next = new URLSearchParams(searchParams);
    if (nextPage === 1) next.delete("page");
    else next.set("page", String(nextPage));
    setSelectedFindingIds(new Set());
    setSearchParams(next);
  }

  async function decide(
    findingId: string,
    decision: "acknowledge" | "dismiss" | "fix",
    reason: string | null = null,
  ) {
    if (
      !state?.workspaceId ||
      !mutationContextReady ||
      busyFindingIds.has(findingId)
    )
      return;
    const workspaceId = state.workspaceId;
    const operationContextKey = selectionContextKey;
    const fallbackIndex = Math.max(
      state.findings.findIndex((finding) => finding.id === findingId),
      0,
    );
    setBusyFindingIds((prev) => new Set(prev).add(findingId));
    setError(null);
    setMutationAnnouncement("");
    try {
      await decideLintFinding(workspaceId, findingId, decision, reason);
      if (latestSelectionContextRef.current !== operationContextKey) return;
      const nextState = await reload(workspaceId, operationContextKey);
      if (latestSelectionContextRef.current !== operationContextKey) return;
      if (decision === "dismiss") {
        setDismissFindingId(null);
        setDismissReason("");
      }
      if (decision === "fix") {
        setFixFindingId(null);
      }
      setSelectedFindingIds((prev) => {
        const next = new Set(prev);
        next.delete(findingId);
        return next;
      });
      setMutationAnnouncement(t(`mutation.${decision}Success`));
      schedulePostMutationFocus(nextState, fallbackIndex, [findingId]);
    } catch (exc) {
      setError(apiErrorMessage(exc, t("errors.decide")));
    } finally {
      setBusyFindingIds((prev) => {
        const next = new Set(prev);
        next.delete(findingId);
        return next;
      });
    }
  }

  async function decideBatch(
    decision: "acknowledge" | "dismiss",
    reason: string | null = null,
  ) {
    if (
      !state?.workspaceId ||
      !mutationContextReady ||
      activeSelectedFindingIds.size === 0 ||
      Array.from(activeSelectedFindingIds).some((id) => busyFindingIds.has(id))
    )
      return;
    const workspaceId = state.workspaceId;
    const operationContextKey = selectionContextKey;
    const ids = Array.from(activeSelectedFindingIds).sort();
    const selectedIndexes = state.findings
      .map((finding, index) =>
        activeSelectedFindingIds.has(finding.id) ? index : -1,
      )
      .filter((index) => index >= 0);
    const fallbackIndex =
      selectedIndexes.length > 0 ? Math.min(...selectedIndexes) : 0;
    const successIds = new Set<string>();
    const failedIds = new Set<string>();
    setBusyFindingIds((prev) => new Set([...prev, ...ids]));
    setError(null);
    setMutationAnnouncement("");
    try {
      for (let offset = 0; offset < ids.length; offset += LINT_BATCH_MAX_IDS) {
        if (latestSelectionContextRef.current !== operationContextKey) return;
        const chunk = ids.slice(offset, offset + LINT_BATCH_MAX_IDS);
        try {
          const result = await decideLintFindingsBatch(workspaceId, {
            finding_ids: chunk,
            decision,
            reason: reason || undefined,
          });
          const chunkIds = new Set(chunk);
          const chunkUpdatedIds = new Set(
            result.updated
              .map((finding) => finding.id)
              .filter((id) => chunkIds.has(id)),
          );
          const chunkFailedIds = new Set(
            result.failed
              .map((failure) => failure.id)
              .filter((id) => chunkIds.has(id)),
          );
          chunk.forEach((id) => {
            if (chunkFailedIds.has(id)) {
              failedIds.add(id);
              successIds.delete(id);
            } else if (chunkUpdatedIds.has(id)) {
              successIds.add(id);
            } else {
              failedIds.add(id);
            }
          });
        } catch {
          chunk.forEach((id) => failedIds.add(id));
        }
      }
      if (latestSelectionContextRef.current !== operationContextKey) return;
      setSelectedFindingIds((prev) => {
        const next = new Set(prev);
        successIds.forEach((id) => next.delete(id));
        failedIds.forEach((id) => next.add(id));
        return next;
      });
      if (failedIds.size > 0) {
        setError(
          t("errors.batchPartialFailure", {
            count: failedIds.size,
            defaultValue: `Failed to update ${failedIds.size} findings.`,
          }),
        );
      }
      if (decision === "dismiss" && batchDismiss) {
        setBatchDismiss(false);
        setDismissReason("");
      }
      const nextState = await reload(workspaceId, operationContextKey);
      if (latestSelectionContextRef.current !== operationContextKey) return;
      setMutationAnnouncement(
        t("mutation.batchResult", {
          succeeded: successIds.size,
          failed: failedIds.size,
        }),
      );
      schedulePostMutationFocus(
        nextState,
        fallbackIndex,
        Array.from(failedIds).sort(),
      );
    } catch (exc) {
      setError(apiErrorMessage(exc, t("errors.decide")));
    } finally {
      setBusyFindingIds((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.delete(id));
        return next;
      });
    }
  }

  function closeDismissDialog() {
    if (batchDismiss) {
      if (
        activeSelectedFindingIds.size === 0 ||
        Array.from(activeSelectedFindingIds).every(
          (id) => !busyFindingIds.has(id),
        )
      ) {
        setBatchDismiss(false);
      }
    } else if (dismissFindingId && !busyFindingIds.has(dismissFindingId)) {
      setDismissFindingId(null);
    }
  }
  function closeFixDialog() {
    if (!fixFindingId || !busyFindingIds.has(fixFindingId)) {
      setFixFindingId(null);
    }
  }

  const dismissFinding = state?.findings?.find(
    (f) => f.id === dismissFindingId,
  );
  const dismissRequiresReason = batchDismiss
    ? Array.from(activeSelectedFindingIds).some(
        (id) => state?.findings.find((f) => f.id === id)?.severity === "error",
      )
    : dismissFinding?.severity === "error";
  const dismissBusy =
    !mutationContextReady ||
    (batchDismiss
      ? Array.from(activeSelectedFindingIds).some((id) =>
          busyFindingIds.has(id),
        )
      : Boolean(dismissFindingId && busyFindingIds.has(dismissFindingId)));
  const fixBusy =
    !mutationContextReady ||
    Boolean(fixFindingId && busyFindingIds.has(fixFindingId));

  if (findingNotFoundKey === `${routeWorkspaceId}:${linkedFindingId}` && linkedFindingId) {
    return <NotFoundScreen />;
  }
  if (linkedFindingId && !linkedFindingMatchesState) {
    if (error) {
      return (
        <section className="feature-main">
          <StatusBanner
            tone="warning"
            action={<button className="icon-button" onClick={reloadLatest} type="button">{t("workspaceResolution.retry", { ns: "shell", defaultValue: "Retry" })}</button>}
          >
            {error}
          </StatusBanner>
        </section>
      );
    }
    return <RouteLoading label={t("loading")} />;
  }
  if (error && (!state || !stateWorkspaceIsCurrent)) {
    return (
      <section className="feature-main">
        <StatusBanner
          tone="warning"
          action={<button className="icon-button" onClick={reloadLatest} type="button">{t("workspaceResolution.retry", { ns: "shell", defaultValue: "Retry" })}</button>}
        >
          {error}
        </StatusBanner>
      </section>
    );
  }
  if (!state || !stateWorkspaceIsCurrent) return <RouteLoading label={t("loading")} />;

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main">
        <Breadcrumbs items={[{ label: t("breadcrumb") }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{t("eyebrow")}</p>
            <h1>{t("title")}</h1>
            <p className="feature-subtitle">{t("subtitle")}</p>
          </div>
          {/* The findings empty state offers the same action as a primary
              button; two weights for one action read as two actions. */}
          {canEdit &&
          (view === "coverage" || (state?.findings.length ?? 0) > 0) ? (
            <button
              className="icon-button"
              disabled={
                !mutationContextReady || actionBusy || busyFindingIds.size > 0
              }
              type="button"
              onClick={triggerRun}
            >
              <Play size={16} aria-hidden="true" />
              <span>{t("runLint")}</span>
            </button>
          ) : null}
          {fixFindingId ? (
            <Dialog
              dismissible={!fixBusy}
              labelledBy="lint-fix-title"
              onClose={closeFixDialog}
            >
              <div className="dialog-header">
                <div>
                  <p className="eyebrow">{t("actions.fix")}</p>
                  <h2 id="lint-fix-title">{t("fixDialog.title")}</h2>
                </div>
              </div>
              <p>{t("fixDialog.body")}</p>
              <div className="dialog-actions">
                <button
                  className="icon-button text-button"
                  disabled={fixBusy}
                  type="button"
                  onClick={closeFixDialog}
                >
                  {t("fixDialog.cancel")}
                </button>
                <button
                  className="icon-button primary"
                  disabled={fixBusy}
                  type="button"
                  onClick={() => void decide(fixFindingId, "fix")}
                >
                  {t("fixDialog.confirm")}
                </button>
              </div>
            </Dialog>
          ) : null}
        </div>
        {roleKnown && !canEdit ? (
          <StatusBanner tone="info">{t("readOnly")}</StatusBanner>
        ) : null}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div
          aria-atomic="true"
          aria-live="polite"
          className="visually-hidden"
          role="status"
        >
          {mutationAnnouncement}
        </div>
        {runStatus ? (
          <StatusBanner tone="info">
            {t("runStatus", {
              status: t(`runState.${runStatus}`, { defaultValue: runStatus }),
            })}
          </StatusBanner>
        ) : null}

        {linkedFindingId ? (
          <div className="lint-linked-banner">
            <StatusBanner tone="info">{t("linkedFinding")}</StatusBanner>
            <button
              className="icon-button text-button"
              type="button"
              onClick={clearLinkedFinding}
            >
              <X size={15} aria-hidden="true" />
              <span>{t("showAll")}</span>
            </button>
          </div>
        ) : null}
        {/* Findings vs Coverage gaps is a view switch, not a filter: it is the
            same lifecycle-tab language the Review queue uses, so it sits above
            the filters rather than inside them. */}
        <ReaderTabs
          active={view}
          ariaLabel={t("view")}
          idBase="lint-view"
          onChange={(value) => setFilterParam("view", value)}
          tabs={VIEWS.map((key) => ({ key, label: t(`viewFilter.${key}`) }))}
        />
        <section className="lint-filter-disclosure">
          <div className="lint-filter-disclosure-header">
            <button
              aria-controls="lint-filter-panel"
              aria-expanded={filtersOpen}
              className="icon-button text-button lint-filter-toggle"
              onClick={() => setFiltersOpen((current) => !current)}
              type="button"
            >
              <span>
                {t("filters.title")}
                {activeFilterCount > 0
                  ? ` (${activeFilterCount})`
                  : ""}
              </span>
              <ChevronDown aria-hidden="true" data-open={filtersOpen} size={16} />
            </button>
            {activeFilterCount > 0 ? (
              <>
                <span className="lint-active-filter-summary">
                  {t("filters.active", { count: activeFilterCount })}
                </span>
                <button
                  className="icon-button text-button"
                  onClick={resetFilters}
                  type="button"
                >
                  {t("filters.reset")}
                </button>
              </>
            ) : null}
          </div>
          <div
            className="lint-filter-grid"
            hidden={!filtersOpen}
            id="lint-filter-panel"
          >
          <LintFilterGroup label={t("status")}>
            <SegmentedControl
              compact
              label={t("status")}
              options={STATUSES.map((key) => ({
                key,
                label: t(`statusFilter.${key}`),
              }))}
              value={status}
              onChange={(value) => {
                setFilterParam("status", value);
              }}
            />
          </LintFilterGroup>
          {view === "findings" ? (
            <>
              <LintFilterGroup label={t("layer")}>
                <SegmentedControl
                  compact
                  label={t("layer")}
                  options={LAYERS.map((key) => ({
                    key,
                    label: t(`layerFilter.${key}`),
                  }))}
                  value={layer}
                  onChange={(value) => {
                    setFilterParam("layer", value);
                  }}
                />
              </LintFilterGroup>
              <LintFilterGroup label={t("severity")}>
                <SegmentedControl
                  compact
                  label={t("severity")}
                  options={SEVERITIES.map((key) => ({
                    key,
                    label: t(`severityFilter.${key}`),
                  }))}
                  value={severity}
                  onChange={(value) => {
                    setFilterParam("severity", value);
                  }}
                />
              </LintFilterGroup>
            </>
          ) : null}
          </div>
        </section>

        {view === "coverage" ? (
          <CoverageGapList gaps={state.gaps} hasMore={state.gapsHasMore} />
        ) : (
          <FindingList
            findings={state.findings}
            treeTargets={state.treeTargets}
            total={state.findingsTotal}
            canEdit={canEdit}
            mutationContextReady={mutationContextReady}
            actionBusy={actionBusy}
            busyFindingIds={busyFindingIds}
            decide={decide}
            decideBatch={decideBatch}
            onDismiss={setDismissFindingId}
            onFix={setFixFindingId}
            onRunLint={() => void triggerRun()}
            status={status}
            linkedFindingId={linkedFindingId}
            selectedFindingIds={activeSelectedFindingIds}
            setSelectedFindingIds={setSelectedFindingIds}
            setBatchDismiss={setBatchDismiss}
            selectAllCheckboxRef={selectAllCheckboxRef}
            findingsHeadingRef={findingsHeadingRef}
          />
        )}
        {view === "findings" && !linkedFindingId && state.findingsTotal > 0 ? (
          <NumericPagination
            labels={{
              navigation: t("pagination.navigation"),
              previous: t("pagination.previous"),
              next: t("pagination.next"),
              page: (value) => t("pagination.page", { page: value }),
              currentPage: (value) =>
                t("pagination.currentPage", { page: value }),
              perPage: t("pagination.perPage"),
              range: (start, end, total) =>
                t("pagination.range", { start, end, total }),
            }}
            onPageChange={goToPage}
            onPageSizeChange={() => undefined}
            page={state.page}
            pageCount={state.pageCount}
            pageSize={state.pageSize}
            pageSizeOptions={[LINT_PAGE_SIZE]}
            total={state.findingsTotal}
          />
        ) : null}
      </section>
      {dismissFindingId || batchDismiss ? (
        <Dialog
          dismissible={!dismissBusy}
          labelledBy="lint-dismiss-title"
          onClose={closeDismissDialog}
        >
          <div className="dialog-header">
            <div>
              <p className="eyebrow">{t("actions.dismiss")}</p>
              <h2 id="lint-dismiss-title">{t("dismissDialog.title")}</h2>
            </div>
          </div>
          <p>{t("dismissDialog.body")}</p>
          <label className="field-stack">
            <span>
              {dismissRequiresReason
                ? t("dismissDialog.reason")
                : t("dismissDialog.reasonOptional")}
            </span>
            <textarea
              disabled={dismissBusy}
              value={dismissReason}
              onChange={(event) => setDismissReason(event.target.value)}
            />
          </label>
          <div className="dialog-actions">
            <button
              className="icon-button text-button"
              disabled={dismissBusy}
              type="button"
              onClick={closeDismissDialog}
            >
              {t("dismissDialog.cancel")}
            </button>
            <button
              className="icon-button danger"
              disabled={
                dismissBusy || (dismissRequiresReason && !dismissReason.trim())
              }
              type="button"
              onClick={() =>
                batchDismiss
                  ? void decideBatch("dismiss", dismissReason.trim() || null)
                  : void decide(
                      dismissFindingId!,
                      "dismiss",
                      dismissReason.trim() || null,
                    )
              }
            >
              {t("dismissDialog.confirm")}
            </button>
          </div>
        </Dialog>
      ) : null}
    </div>
  );
}

function LintFilterGroup({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="lint-filter-group">
      <span className="eyebrow">{label}</span>
      {children}
    </div>
  );
}

/**
 * Severity is a labelled pill: the card surface stays neutral, so the queue
 * does not read as a wall of tinted panels. Colour is never the only signal —
 * the glyph shape and the word itself both carry it.
 */
function SeverityIcon({
  severity,
  label,
}: {
  severity: string;
  label: string;
}) {
  let icon;
  if (severity === "error")
    icon = <XCircle size={16} className="color-danger" aria-hidden="true" />;
  else if (severity === "warning")
    icon = (
      <AlertTriangle size={16} className="color-warning" aria-hidden="true" />
    );
  else if (severity === "info")
    icon = <Info size={16} className="color-info" aria-hidden="true" />;
  else icon = <CheckCircle2 size={16} aria-hidden="true" />;

  return (
    <span className="lint-severity-badge">
      {icon}
      <span className="lint-severity-badge__label">{label}</span>
    </span>
  );
}

function FindingList({
  findings,
  treeTargets,
  total,
  canEdit,
  mutationContextReady,
  actionBusy,
  busyFindingIds,
  decide,
  decideBatch,
  onDismiss,
  onFix,
  onRunLint,
  status,
  linkedFindingId,
  selectedFindingIds,
  setSelectedFindingIds,
  setBatchDismiss,
  selectAllCheckboxRef,
  findingsHeadingRef,
}: {
  findings: LintFinding[];
  treeTargets: Record<string, TreeNodePayload>;
  total: number;
  canEdit: boolean;
  mutationContextReady: boolean;
  actionBusy: boolean;
  busyFindingIds: Set<string>;
  onRunLint: () => void;
  decide: (
    id: string,
    decision: "acknowledge" | "dismiss" | "fix",
  ) => Promise<void>;
  decideBatch: (
    decision: "acknowledge" | "dismiss",
    reason?: string | null,
  ) => Promise<void>;
  onDismiss: (id: string) => void;
  onFix: (id: string) => void;
  status: StatusFilter;
  linkedFindingId: string | null;
  selectedFindingIds: Set<string>;
  setSelectedFindingIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  setBatchDismiss: (b: boolean) => void;
  selectAllCheckboxRef: React.MutableRefObject<HTMLInputElement | null>;
  findingsHeadingRef: React.MutableRefObject<HTMLSpanElement | null>;
}) {
  const routeWorkspaceId = useRouteWorkspaceId();
  const { t } = useTranslation("lint");
  const actionableFindings = findings.filter(
    (f) => f.status === "open" || f.status === "acknowledged",
  );
  const allActionableSelected =
    actionableFindings.length > 0 &&
    actionableFindings.every((f) => selectedFindingIds.has(f.id));

  function toggleSelectAll() {
    if (!mutationContextReady) return;
    if (allActionableSelected) {
      setSelectedFindingIds(new Set());
    } else {
      setSelectedFindingIds(new Set(actionableFindings.map((f) => f.id)));
    }
  }

  function toggleRowSelection(id: string) {
    if (!mutationContextReady) return;
    setSelectedFindingIds((prev: Set<string>) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <section className="feature-section">
      <div className="panel-header">
        <div className="panel-title-with-count">
          <span
            aria-level={2}
            ref={findingsHeadingRef}
            role="heading"
            tabIndex={-1}
          >
            {t("findings")}
          </span>
          <CountChip value={total} compact />
        </div>
      </div>
      {findings.length === 0 ? (
        <div className="empty-state empty-state-rich">
          <ClipboardCheck size={22} aria-hidden="true" />
          <h2>{t(`empty.${status}`)}</h2>
          <p>
            {t("empty.hint", {
              defaultValue:
                "Run lint to check this workspace against the rules enabled in Settings.",
            })}
          </p>
          {canEdit ? (
            <div className="empty-state-actions">
              <button
                className="icon-button primary"
                disabled={actionBusy || !mutationContextReady}
                onClick={onRunLint}
                type="button"
              >
                <Play size={16} aria-hidden="true" />
                <span>{t("runLint")}</span>
              </button>
            </div>
          ) : null}
        </div>
      ) : (
        <div className="review-list">
          {canEdit && actionableFindings.length > 0 && (
            <div
              className="bulk-toolbar row-badges lint-bulk-toolbar"
              role="toolbar"
              aria-label={t("actions.bulkActions")}
            >
              <label className="lint-bulk-label">
                <input
                  type="checkbox"
                  checked={allActionableSelected}
                  ref={(el) => {
                    selectAllCheckboxRef.current = el;
                    if (el)
                      el.indeterminate =
                        selectedFindingIds.size > 0 && !allActionableSelected;
                  }}
                  onChange={toggleSelectAll}
                  disabled={
                    actionBusy ||
                    !mutationContextReady ||
                    busyFindingIds.size > 0
                  }
                  aria-label={t("actions.selectAll", {
                    count: actionableFindings.length,
                  })}
                />
                <span>
                  {selectedFindingIds.size > 0
                    ? t("actions.selectedCount", {
                        count: selectedFindingIds.size,
                        defaultValue: `${selectedFindingIds.size} selected`,
                      })
                    : t("actions.selectAll", {
                        count: actionableFindings.length,
                      })}
                </span>
              </label>
              <div className="lint-bulk-spacer" />
              {selectedFindingIds.size > 0 && (
                <div className="row-badges">
                  <button
                    className="icon-button"
                    type="button"
                    disabled={
                      actionBusy ||
                      !mutationContextReady ||
                      Array.from(selectedFindingIds).some((id) =>
                        busyFindingIds.has(id),
                      )
                    }
                    onClick={() => decideBatch("acknowledge")}
                  >
                    <Check size={15} aria-hidden="true" />
                    <span>{t("actions.acknowledge")}</span>
                  </button>
                  <button
                    className="icon-button"
                    type="button"
                    disabled={
                      actionBusy ||
                      !mutationContextReady ||
                      Array.from(selectedFindingIds).some((id) =>
                        busyFindingIds.has(id),
                      )
                    }
                    onClick={() => setBatchDismiss(true)}
                  >
                    <X size={15} aria-hidden="true" />
                    <span>{t("actions.dismiss")}</span>
                  </button>
                </div>
              )}
              <div role="status" aria-live="polite" className="visually-hidden">
                {actionBusy
                  ? t("loadingMore")
                  : selectedFindingIds.size > 0
                    ? t("actions.selectedCount", {
                        count: selectedFindingIds.size,
                        defaultValue: `${selectedFindingIds.size} selected`,
                      })
                    : ""}
              </div>
            </div>
          )}
          {findings.map((finding) => {
            const treeTarget = finding.target_id
              ? treeTargets[finding.target_id]
              : undefined;
            const href = treeTarget
              ? treeNodeHref(routeWorkspaceId, treeTarget)
              : finding.target_id && finding.target_type !== "workspace_tree_node"
              ? searchResultHref({
                  id: finding.target_id,
                  type: finding.target_type,
                }, routeWorkspaceId)
              : null;
            const actionable =
              finding.status === "open" || finding.status === "acknowledged";
            const targetLabel =
              finding.target_title ??
              t(`targetType.${finding.target_type}`, {
                defaultValue: labelFor(finding.target_type),
              });
            const message = lintFindingMessage(t, finding);
            const ruleLabel = lintRuleLabel(t, finding.rule_id);
            const isSelected = selectedFindingIds.has(finding.id);
            const isBusy = busyFindingIds.has(finding.id);
            // `suggested_action` carries two shapes: the deterministic rules
            // write a plain sentence with the exact location ("...at line 3"),
            // while the maintenance/AI paths write JSON. Both are worth
            // showing; only the structured one needs field-by-field rendering.
            const structuredAction = parseSuggestedAction(
              finding.suggested_action,
            );
            // When the message already interpolates the finding's specifics
            // the sentence would only restate it; when it states the rule's
            // general condition, the sentence carries the exact location.
            const plainAction =
              !structuredAction &&
              !lintMessageIsSpecific(t, finding) &&
              finding.suggested_action?.trim()
                ? finding.suggested_action.trim()
                : null;

            return (
              <article
                className={`lint-finding-card${finding.id === linkedFindingId ? " is-linked-target" : ""}${isBusy ? " is-busy" : ""}`}
                data-severity={finding.severity}
                id={`lint-finding-${finding.id}`}
                key={finding.id}
                tabIndex={-1}
              >
                <div className="lint-finding-card__main">
                  {canEdit && actionable ? (
                    <label className="lint-row-selector">
                      <input
                        type="checkbox"
                        aria-label={t("actions.selectRow", {
                          title: ruleLabel,
                          target: targetLabel,
                          defaultValue: `Select ${ruleLabel} on ${targetLabel}`,
                        })}
                        disabled={isBusy || !mutationContextReady}
                        checked={isSelected}
                        onChange={() => toggleRowSelection(finding.id)}
                        className="lint-row-checkbox"
                      />
                    </label>
                  ) : null}
                  <div className="lint-finding-body">
                    <p className="lint-finding-message">
                      <SeverityIcon
                        severity={finding.severity}
                        label={t(`severityFilter.${finding.severity}`, {
                          defaultValue: finding.severity,
                        })}
                      />
                      <span>{message}</span>
                    </p>
                    {plainAction ? (
                      <p className="lint-finding-detail">{plainAction}</p>
                    ) : null}
                    <div className="row-badges lint-finding-chips">
                      {/* Rule and target share one bordered box split by a
                          hairline, so "this rule, on this page" reads as a
                          single unit instead of two chips whose heights and
                          corner radii never agreed. */}
                      <span className="lint-source-chip">
                        <span className="lint-rule-chip">{ruleLabel}</span>
                        {href ? (
                          <Link
                            className="lint-target-chip"
                            to={href}
                            aria-label={t("actions.openTarget", {
                              title: targetLabel,
                              defaultValue: `Open target ${targetLabel}`,
                            })}
                          >
                            <ExternalLink size={13} aria-hidden="true" />
                            <span>{targetLabel}</span>
                          </Link>
                        ) : (
                          <span className="lint-target-chip is-static">
                            {targetLabel}
                          </span>
                        )}
                      </span>
                      {/* Every row in the open queue is "Open", so the pill
                          only earns its place once the status is something
                          the reader has not already inferred from the tab. */}
                      {finding.status === "open" ? null : (
                        <StatusPill status={finding.status} compact />
                      )}
                    </div>
                  </div>
                  {canEdit && actionable ? (
                    <ActionMenu
                      disabled={isBusy || !mutationContextReady}
                      items={[
                        ...(finding.status === "open"
                          ? [
                              {
                                key: "acknowledge",
                                label: t("actions.acknowledge"),
                                icon: <Check size={16} aria-hidden="true" />,
                                onSelect: () =>
                                  decide(finding.id, "acknowledge"),
                              },
                            ]
                          : []),
                        {
                          key: "fix",
                          label: t("actions.fix"),
                          icon: <Wrench size={16} aria-hidden="true" />,
                          onSelect: () => onFix(finding.id),
                        },
                        {
                          key: "dismiss",
                          label: t("actions.dismiss"),
                          icon: <X size={16} aria-hidden="true" />,
                          onSelect: () => onDismiss(finding.id),
                          tone: "danger" as const,
                        },
                      ]}
                      label={t("actions.more", {
                        title: ruleLabel,
                        target: targetLabel,
                      })}
                    />
                  ) : null}
                </div>
                {structuredAction ? (
                  <div className="lint-finding-details-panel">
                    <strong>{t("details.suggestedAction")}</strong>
                    <SuggestedAction value={structuredAction} />
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function CoverageGapList({
  gaps,
  hasMore,
}: {
  gaps: CoverageGap[];
  hasMore: boolean;
}) {
  const { t } = useTranslation("lint");
  const routeWorkspaceId = useRouteWorkspaceId();
  return (
    <section className="feature-section">
      <div className="panel-header">
        <div className="panel-title-with-count">
          <span>{t("coverage.title")}</span>
          <CountChip value={gaps.length} approxMore={hasMore} compact />
        </div>
      </div>
      {gaps.length === 0 ? (
        <div className="empty-state empty-state-rich">
          <h2>{t("coverage.empty")}</h2>
        </div>
      ) : (
        <div className="review-list">
          {/* Same card language as the findings list: the two views are one
              queue, and the shared `.review-row` grid made them read as two
              different products. */}
          {gaps.map((gap) => (
            <article className="lint-finding-card" key={gap.id}>
              <div className="lint-finding-card__main">
                <div className="lint-finding-body">
                  <p className="lint-finding-message">
                    <span>{gap.query_norm}</span>
                  </p>
                  <p className="lint-finding-detail">
                    {(gap.tried_lanes ?? [])
                      .map((lane) =>
                        t(`lane.${lane}`, { defaultValue: labelFor(lane) }),
                      )
                      .join(" / ")}
                  </p>
                  <div className="row-badges lint-finding-chips">
                    <span className="lint-source-chip">
                      <span className="lint-rule-chip">
                        {t(`surface.${gap.surface}`, {
                          defaultValue: labelFor(gap.surface),
                        })}
                      </span>
                      {gap.finding_id ? (
                        <Link
                          className="lint-target-chip"
                          to={workspacePath(routeWorkspaceId, `lint/findings/${encodeURIComponent(gap.finding_id)}`)}
                        >
                          <ExternalLink size={13} aria-hidden="true" />
                          <span>{t("coverage.openFinding")}</span>
                        </Link>
                      ) : null}
                    </span>
                    <StatusPill status={gap.status} compact />
                    <CountChip
                      value={gap.hit_count}
                      label={t("coverage.hits", { count: gap.hit_count })}
                    />
                  </div>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * `suggested_action` is a dual-format column. Every deterministic rule
 * (rules_format / rules_links / rules_knowledge / rules_nav) writes a plain
 * sentence into it; only the maintenance and AI paths write JSON. Parsing it
 * as JSON unconditionally threw on the plain-sentence case and replaced the
 * one genuinely useful line ("Unclosed code fence opened at line 10.") with a
 * notice saying nothing was available. Return null for anything that is not a
 * JSON object so the caller can render the sentence as-is.
 */
function parseSuggestedAction(
  value: string | null | undefined,
): Record<string, unknown> | null {
  if (!value) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return null;
  }
  const record = parsed as Record<string, unknown>;
  return suggestedEntries(record).length > 0 ? record : null;
}

function SuggestedAction({ value }: { value: Record<string, unknown> }) {
  const { t } = useTranslation("lint");
  const entries = suggestedEntries(value);
  const aiSuggested =
    value.source === "client_llm" || value.client_llm_verdict != null;
  return (
    <div className="suggested-action">
      {aiSuggested ? (
        <span className="suggestion-source-badge">
          {t("suggestedAction.aiBadge")}
        </span>
      ) : null}
      {entries.map(([key, entry]) => (
        <div className="suggested-action-field" key={key}>
          <strong>
            {t(`suggestedAction.fields.${key}`, {
              defaultValue: labelFor(key),
            })}
            :
          </strong>
          <SuggestedValue value={entry} />
        </div>
      ))}
    </div>
  );
}

const DISPLAY_ACTION_FIELDS = new Set([
  "proposal",
  "verdict",
  "confidence",
  "rationale",
  "reason",
  "anchor",
  "nav_summary",
  "negative_cues",
  "aliases",
  "nav_description",
  "placement_hint",
  "title",
  "page_type",
  "outline",
  "concept_path",
  "queries",
  "similarity",
  "session_count",
  "tree_distance",
  "supported",
  "contradicts",
  "client_llm_verdict",
]);

function suggestedEntries(value: Record<string, unknown>) {
  return Object.entries(value).filter(([key]) =>
    DISPLAY_ACTION_FIELDS.has(key),
  );
}

function SuggestedValue({ value }: { value: unknown }) {
  const { t } = useTranslation("lint");
  if (Array.isArray(value)) {
    return (
      <ul>
        {value.map((item, index) => (
          <li key={index}>
            <SuggestedValue value={item} />
          </li>
        ))}
      </ul>
    );
  }
  if (value && typeof value === "object") {
    const entries = suggestedEntries(value as Record<string, unknown>);
    return (
      <div>
        {entries.map(([key, entry]) => (
          <div key={key}>
            <strong>
              {t(`suggestedAction.fields.${key}`, {
                defaultValue: labelFor(key),
              })}
              :
            </strong>{" "}
            <SuggestedValue value={entry} />
          </div>
        ))}
      </div>
    );
  }
  return <span>{String(value ?? "")}</span>;
}

function labelFor(value: string) {
  return value.replace(/_/g, " ");
}

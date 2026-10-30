import {
  ChevronRight,
  Bot,
  CheckSquare,
  FileText,
  Library,
} from "lucide-react";
import { useCallback } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useSearchParams } from "react-router-dom";

import {
  getSettingsPolicies,
  listActivityFeed,
  listRecentReviewDecisions,
  listReviews,
} from "../../shared/api/operations";
import type {
  ActivityFeedEntry,
  ActivityFeedData,
  ActivityFeedEvent,
  ActivityPeriod,
  ReviewItem,
  SettingsPolicies,
} from "../../shared/api/operations";
import { listContextPages } from "../../shared/api/context";
import { listLibraryItems } from "../../shared/api/library";
import { useRouteWorkspaceId, workspacePath } from "../../shared/lib/workspaceRoutes";
import { ActivityEntryRow } from "../../shared/components/ActivityEntryRow";
import { ActivityGroupingControl } from "../../shared/components/ActivityGroupingControl";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import {
  FirstUseChecklist,
  FIRST_USE_STEPS,
  firstIncompleteStep,
  firstUseStepByKey,
  type FirstUseProgress,
} from "../../shared/components/FirstUseChecklist";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { useActivityGroupLoader } from "../../shared/hooks/useActivityGroupLoader";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { useScreenEntryRefresh } from "../../shared/hooks/useScreenEntryRefresh";
import { useLabsPreferences } from "../../shared/hooks/useLabsPreferences";
import { activityCountLabel, activityEntryCounts, activityHref, parseActivityQuery, reconcileActivityReadGroup } from "../../shared/lib/activityFeed";
import { iconForActivityLane } from "../../shared/lib/activityLanes";
import { applyPeriodParam, parsePeriodParam } from "../../shared/lib/urlState";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { isEditorRole } from "../../shared/workspace/permissions";

const DASHBOARD_PERIODS: ActivityPeriod[] = ["today", "week", "month", "all"];
// The server completes read groups before budgeting display entries. The card
// previews eight entries and labels the original events they represent separately.
const DASHBOARD_ACTIVITY_FETCH_LIMIT = 50;
const DASHBOARD_RECENT_DISPLAY_LIMIT = 8;

type DashboardState = {
  workspaceId: string;
  queryKey: string;
  activity: ActivityFeedEntry[];
  activityLaneCounts: ActivityFeedData["lanes"] | null;
  systemActivityCount: number;
  reviews: ReviewItem[];
  reviewsHasMore: boolean;
  policies: SettingsPolicies;
  setupProgress: FirstUseProgress;
};

export function DashboardScreen() {
  const routeWorkspaceId = useRouteWorkspaceId();
  const { t } = useTranslation("dashboard");
  const { preferences: labsPreferences } = useLabsPreferences();
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const period = parsePeriod(searchParams.get("period"));
  const query = { ...parseActivityQuery(searchParams, { period: "today", kind: "workspace" }), period, kind: "workspace" as const, subtype: "all" };
  const groupReads = query.group_reads;
  const queryKey = `${period}:${groupReads}`;
  const welcome = searchParams.get("welcome") === "1";
  // A viewer bounced off an editor-gated route lands here carrying the reason;
  // surface it so the redirect is explained instead of silently swallowed.
  const editorRequired = Boolean(
    (location.state as { editorRequired?: boolean } | null)?.editorRequired,
  );
  // Viewer write-gate: the setup CTAs route to editor-gated surfaces, so they
  // are hidden for sub-editor roles. Backend RBAC still re-enforces.
  const workspaceContext = useOptionalWorkspaceContext();
  const canEdit = isEditorRole(workspaceContext?.selectedWorkspace?.role);
  const { data: state, error, loading, reload, setData } = useAsyncLoad<DashboardState>(
    async () => {
      const workspaceId = routeWorkspaceId;
      const [
        activity,
        systemActivity,
        reviews,
        reviewDecisions,
        policies,
        sources,
        contextPages,
      ] =
        await Promise.all([
          listActivityFeed(workspaceId, {
            ...query,
            limit: DASHBOARD_ACTIVITY_FETCH_LIMIT,
          }),
          listActivityFeed(workspaceId, { period, kind: "system", group_reads: groupReads, limit: 1 }),
          listReviews(workspaceId, { status: "pending", limit: 20 }),
          listRecentReviewDecisions(workspaceId, { limit: 1 }),
          getSettingsPolicies(workspaceId),
          listLibraryItems(workspaceId),
          listContextPages(workspaceId),
        ]);
      return {
        workspaceId,
        queryKey,
        activity: activity.entries,
        activityLaneCounts: activity.lanes,
        systemActivityCount: systemActivity.entries.length,
        reviews: reviews.items,
        // The pending count is fetched capped at limit:20; a next_cursor means
        // there are more pending reviews than counted (C19).
        reviewsHasMore: reviews.next_cursor != null,
        policies,
        setupProgress: {
          library: sources.data.length > 0,
          context: contextPages.data.length > 0,
          // Cumulative like the other steps: the team has used Review once any
          // decision exists. A pending-queue check would regress to incomplete
          // the moment every pending review is decided (and read "done" while
          // items merely waited).
          review: reviewDecisions.length > 0,
        },
      };
    },
    [routeWorkspaceId, period, groupReads],
    {
      errorMessage: t("errors.load", { defaultValue: "Failed to load Dashboard" }),
    },
  );

  useScreenEntryRefresh(workspacePath(routeWorkspaceId, "dashboard"), reload, { queryDeps: [routeWorkspaceId, period, groupReads] });

  function updatePeriod(nextPeriod: ActivityPeriod) {
    setSearchParams(
      applyPeriodParam(searchParams, "period", nextPeriod, "today"),
      { replace: true },
    );
  }

  const systemActivityHref = activityHref({ ...query, kind: "system" }, undefined, routeWorkspaceId);
  const workspaceActivity = state?.activity ?? [];
  const preview = workspaceActivity.slice(0, DASHBOARD_RECENT_DISPLAY_LIMIT);
  const previewCounts = activityEntryCounts(preview);
  const loadedCounts = activityEntryCounts(workspaceActivity);
  const reconcileGroup = useCallback((token: string, events: ActivityFeedEvent[]) => {
    setData(current => current?.workspaceId === routeWorkspaceId && current.queryKey === queryKey ? { ...current, activity: reconcileActivityReadGroup(current.activity, token, events) } : current);
  }, [queryKey, routeWorkspaceId, setData]);
  const loadGroup = useActivityGroupLoader(state?.workspaceId === routeWorkspaceId ? routeWorkspaceId : null, reconcileGroup, reload);
  function updateGrouping(checked: boolean) {
    const next = new URLSearchParams(searchParams);
    next.set("group_reads", checked ? "1" : "0");
    // Keep the controlled native checkbox in sync with its URL-backed value.
    setSearchParams(next, { replace: true, flushSync: true });
  }
  const hiddenSystemActivityCount = state?.systemActivityCount ?? 0;
  const setupStarted = Object.values(state?.setupProgress ?? {}).some(Boolean);
  const needsSetupGuide =
    !state?.setupProgress.library ||
    !state.setupProgress.context ||
    !state.setupProgress.review;
  // A lane count reads as a lower bound only where the server confirmed more
  // for that lane; a merely full window is described by the scope text.
  const laneMoreSuffix = (lane: "advisory" | "history") =>
    state?.activityLaneCounts?.[lane]?.has_more === true ? "+" : undefined;
  const lanes = (() => {
    // Pending reviews appear in the activity feed as one pending-lane row per
    // ReviewItem (carrying review_item_id); those rows are already counted via
    // reviews.length, so only review-less pending activity rows add here.
    const pending = workspaceActivity.filter(
      (item) => item.event.lane === "pending" && item.event.review_item_id == null,
    );
    const advisory = workspaceActivity.filter((item) => item.event.lane === "advisory");
    const history = workspaceActivity.filter((item) => item.event.lane === "history");
    // One vocabulary with the Activity trio: same titles, same descriptions,
    // same glyphs. No period suffix on the titles either — the segmented
    // control directly above already states the period, and only two of the
    // three lanes are period-scoped (Pending counts every waiting review), so a
    // suffix on part of the row would misread as scoping all of it.
    return [
      {
        id: "pending",
        title: t("lanes.pending", { defaultValue: "Pending" }),
        description: t("lanes.pendingHint", { defaultValue: "Needs review" }),
        count: (state?.reviews.length ?? 0) + pending.length,
        // The pending review count is capped at the limit:20 fetch; show "+"
        // when more pending reviews exist so the lane reads as a lower bound (C19).
        countSuffix: state?.reviewsHasMore ? "+" : undefined,
        href: workspacePath(routeWorkspaceId, "review"),
        icon: iconForActivityLane("pending"),
      },
      {
        id: "advisory",
        title: t("lanes.advisory", { defaultValue: "Advisory" }),
        description: t("lanes.advisoryHint", { defaultValue: "Needs attention" }),
        count: activityEntryCounts(advisory).represented_event_count,
        countSuffix: laneMoreSuffix("advisory"),
        href: activityHref(query, "advisory", routeWorkspaceId),
        icon: iconForActivityLane("advisory"),
      },
      {
        id: "history",
        title: t("lanes.history", { defaultValue: "History" }),
        description: t("lanes.historyHint", { defaultValue: "Completed changes" }),
        count: activityEntryCounts(history).represented_event_count,
        countSuffix: laneMoreSuffix("history"),
        href: activityHref(query, "history", routeWorkspaceId),
        icon: iconForActivityLane("history"),
      },
    ];
  })();

  if (!state || state.workspaceId !== routeWorkspaceId || state.queryKey !== queryKey) {
    if (error && !loading) return <div className="workspace-screen single-column"><section className="feature-main">
      <h1>{t("header.title", { defaultValue: "Workspace Operations" })}</h1>
      <StatusBanner tone="warning">{error}<button className="icon-button text-button activity-feed-reload" type="button" onClick={reload}>{t("activity:feed.reload")}</button></StatusBanner>
    </section></div>;
    return <RouteLoading />;
  }

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main">
        <Breadcrumbs items={[{ label: t("breadcrumb", { defaultValue: "Dashboard" }) }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{t("header.eyebrow", { defaultValue: "Workspace now" })}</p>
            <h1>{t("header.title", { defaultValue: "Workspace Operations" })}</h1>
            <p className="feature-subtitle">
              {t("header.subtitle", {
                defaultValue: "Know what readers and AI will see before anything changes.",
              })}
            </p>
          </div>
          {workspaceActivity.length > 0 ? (
            // A plain count, so it takes the count chip every other header uses
            // — StatusPill is reserved for real states (Setup's "Partial").
            <CountChip
              value={loadedCounts.returned_entry_count}
              formatted={activityCountLabel(loadedCounts.returned_entry_count, loadedCounts.represented_event_count)}
            />
          ) : null}
        </div>
        {error ? <StatusBanner tone="warning">{error}<button className="icon-button text-button activity-feed-reload" type="button" onClick={reload}>{t("activity:feed.reload")}</button></StatusBanner> : null}
        {editorRequired ? (
          <StatusBanner tone="info">
            {t("editorRequiredBanner", {
              defaultValue:
                "Editor access is required to create or edit content in this workspace.",
            })}
          </StatusBanner>
        ) : null}
        <PeriodControl period={period} onChange={updatePeriod} label={t("period.label", { defaultValue: "Dashboard activity period" })} />
        <section className="feature-section">
          <div className="metric-grid">
            {lanes.map((lane) => (
              <Link className="metric-card linked" key={lane.id} to={lane.href}>
                <div className="metric-card-title">
                  {lane.icon}
                  <span>{lane.title}</span>
                </div>
                <strong>
                  {lane.count}
                  {lane.countSuffix ?? ""}
                </strong>
                <p>{lane.description}</p>
                {lane.id !== "pending" ? <p className="activity-feed-scope">{t("activity:feed.scope", { defaultValue: "Current query range" })}</p> : null}
              </Link>
            ))}
          </div>
        </section>
        {welcome || needsSetupGuide ? (
          <DashboardWelcome
            canEdit={canEdit}
            compact={setupStarted}
            showWorkspaceDiscovery={welcome}
            setupProgress={state.setupProgress}
          />
        ) : null}
        <section className="feature-section">
          <div className="panel-header">
            <span>{t("spaces.title", { defaultValue: "Spaces" })}</span>
          </div>
          <div className="metric-grid">
            <Link className="metric-card linked" data-kind="source" to={workspacePath(routeWorkspaceId, "library")}>
              <div className="metric-card-title">
                <Library size={16} aria-hidden="true" />
                <span>{t("spaces.library", { defaultValue: "Library" })}</span>
              </div>
              <p>{t("spaces.libraryPurpose", { defaultValue: "Source material to cite" })}</p>
            </Link>
            <Link className="metric-card linked" data-kind="context" to={workspacePath(routeWorkspaceId, "context")}>
              <div className="metric-card-title">
                <FileText size={16} aria-hidden="true" />
                <span>{t("spaces.context", { defaultValue: "Context" })}</span>
              </div>
              <p>{t("spaces.contextPurpose", { defaultValue: "Durable team knowledge" })}</p>
            </Link>
            {labsPreferences.agent ? (
              <Link
                className="metric-card linked"
                data-kind="agent"
                to={workspacePath(routeWorkspaceId, "agent")}
              >
                <div className="metric-card-title">
                  <Bot size={16} aria-hidden="true" />
                  <span>{t("spaces.agent", { defaultValue: "Agent" })}</span>
                </div>
                <p>{t("spaces.agentPurpose", { defaultValue: "Instructions, skills, and memory" })}</p>
              </Link>
            ) : null}
          </div>
        </section>
        <section className="feature-section activity-feed-panel">
          <ListPanelHeader
            title={t("recent.title", { defaultValue: "Recent activity" })}
            count={preview.length}
            unit={t("activity:feed.entryUnit", { count: preview.length, defaultValue: "entries" })}
            action={<Link className="activity-feed-see-all" to={activityHref(query, undefined, routeWorkspaceId)}>
              {t("recent.viewAll", { defaultValue: "View all activity" })}
              <ChevronRight size={14} aria-hidden="true" />
            </Link>}
          />
          <div className="activity-feed-summary">
            <div>
              <p className="activity-feed-counts">{activityCountLabel(previewCounts.returned_entry_count, previewCounts.represented_event_count)}</p>
              <p className="activity-feed-scope">{t("activity:feed.preview", { count: preview.length, loaded: loadedCounts.returned_entry_count, defaultValue: "Showing {{count}} entries from the loaded range" })} · {t("activity:feed.scope", { defaultValue: "Current query range" })}</p>
            </div>
            <ActivityGroupingControl checked={groupReads} onChange={updateGrouping} />
          </div>
          {workspaceActivity.length === 0 ? (
            <div className="empty-state empty-state-rich">
              <h2>
                {t("recent.emptyTitle", {
                  defaultValue: "No workspace activity for {{period}}",
                  period: periodLabel(period, t),
                })}
              </h2>
              <p>
                {t("recent.emptyDescription", {
                  defaultValue:
                    "Page, source, review, and Ask changes appear here. System sign-in events stay in Activity so the dashboard remains focused.",
                })}
              </p>
              {hiddenSystemActivityCount > 0 ? (
                <div className="empty-state-actions">
                  {/* D1: the system probe uses limit=1, so its item count is not a
                      real total — show a fixed label (no count) and a hint that
                      system events live in Activity, not the Dashboard. */}
                  <Link className="icon-button text-button" to={systemActivityHref}>
                    {t("recent.viewSystemEvents", {
                      defaultValue: "View system events in Activity",
                    })}
                  </Link>
                  <p className="meta-line">
                    {t("recent.systemEventsHint", {
                      defaultValue: "System events are hidden from Dashboard",
                    })}
                  </p>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="operations-list activity-feed-list">
              {preview.map((entry) => <ActivityEntryRow entry={entry} query={query} loadGroup={loadGroup} key={entry.id} />)}
            </div>
          )}
        </section>
      </section>
    </div>
  );
}



function DashboardWelcome({
  canEdit,
  compact,
  showWorkspaceDiscovery,
  setupProgress,
}: {
  canEdit: boolean;
  compact: boolean;
  showWorkspaceDiscovery: boolean;
  setupProgress: FirstUseProgress;
}) {
  const routeWorkspaceId = useRouteWorkspaceId();
  const { t } = useTranslation("dashboard");
  const nextStepKey = firstIncompleteStep(setupProgress) ?? "review";
  const nextStep = firstUseStepByKey(nextStepKey);
  const nextStepLabel = t(`firstUse.steps.${nextStepKey}.label`, {
    defaultValue: nextStep.label,
  });
  const completedCount = FIRST_USE_STEPS.filter(
    (step) => setupProgress[step.key],
  ).length;
  const progressValue = Math.round((completedCount / FIRST_USE_STEPS.length) * 100);
  return (
    <section
      className={`feature-section detail-section dashboard-setup-card ${
        compact ? "is-compact" : ""
      }`}
      aria-label={t("setup.label", { defaultValue: "Workspace setup" })}
    >
      <div className="detail-heading">
        <div>
          <p className="eyebrow">{t("setup.eyebrow", { defaultValue: "Setup" })}</p>
          <h2>
            {compact
              ? t("setup.next", {
                  defaultValue: "Next: {{label}}",
                  label: nextStepLabel,
                })
              : t("setup.title", { defaultValue: "Start your first workspace" })}
          </h2>
          {compact ? (
            <p className="feature-subtitle">
              {t("setup.progress", {
                count: completedCount,
                total: FIRST_USE_STEPS.length,
                defaultValue: "{{count}} of {{total}} setup steps complete.",
              })}
            </p>
          ) : (
            <p className="feature-subtitle">
              {t("setup.description", {
                defaultValue:
                  "Add trusted materials, organize durable pages, then review changes before the wiki is updated.",
              })}
            </p>
          )}
        </div>
        {compact && canEdit ? (
          <div className="detail-heading-actions">
            <Link className="icon-button primary" to={workspacePath(routeWorkspaceId, nextStep.href)}>
              {t(`firstUse.steps.${nextStepKey}.action`, {
                defaultValue: nextStep.actionLabel,
              })}
            </Link>
          </div>
        ) : null}
      </div>
      {compact ? (
        <div
          aria-label={t("setup.progressLabel", {
            defaultValue: "Workspace setup progress",
          })}
          aria-valuemax={100}
          aria-valuemin={0}
          aria-valuenow={progressValue}
          className="setup-progress"
          role="progressbar"
        >
          <div
            className="setup-progress-bar"
            style={{ width: `${progressValue}%` }}
          />
        </div>
      ) : null}
      {!compact ? (
        <FirstUseChecklist
          completed={setupProgress}
          nextStep={nextStepKey}
          showPrimaryAction={canEdit}
        />
      ) : null}
      {showWorkspaceDiscovery ? (
        <div className="dashboard-welcome-guidance">
          <div className="dashboard-welcome-workspaces">
            <div>
              <strong>
                {t("setup.customizeWorkspaceTitle", {
                  defaultValue: "Make this workspace yours",
                })}
              </strong>
              <p>
                {t("setup.customizeWorkspaceDescription", {
                  defaultValue:
                    "You can rename this workspace. For ongoing team work, create a separate collaborative workspace from the workspace switcher.",
                })}
              </p>
            </div>
            <Link
              className="icon-button text-button"
              to={`${workspacePath(routeWorkspaceId, "settings")}?section=workspace`}
            >
              {t("setup.renameWorkspaceAction", {
                defaultValue: "Rename workspace",
              })}
            </Link>
          </div>
          <div className="dashboard-welcome-workspaces">
            <div>
              <strong>
                {t("setup.findWorkspaceTitle", {
                  defaultValue: "Looking for a shared workspace?",
                })}
              </strong>
              <p>
                {t("setup.findWorkspaceDescription", {
                  defaultValue:
                    "Browse discoverable workspaces without changing your current selection.",
                })}
              </p>
            </div>
            <Link className="icon-button text-button" to="/workspaces/find">
              {t("setup.findWorkspaceAction", {
                defaultValue: "Find workspaces",
              })}
            </Link>
          </div>
        </div>
      ) : null}
      {!compact ? (
        <div className="metric-grid dashboard-setup-actions">
          <Link
            className={`metric-card linked ${
              nextStep.key === "library" ? "recommended" : ""
            }`}
            to={workspacePath(routeWorkspaceId, "library")}
          >
            <div className="metric-card-title">
              <Library size={16} aria-hidden="true" />
              <span>{t("spaces.library", { defaultValue: "Library" })}</span>
            </div>
            <p>
              {t("setup.libraryDescription", {
                defaultValue: "Collect source material that answers should cite.",
              })}
            </p>
          </Link>
          <Link
            className={`metric-card linked ${
              nextStep.key === "context" ? "recommended" : ""
            }`}
            to={workspacePath(routeWorkspaceId, "context")}
          >
            <div className="metric-card-title">
              <FileText size={16} aria-hidden="true" />
              <span>{t("spaces.context", { defaultValue: "Context" })}</span>
            </div>
            <p>
              {t("setup.contextDescription", {
                defaultValue: "Keep reviewed pages where the team can read and edit them.",
              })}
            </p>
          </Link>
          <Link
            className={`metric-card linked ${
              nextStep.key === "review" ? "recommended" : ""
            }`}
            to={workspacePath(routeWorkspaceId, "review")}
          >
            <div className="metric-card-title">
              <CheckSquare size={16} aria-hidden="true" />
              <span>{t("setup.review", { defaultValue: "Review" })}</span>
            </div>
            <p>
              {t("setup.reviewDescription", {
                defaultValue:
                  "Approve, reject, or request changes before updates become durable.",
              })}
            </p>
          </Link>
        </div>
      ) : null}
    </section>
  );
}

function PeriodControl({
  period,
  label,
  onChange,
}: {
  period: ActivityPeriod;
  label: string;
  onChange: (period: ActivityPeriod) => void;
}) {
  const { t } = useTranslation("dashboard");
  return (
    <SegmentedControl
      label={label}
      options={DASHBOARD_PERIODS.map((entry) => ({
        key: entry,
        label: periodFilterLabel(entry, t),
      }))}
      value={period}
      onChange={onChange}
    />
  );
}

function parsePeriod(value: string | null): ActivityPeriod {
  return parsePeriodParam(value, DASHBOARD_PERIODS, "today");
}

function labelFor(value: string) {
  return value.replace(/_/g, " ");
}

function periodLabel(period: ActivityPeriod, t: ReturnType<typeof useTranslation>["t"]) {
  return t(`period.options.${period}`, { defaultValue: labelFor(period) });
}

// The control names a filter; the sentences above interpolate the same period
// mid-phrase. Only the control takes Title Case, matching Activity and Chats.
function periodFilterLabel(
  period: ActivityPeriod,
  t: ReturnType<typeof useTranslation>["t"],
) {
  return t(`period.filterOptions.${period}`, { defaultValue: labelFor(period) });
}

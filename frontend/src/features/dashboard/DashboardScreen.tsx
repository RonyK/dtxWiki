import {
  ExternalLink,
  Bot,
  CheckSquare,
  FileText,
  Library,
} from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useSearchParams } from "react-router-dom";

import {
  getSettingsPolicies,
  listActivity,
  listAskSessions,
  listRecentReviewDecisions,
  listReviews,
} from "../../shared/api/operations";
import type {
  ActivityItem,
  ActivityPeriod,
  ReviewItem,
  SettingsPolicies,
} from "../../shared/api/operations";
import { listContextPages } from "../../shared/api/context";
import { listLibraryItems } from "../../shared/api/library";
import { getCurrentWorkspaceId } from "../../shared/api/workspace";
import { ActivityActorBadge } from "../../shared/components/ActivityActorBadge";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import {
  FirstUseChecklist,
  firstIncompleteStep,
  firstUseStepByKey,
  type FirstUseProgress,
} from "../../shared/components/FirstUseChecklist";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import {
  activityTargetHref,
  isSystemActivityItem,
  presentActivityItem,
} from "../../shared/lib/activityLabels";
import { iconForActivityLane } from "../../shared/lib/activityLanes";
import {
  formatRelativeTimestamp,
  formatVersionTimestamp,
} from "../../shared/lib/datetime";
import { applyPeriodParam, parsePeriodParam } from "../../shared/lib/urlState";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { isEditorRole } from "../../shared/workspace/permissions";

const DASHBOARD_PERIODS: ActivityPeriod[] = ["today", "week", "month", "all"];
// The activity overview path returns no cursor, so lane counts are computed
// from a wide fetch window; a full window means the counts are lower bounds
// and render with a "+" suffix. The feed itself stays a short preview.
const DASHBOARD_ACTIVITY_FETCH_LIMIT = 50;
const DASHBOARD_RECENT_DISPLAY_LIMIT = 8;

type DashboardState = {
  activity: ActivityItem[];
  activityHasMore: boolean;
  systemActivityCount: number;
  reviews: ReviewItem[];
  reviewsHasMore: boolean;
  policies: SettingsPolicies;
  setupProgress: FirstUseProgress;
};

export function DashboardScreen() {
  const { t, i18n } = useTranslation("dashboard");
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const period = parsePeriod(searchParams.get("period"));
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
  const { data: state, error } = useAsyncLoad<DashboardState>(
    async () => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new Error("Workspace not found");
      }
      const [
        activity,
        systemActivity,
        reviews,
        reviewDecisions,
        policies,
        sources,
        contextPages,
        askSessions,
      ] =
        await Promise.all([
          listActivity(workspaceId, {
            period,
            kind: "workspace",
            limit: DASHBOARD_ACTIVITY_FETCH_LIMIT,
          }),
          listActivity(workspaceId, { period, kind: "system", limit: 1 }),
          listReviews(workspaceId, { status: "pending", limit: 20 }),
          listRecentReviewDecisions(workspaceId, { limit: 1 }),
          getSettingsPolicies(workspaceId),
          listLibraryItems(workspaceId),
          listContextPages(workspaceId),
          listAskSessions(workspaceId),
        ]);
      return {
        activity: activity.items,
        // The overview activity path never returns a cursor, so a full fetch
        // window is the cap signal: counts then read as lower bounds ("N+").
        activityHasMore:
          activity.items.length >= DASHBOARD_ACTIVITY_FETCH_LIMIT,
        systemActivityCount: systemActivity.items.length,
        reviews: reviews.items,
        // The pending count is fetched capped at limit:20; a next_cursor means
        // there are more pending reviews than counted (C19).
        reviewsHasMore: reviews.next_cursor != null,
        policies,
        setupProgress: {
          library: sources.data.length > 0,
          context: contextPages.data.length > 0,
          ask: askSessions.items.length > 0,
          // Cumulative like the other steps: the team has used Review once any
          // decision exists. A pending-queue check would regress to incomplete
          // the moment every pending review is decided (and read "done" while
          // items merely waited).
          review: reviewDecisions.length > 0,
        },
      };
    },
    [period],
    {
      errorMessage: t("errors.load", { defaultValue: "Failed to load Dashboard" }),
      fallbackData: {
        activity: [],
        activityHasMore: false,
        systemActivityCount: 0,
        reviews: [],
        reviewsHasMore: false,
        policies: emptyPolicies(),
        setupProgress: {},
      },
    },
  );

  function updatePeriod(nextPeriod: ActivityPeriod) {
    setSearchParams(
      applyPeriodParam(searchParams, "period", nextPeriod, "today"),
      { replace: true },
    );
  }

  // Activity links must carry BOTH the period and the kind. The lane counts are
  // computed from the workspace-only slice of the current Dashboard period, but
  // ActivityScreen defaults a missing `period` to "all" (not "today") and a
  // missing `kind` to "all" (system events included). Omitting either would
  // land the user on a different scope than the card counted, so always emit
  // period + kind explicitly.
  const systemActivityHref = `/activity?period=${period}&kind=system`;
  const workspaceActivity = useMemo(
    () => (state?.activity ?? []).filter((item) => !isSystemActivityItem(item)),
    [state?.activity],
  );
  const hiddenSystemActivityCount = state?.systemActivityCount ?? 0;
  const setupStarted = Object.values(state?.setupProgress ?? {}).some(Boolean);
  const needsSetupGuide =
    !state?.setupProgress.library ||
    !state.setupProgress.context ||
    !state.setupProgress.ask;
  const lanes = useMemo(() => {
    // Pending reviews appear in the activity feed as one pending-lane row per
    // ReviewItem (carrying review_item_id); those rows are already counted via
    // reviews.length, so only review-less pending activity rows add here.
    const pending = workspaceActivity.filter(
      (item) => item.lane === "pending" && item.review_item_id == null,
    );
    const advisory = workspaceActivity.filter((item) => item.lane === "advisory");
    const history = workspaceActivity.filter((item) => item.lane === "history");
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
        href: "/review",
        icon: iconForActivityLane("pending"),
      },
      {
        id: "advisory",
        title: t("lanes.advisory", { defaultValue: "Advisory" }),
        description: t("lanes.advisoryHint", { defaultValue: "Needs attention" }),
        count: advisory.length,
        // Activity-derived counts come from a capped fetch window; a full
        // window means the period holds more rows, so render a lower bound.
        countSuffix: state?.activityHasMore ? "+" : undefined,
        href: `/activity/advisory?period=${period}&kind=workspace`,
        icon: iconForActivityLane("advisory"),
      },
      {
        id: "history",
        title: t("lanes.history", { defaultValue: "History" }),
        description: t("lanes.historyHint", { defaultValue: "Completed changes" }),
        count: history.length,
        countSuffix: state?.activityHasMore ? "+" : undefined,
        href: `/activity/history?period=${period}&kind=workspace`,
        icon: iconForActivityLane("history"),
      },
    ];
  }, [period, state, t, workspaceActivity]);

  if (!state) {
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
              value={workspaceActivity.length}
              approxMore={state.activityHasMore}
              label={activityUnitForPeriod(workspaceActivity.length, period, t)}
            />
          ) : null}
        </div>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
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
            <Link className="metric-card linked" data-kind="source" to="/library">
              <div className="metric-card-title">
                <Library size={16} aria-hidden="true" />
                <span>{t("spaces.library", { defaultValue: "Library" })}</span>
              </div>
              <p>{t("spaces.libraryPurpose", { defaultValue: "Source material to cite" })}</p>
            </Link>
            <Link className="metric-card linked" data-kind="context" to="/context">
              <div className="metric-card-title">
                <FileText size={16} aria-hidden="true" />
                <span>{t("spaces.context", { defaultValue: "Context" })}</span>
              </div>
              <p>{t("spaces.contextPurpose", { defaultValue: "Durable team knowledge" })}</p>
            </Link>
            <Link
              className="metric-card linked"
              data-kind="agent"
              to="/agent"
            >
              <div className="metric-card-title">
                <Bot size={16} aria-hidden="true" />
                <span>{t("spaces.agent", { defaultValue: "Agent" })}</span>
              </div>
              <p>{t("spaces.agentPurpose", { defaultValue: "Instructions, skills, and memory" })}</p>
            </Link>
          </div>
        </section>
        <section className="feature-section">
          <ListPanelHeader
            title={t("recent.title", { defaultValue: "Recent activity" })}
            count={workspaceActivity.length}
            // The overview activity path has no cursor, so a full fetch window
            // means the count is a lower bound ("N+"), not a true total.
            approxMore={state.activityHasMore}
            unit={t(
              workspaceActivity.length === 1
                ? "recent.unit_one"
                : "recent.unit_other",
              {
                defaultValue:
                  workspaceActivity.length === 1 ? "event" : "events",
              },
            )}
          />
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
            <div className="operations-list">
              {workspaceActivity
                .slice(0, DASHBOARD_RECENT_DISPLAY_LIMIT)
                .map((item) => {
                  const presented = presentActivityItem(item);
                  const action = classifyDashboardActivityAction(item);
                  const targetHref = activityTargetHref(item);
                  const targetKind = item.target?.target_kind;
                  // Same rule as ActivityRow: show the real target name only
                  // when the headline does not already say it, otherwise the
                  // kind label -- never the same string twice in one row.
                  const targetTitle = item.target_title?.trim() || null;
                  const chipText =
                    targetTitle && targetTitle !== presented.title
                      ? targetTitle
                      : targetKind
                        ? t(`activity:targetKinds.${targetKind}`, {
                            defaultValue: targetKind.replace(/_/g, " "),
                          })
                        : null;
                  return (
                    <article className="activity-row" key={item.id}>
                      <div className="activity-row__main">
                        <div className="activity-row__body">
                          <p className="activity-row__title">
                            <ActivityActorBadge actor={item.actor} />
                            <span>{presented.title}</span>
                          </p>
                          {presented.body ? (
                            <p className="activity-row__detail">
                              {presented.body}
                            </p>
                          ) : null}
                          {targetHref && chipText ? (
                            <span className="row-badges activity-row__chips">
                              <Link
                                aria-label={t("activity:actions.openTarget", {
                                  title: targetTitle ?? presented.title,
                                  defaultValue: `Open target ${targetTitle ?? presented.title}`,
                                })}
                                className="activity-source-chip"
                                to={targetHref}
                              >
                                <ExternalLink size={13} aria-hidden="true" />
                                <span>{chipText}</span>
                              </Link>
                            </span>
                          ) : null}
                        </div>
                        <div className="activity-row__trailing">
                          <StatusPill
                            status={dashboardActivityActionLabel(item, t)}
                            // The pill infers its tone from the English label,
                            // so a translated one has to state it.
                            tone={action === "failed" ? "danger" : undefined}
                            compact
                          />
                          {/* C5: one timestamp convention — relative text with
                              the long absolute tooltip, matching ActivityRow. */}
                          <time
                            className="activity-relative-time"
                            dateTime={item.created_at}
                            title={formatVersionTimestamp(item.created_at, "long")}
                          >
                            {formatRelativeTimestamp(item.created_at, i18n.language)}
                          </time>
                        </div>
                      </div>
                    </article>
                  );
                })}
              {workspaceActivity.length > DASHBOARD_RECENT_DISPLAY_LIMIT ? (
                <div className="load-more-row">
                  <Link
                    className="icon-button text-button"
                    to={`/activity?period=${period}`}
                  >
                    {t("recent.viewAll", { defaultValue: "View all activity" })}
                  </Link>
                </div>
              ) : null}
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
  const { t } = useTranslation("dashboard");
  const nextStepKey = firstIncompleteStep(setupProgress) ?? "review";
  const nextStep = firstUseStepByKey(nextStepKey);
  const nextStepLabel = t(`firstUse.steps.${nextStepKey}.label`, {
    defaultValue: nextStep.label,
  });
  const completedCount = Object.values(setupProgress).filter(Boolean).length;
  const progressValue = Math.round((completedCount / 4) * 100);
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
                defaultValue: "{{count}} of 4 setup steps complete.",
              })}
            </p>
          ) : (
            <p className="feature-subtitle">
              {t("setup.description", {
                defaultValue:
                  "Add trusted materials, organize durable pages, ask context-aware questions, then review changes before the wiki is updated.",
              })}
            </p>
          )}
        </div>
        {compact && canEdit ? (
          <div className="detail-heading-actions">
            <Link className="icon-button primary" to={nextStep.href}>
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
                {t("setup.personalWorkspaceTitle", {
                  defaultValue: "Make your Personal Workspace yours",
                })}
              </strong>
              <p>
                {t("setup.personalWorkspaceDescription", {
                  defaultValue:
                    "You can rename your Personal Workspace. For ongoing team work, create a separate collaborative workspace from the workspace switcher.",
                })}
              </p>
            </div>
            <Link
              className="icon-button text-button"
              to="/settings?section=workspace"
            >
              {t("setup.renamePersonalAction", {
                defaultValue: "Rename Personal",
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
            to="/library"
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
            to="/context"
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
              nextStep.key === "ask" ? "recommended" : ""
            }`}
            to="/ask"
          >
            <div className="metric-card-title">
              <Bot size={16} aria-hidden="true" />
              <span>{t("setup.ask", { defaultValue: "Ask" })}</span>
            </div>
            <p>
              {t("setup.askDescription", {
                defaultValue: "Ask questions, promote useful answers, and review changes.",
              })}
            </p>
          </Link>
          <Link
            className={`metric-card linked ${
              nextStep.key === "review" ? "recommended" : ""
            }`}
            to="/review"
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

// "events", not "changes": the same number is chipped as events in the Recent
// activity header and on Activity, so one noun counts it everywhere.
function activityUnitForPeriod(
  count: number,
  period: ActivityPeriod,
  t: ReturnType<typeof useTranslation>["t"],
) {
  return t(`header.activityUnit.${period}`, {
    count,
    defaultValue: "events",
  });
}

type DashboardActivityAction =
  | "restored"
  | "published"
  | "created"
  | "edited"
  | "failed";

// Audit-derived activity carries the machine action code in body
// ("domain.entity.operation"), which is locale-independent — classify from it
// first so Korean (or any localized) titles still resolve the right action.
const AUDIT_ACTION_CODE_PATTERN = /^[a-z0-9_]+(?:\.[a-z0-9_]+)+$/;

/** Same axis as the row's badge, so glyph and label never disagree. */
// An attempt that did not happen must never read as a completed change. MCP and
// job rows name the outcome as the last word of the title ("MCP update draft
// failed") and as a status/error field in the detail body; the verb keywords
// below see only the tool name and would call that row an edit.
const FAILED_TITLE_PATTERN = /\s(failed|rejected_by_policy|conflict)$/i;
const FAILED_DETAIL_PATTERN =
  /(?:^|;\s*)(?:status=(?:failed|rejected_by_policy|conflict)|error=)/;

function classifyDashboardActivityAction(
  item: ActivityItem,
): DashboardActivityAction | null {
  const body = item.body ?? "";
  if (FAILED_TITLE_PATTERN.test(item.title) || FAILED_DETAIL_PATTERN.test(body)) {
    return "failed";
  }
  if (AUDIT_ACTION_CODE_PATTERN.test(body)) {
    const operation = body.split(".").at(-1) ?? "";
    if (/^(restore|revert)/.test(operation)) {
      return "restored";
    }
    if (/^(publish|activate)/.test(operation)) {
      return "published";
    }
    if (/^(create|import|upload|add)/.test(operation)) {
      return "created";
    }
    if (/^(update|edit|save|draft|write)/.test(operation)) {
      return "edited";
    }
  }
  // Free-text rows (e.g. review decision summaries) fall back to keyword
  // matching over the display text.
  const text = `${item.title} ${body} ${(item.affordances ?? []).join(" ")}`
    .toLowerCase();
  if (/\b(restored|restore|reverted|revert)\b/.test(text)) {
    return "restored";
  }
  if (/\b(published|publish|activated|activate)\b/.test(text)) {
    return "published";
  }
  if (/\b(created|create|imported|import|uploaded|upload|added|add)\b/.test(text)) {
    return "created";
  }
  if (/\b(edited|edit|updated|update|saved|save|draft|write|wrote)\b/.test(text)) {
    return "edited";
  }
  return null;
}

function dashboardActivityActionLabel(
  item: ActivityItem,
  t: ReturnType<typeof useTranslation>["t"],
) {
  const action = classifyDashboardActivityAction(item);
  if (action) {
    return t(`recent.action.${action}`, { defaultValue: labelFor(action) });
  }
  // The badge column is one axis: what happened. An unclassifiable row (an MCP
  // read, a search) falls back to a neutral verb, never to its lane name — a
  // "History" badge next to "Created"/"Edited" would silently switch axes.
  return t("recent.action.logged", { defaultValue: "Logged" });
}

function emptyPolicies(): SettingsPolicies {
  const policy = (name: string, title: string) => ({
    name,
    title,
    policy: {},
    // A fallback response must never masquerade as a server revision. If a
    // caller reaches a guarded save, this value produces a safe conflict.
    revision: "unavailable",
  });
  return {
    workspace: policy("workspace", "Workspace policy"),
    library: policy("library", "Library policy"),
    citation: policy("citation", "Citation policy"),
    context_write: policy("context_write", "Context policy"),
    agent_injection: policy("agent_injection", "Agent policy"),
    review: policy("review", "Review policy"),
    lint: policy("lint", "Lint rules"),
    mcp_clients: [],
  };
}

import { ChevronDown, ChevronRight, RotateCcw } from "lucide-react";
import type { TFunction } from "i18next";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError, apiErrorMessage } from "../../shared/api/client";
import { listActivityFeed } from "../../shared/api/operations";
import type {
  ActivityFeedData,
  ActivityFeedEntry,
  ActivityFeedEvent,
  ActivityLane,
  ActivityPeriod,
} from "../../shared/api/operations";
import { useRouteWorkspaceId, workspacePath } from "../../shared/lib/workspaceRoutes";
import { ActivityEntryRow } from "../../shared/components/ActivityEntryRow";
import { ActivityGroupingControl } from "../../shared/components/ActivityGroupingControl";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useActivityGroupLoader } from "../../shared/hooks/useActivityGroupLoader";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { ACTIVITY_SUBTYPES } from "../../shared/lib/activityLabels";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { isEditorRole } from "../../shared/workspace/permissions";
import {
  DEFAULT_ACTIVITY_QUERY, activityCountLabel, activityEntryCounts,
  activityHref, activityQueryParams, parseActivityQuery, reconcileActivityReadGroup,
  type ActivityGroupLoader, type ActivityQuery,
} from "../../shared/lib/activityFeed";
import { iconForActivityLane } from "../../shared/lib/activityLanes";
const PERIODS: ActivityPeriod[] = ["today", "yesterday", "week", "month", "all"];
const ACTIVITY_KINDS = ["all", "workspace", "mcp", "system"] as const;
const LANES: Array<{ lane: ActivityLane; title: string; tone: string }> = [
  { lane: "pending", title: "Pending", tone: "Needs review" },
  { lane: "advisory", title: "Advisory", tone: "Needs attention" },
  { lane: "history", title: "History", tone: "Completed changes" },
];
type ActivityKind = (typeof ACTIVITY_KINDS)[number];

const OVERVIEW_LIMIT = 50;
const LANE_PAGE_SIZE = 50;
type FeedScreenState = { workspaceId: string; queryKey: string; feed: ActivityFeedData };


function useActivityQuery() {
  const [params, setParams] = useSearchParams();
  const query = parseActivityQuery(params);
  function update(next: Partial<ActivityQuery>) {
    const validated = parseActivityQuery(activityQueryParams({ ...query, ...next }));
    // Commit a native checkbox's URL-backed value during its change event;
    // a deferred router transition can otherwise restore its old checked state.
    setParams(activityQueryParams(validated), { replace: true, flushSync: next.group_reads !== undefined });
  }
  function reset() { setParams(activityQueryParams(DEFAULT_ACTIVITY_QUERY), { replace: true }); }
  return { query, update, reset };
}

function useFeed(query: ActivityQuery, lane?: ActivityLane) {
  const routeWorkspaceId = useRouteWorkspaceId();
  const { t } = useTranslation("activity");
  const queryKey = `${lane ?? "overview"}:${activityQueryParams(query)}`;
  const result = useAsyncLoad<FeedScreenState>(async (signal) => {
    const workspaceId = routeWorkspaceId;
    const feed = await listActivityFeed(workspaceId, { ...query, lane, limit: lane ? LANE_PAGE_SIZE : OVERVIEW_LIMIT }, signal);
    return { workspaceId, queryKey, feed };
  }, [routeWorkspaceId, queryKey], {
    signalAware: true,
    errorMessage: t("errors.load", { defaultValue: "Failed to load Activity" }),
  });
  const { setData } = result;
  const reconcile = useCallback((token: string, events: ActivityFeedEvent[]) => {
    setData(current => current?.queryKey === queryKey ? reconcileFeedGroup(current, token, events) : current);
  }, [queryKey, setData]);
  const loadGroup = useActivityGroupLoader(result.data?.workspaceId, reconcile, result.reload);
  return { ...result, queryKey, loadGroup };
}

export function ActivityScreen() {
  const routeWorkspaceId = useRouteWorkspaceId();
  const { t } = useTranslation("activity");
  const { query, update, reset } = useActivityQuery();
  const { data, error, loading, reload, queryKey, loadGroup } = useFeed(query);
  if (!data || data.queryKey !== queryKey) {
    if (error && !loading) return <ActivityLoadError error={error} reload={reload} />;
    return <RouteLoading />;
  }
  const grouped = groupByLane(data.feed.entries);
  return (
    <div className="workspace-screen single-column activity-screen">
      <section className="feature-main">
        <Breadcrumbs items={[{ label: t("breadcrumb", { defaultValue: "Activity" }) }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{t("header.eyebrow", { defaultValue: "Operations timeline" })}</p>
            <h1>{t("header.title", { defaultValue: "Activity" })}</h1>
            <p className="feature-subtitle">{t("header.subtitle", { defaultValue: "Pending, advisory, and completed workspace events." })}</p>
            <p className="activity-feed-counts">{activityCountLabel(data.feed.entries.length, activityEntryCounts(data.feed.entries).represented_event_count)}</p>
            <p className="activity-feed-scope">{t("feed.scope", { defaultValue: "Current query range" })}</p>
          </div>
        </div>
        {error ? <StatusBanner tone="warning">{error}<button className="icon-button text-button activity-feed-reload" onClick={reload} type="button">{t("feed.reload", { defaultValue: "Reload activity" })}</button></StatusBanner> : null}
        <ActivityFiltersPanel
          kind={query.kind} kindFilterLabel={t("kind.label", { defaultValue: "Type" })}
          subtype={query.subtype} subtypeFilterLabel={t("subtype.label", { defaultValue: "Sub-type" })}
          period={query.period} periodFilterLabel={t("period.label", { defaultValue: "Period" })}
          groupReads={query.group_reads} onGroupingChange={group_reads => update({ group_reads })}
          onKindChange={kind => update({ kind })} onPeriodChange={period => update({ period })}
          onSubtypeChange={subtype => update({ subtype })} onReset={reset}
        />
        <section className="feature-section">
          <div className="metric-grid">
            {LANES.map(({ lane, title, tone }) => (
              <Link className="metric-card linked" key={lane} to={activityHref(query, lane, routeWorkspaceId)}>
                <div className="metric-card-title">{iconForActivityLane(lane)}<span>{t(`lanes.${lane}.title`, { defaultValue: title })}</span></div>
                <strong>
                  {data.feed.lanes[lane]?.represented_event_count ?? activityEntryCounts(grouped.get(lane) ?? []).represented_event_count}
                  {/* A lower bound only where the server confirmed more for this lane. */}
                  {data.feed.lanes[lane]?.has_more === true ? "+" : ""}
                </strong>
                <p>{t(`lanes.${lane}.tone`, { defaultValue: tone })}</p>
                <p className="activity-feed-scope">{t("feed.scope", { defaultValue: "Current query range" })}</p>
              </Link>
            ))}
          </div>
          {data.feed.has_more ? <p className="activity-window-notice" role="status">{t("overview.windowNotice", { count: data.feed.returned_entry_count })}</p> : null}
        </section>
        {LANES.map(({ lane, title }) => <ActivityLaneSection key={`${queryKey}:${lane}`} query={query} lane={lane} title={t(`lanes.${lane}.title`, { defaultValue: title })} entries={grouped.get(lane) ?? []} loadGroup={loadGroup} />)}
      </section>
    </div>
  );
}

function PeriodControl({
  label,
  period,
  onChange,
}: {
  label: string;
  period: ActivityPeriod;
  onChange: (period: ActivityPeriod) => void;
}) {
  const { t } = useTranslation("activity");
  return (
    <SegmentedControl
      label={label}
      options={PERIODS.map((entry) => ({
        key: entry,
        label: periodLabel(entry, t),
        title: periodTitle(entry, t),
      }))}
      value={period}
      onChange={onChange}
    />
  );
}

function ActivityKindControl({
  kind,
  label,
  onChange,
}: {
  kind: ActivityKind;
  label: string;
  onChange: (kind: ActivityKind) => void;
}) {
  const { t } = useTranslation("activity");
  return (
    <SegmentedControl
      label={label}
      options={ACTIVITY_KINDS.map((entry) => ({
        key: entry,
        label: kindLabel(entry, t),
        title: kindTitle(entry, t),
      }))}
      value={kind}
      onChange={onChange}
    />
  );
}

function subtypeLabel(subtype: string, t: TFunction) {
  return subtype === "all"
    ? t("subtype.all", { defaultValue: "All" })
    : t(`subtype.options.${subtype}`, { defaultValue: subtype });
}

function ActivitySubtypeControl({
  kind,
  label,
  subtype,
  onChange,
}: {
  kind: ActivityKind;
  label: string;
  subtype: string;
  onChange: (subtype: string) => void;
}) {
  const { t } = useTranslation("activity");
  const options = ACTIVITY_SUBTYPES[kind] ?? [];
  // This changes a list filter, not a view panel. A radiogroup expresses the
  // single selected value without dangling tab -> tabpanel ARIA references.
  return (
    <SegmentedControl
      className="activity-subtype-control"
      compact
      label={label}
      onChange={onChange}
      options={options.map((entry) => ({
        key: entry,
        label: subtypeLabel(entry, t),
      }))}
      value={subtype}
    />
  );
}

type ActivityFiltersPanelProps = {
  kind: ActivityKind;
  subtype: string;
  period: ActivityPeriod;
  kindFilterLabel: string;
  subtypeFilterLabel: string;
  periodFilterLabel: string;
  groupReads: boolean;
  onGroupingChange: (checked: boolean) => void;
  onKindChange: (kind: ActivityKind) => void;
  onSubtypeChange: (subtype: string) => void;
  onPeriodChange: (period: ActivityPeriod) => void;
  onReset: () => void;
};

function ActivityFiltersPanel({
  kind,
  subtype,
  period,
  kindFilterLabel,
  subtypeFilterLabel,
  periodFilterLabel,
  onKindChange,
  onSubtypeChange,
  onPeriodChange,
  onReset,
  groupReads,
  onGroupingChange,
}: ActivityFiltersPanelProps) {
  const { t } = useTranslation("activity");
  const compactFilters = useMediaQuery("(max-width: 640px)");
  const [open, setOpen] = useState(() => !compactFilters);
  const bodyId = useId();
  const activeFilterCount = [
    kind !== "all",
    subtype !== "all",
    period !== "all",
  ].filter(Boolean).length;

  useEffect(() => {
    setOpen(!compactFilters);
  }, [compactFilters]);

  return (
    <section className="activity-filter-shell">
      <ActivityFilterSummary
        kind={kind}
        subtype={subtype}
        period={period}
        onReset={onReset}
        groupReads={groupReads}
      />
      <ActivityGroupingControl checked={groupReads} onChange={onGroupingChange} />
      <button
        aria-controls={bodyId}
        aria-expanded={open}
        className="activity-filter-toggle"
        onClick={() => setOpen((current) => !current)}
        type="button"
      >
        <span>
          {activeFilterCount > 0
            ? t("filters.activeTitle", { count: activeFilterCount })
            : t("filters.title", { defaultValue: "Filters" })}
        </span>
        <ChevronDown aria-hidden="true" data-open={open} size={14} />
      </button>
      {open ? (
        <div className="activity-filters" id={bodyId} aria-label="Activity filters">
          <div className="activity-filter-group activity-type-filter">
            <div className="activity-filter-heading">
              <span className="eyebrow">{kindFilterLabel}</span>
              <ActivityKindControl
                kind={kind}
                label={kindFilterLabel}
                onChange={onKindChange}
              />
            </div>
            {ACTIVITY_SUBTYPES[kind] ? (
              <div className="activity-subtype-row">
                <span className="activity-subtype-label">
                  {subtypeFilterLabel}
                </span>
                <ActivitySubtypeControl
                  kind={kind}
                  label={subtypeFilterLabel}
                  subtype={subtype}
                  onChange={onSubtypeChange}
                />
              </div>
            ) : null}
          </div>
          <div className="activity-filter-group activity-period-filter">
            <div className="activity-filter-heading">
              <span className="eyebrow">{periodFilterLabel}</span>
              <PeriodControl
                label={periodFilterLabel}
                period={period}
                onChange={onPeriodChange}
              />
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}

// A summary of the active (non-default) filters with a one-click Reset, so a
// deep-linked view (e.g. Review's "More activities") shows what is applied and
// is easy to clear. It sits outside the collapsible body on purpose: a folded
// panel must still name the filters that are cutting the list.
function ActivityFilterSummary({
  kind,
  subtype,
  period,
  onReset,
  groupReads,
}: {
  kind: ActivityKind;
  subtype: string;
  period: ActivityPeriod;
  onReset: () => void;
  groupReads: boolean;
}) {
  const { t } = useTranslation("activity");
  const chips: string[] = [];
  if (kind !== "all") {
    chips.push(
      subtype !== "all"
        ? `${kindLabel(kind, t)} > ${subtypeLabel(subtype, t)}`
        : kindLabel(kind, t),
    );
  }
  if (period !== "all") {
    chips.push(periodLabel(period, t));
  }
  // The grouping mode is always named, even on the default view, but only a
  // view that differs from the defaults is labelled "Filtered" and offers Reset.
  const filtered = chips.length > 0 || !groupReads;
  chips.push(t(groupReads ? "feed.grouped" : "feed.raw"));
  return (
    <div className="activity-filter-summary">
      {filtered ? (
        <span className="activity-filter-summary-label">
          {t("filterSummary.label", { defaultValue: "Filtered" })}
        </span>
      ) : null}
      {chips.map((chip) => (
        <span className="activity-filter-chip" key={chip}>
          {chip}
        </span>
      ))}
      {filtered ? (
        <button className="activity-filter-reset" onClick={onReset} type="button">
          <RotateCcw size={13} aria-hidden="true" />
          <span>{t("filterSummary.reset", { defaultValue: "Reset" })}</span>
        </button>
      ) : null}
    </div>
  );
}

function ActivityLaneEmpty({
  kind,
  title,
  filtered = false,
}: {
  kind: ActivityKind;
  title: string;
  filtered?: boolean;
}) {
  const { t } = useTranslation("activity");
  const routeWorkspaceId = useRouteWorkspaceId();
  const workspace = useOptionalWorkspaceContext();
  const showChats = isEditorRole(workspace?.selectedWorkspace?.role);
  const message =
    kind === "system"
      ? t("empty.system", { defaultValue: "No system events for this period." })
      : kind === "mcp"
        ? t("empty.mcp", { defaultValue: "No MCP traces for this period." })
        : t("empty.workspace", {
            defaultValue: "No workspace activity for this period.",
          });
  // When a client-side filter empties the current page but later pages still
  // hold rows, say so and point at Load more rather than claiming nothing exists.
  if (filtered) {
    return (
      <div className="empty-state empty-state-rich">
        <h2>
          {t("empty.filteredTitle", {
            defaultValue: "No matches on this page",
          })}
        </h2>
        <p>
          {t("empty.filteredHint", {
            defaultValue: "Load more to look for matches in older activity.",
          })}
        </p>
      </div>
    );
  }
  // Codex #29: the kind==="all" message is never rendered (it just repeats the
  // heading; see below), so empty.all is intentionally not looked up.
  return (
    <div className="empty-state empty-state-rich">
      <h2>
        {t("empty.title", {
          defaultValue: "No {{title}} activity",
          title: title.toLowerCase(),
        })}
      </h2>
      {/* UI-22: the generic "all kinds" message just repeats the heading, so it
          is dropped there; kind-specific messages ("No system events…", "No MCP
          traces…") add context the heading lacks and are kept. */}
      {kind === "all" ? null : <p>{message}</p>}
      {kind === "system" ? null : kind === "mcp" ? (
        <div className="empty-state-actions">
          <Link className="icon-button text-button" to={`${workspacePath(routeWorkspaceId, "settings/integrations")}`}>
            {t("actions.openMcpSetup", { defaultValue: "Open MCP setup" })}
          </Link>
          {showChats ? (
            <Link className="icon-button text-button" to={workspacePath(routeWorkspaceId, "chats")}>
              {t("actions.openChats", { defaultValue: "Open Chats" })}
            </Link>
          ) : null}
        </div>
      ) : (
        <div className="empty-state-actions">
          <Link className="icon-button text-button" to={workspacePath(routeWorkspaceId, "context")}>
            {t("actions.browseContext", { defaultValue: "Browse Context pages" })}
          </Link>
          <Link className="icon-button text-button" to={`${workspacePath(routeWorkspaceId, "context/pages/new")}?from=context`}>
            {t("actions.createPage", { defaultValue: "Create a new page" })}
          </Link>
        </div>
      )}
    </div>
  );
}

const LANE_PREVIEW_LIMIT = 5;

function ActivityLaneSection({ query, lane, title, entries, loadGroup }: {
  query: ActivityQuery; lane: ActivityLane; title: string; entries: ActivityFeedEntry[];
  loadGroup: ActivityGroupLoader;
}) {
  const { t } = useTranslation("activity");
  const routeWorkspaceId = useRouteWorkspaceId();
  const preview = entries.slice(0, LANE_PREVIEW_LIMIT);
  const counts = activityEntryCounts(preview);
  const actionKey = { pending: "feed.seeAllPending", advisory: "feed.seeAllAdvisory", history: "feed.seeAllHistory" } as const;
  return <section className="feature-section activity-feed-panel">
    <ListPanelHeader title={title} count={preview.length} unit={t("feed.entryUnit", { count: preview.length, defaultValue: "entries" })}
      action={<Link className="activity-feed-see-all" to={activityHref(query, lane, routeWorkspaceId)}>{t(actionKey[lane])}<ChevronRight size={14} aria-hidden="true" /></Link>} />
    <div className="activity-feed-summary"><div><p className="activity-feed-counts">{activityCountLabel(counts.returned_entry_count, counts.represented_event_count)}</p><p className="activity-feed-scope">{t("feed.scope", { defaultValue: "Current query range" })}</p></div></div>
    {preview.length === 0 ? <ActivityLaneEmpty kind={query.kind} title={title} /> : <div className="operations-list activity-feed-list">{preview.map(entry => <ActivityEntryRow entry={entry} query={query} loadGroup={loadGroup} key={entry.id} />)}</div>}
  </section>;
}

function parseLane(value: string | undefined): ActivityLane | null {
  return LANES.some(entry => entry.lane === value) ? value as ActivityLane : null;
}

export function ActivityLanePage() {
  const routeWorkspaceId = useRouteWorkspaceId();
  const { t } = useTranslation("activity");
  const { lane: laneParam } = useParams();
  const lane = parseLane(laneParam);
  const { query, update, reset } = useActivityQuery();
  const { data, error, loading, setData, setError, reload, queryKey, loadGroup } = useFeed(query, lane ?? undefined);
  const [loadingMore, setLoadingMore] = useState(false);
  const moreController = useRef<AbortController | null>(null);
  useEffect(() => {
    setLoadingMore(false);
    return () => { moreController.current?.abort(); moreController.current = null; };
  }, [queryKey]);
  async function loadMore() {
    if (!lane || !data?.workspaceId || data.queryKey !== queryKey || !data.feed.next_cursor || loadingMore) return;
    const controller = new AbortController();
    moreController.current = controller;
    const previousCursor = data.feed.next_cursor;
    const previousKey = data.queryKey;
    setLoadingMore(true);
    setError(null);
    try {
      const nextPage = await listActivityFeed(data.workspaceId, { ...query, lane, limit: LANE_PAGE_SIZE, cursor: previousCursor }, controller.signal);
      if (controller.signal.aborted) return;
      setData(current => {
        if (!current || current.queryKey !== previousKey || current.feed.next_cursor !== previousCursor) return current;
        const ids = new Set(current.feed.entries.map(entry => entry.id));
        const entries = [...current.feed.entries, ...nextPage.entries.filter(entry => !ids.has(entry.id))];
        const counts = activityEntryCounts(entries);
        return { ...current, feed: { ...nextPage, entries, ...counts } };
      });
    } catch (exc) {
      if (controller.signal.aborted) return;
      if (exc instanceof ApiError && exc.body.error_code === "invalid_activity_token") {
        setData(current => current?.queryKey === previousKey ? { ...current, feed: { ...current.feed, next_cursor: null } } : current);
        reload();
      } else setError(apiErrorMessage(exc, t("errors.loadMore", { defaultValue: "Failed to load more" })));
    } finally {
      if (moreController.current === controller) { moreController.current = null; setLoadingMore(false); }
    }
  }
  if (!lane) return <Navigate replace to={activityHref(query, undefined, routeWorkspaceId)} />;
  if (!data || data.queryKey !== queryKey) {
    if (error && !loading) return <ActivityLoadError error={error} reload={reload} />;
    return <RouteLoading />;
  }
  const title = t(`lanes.${lane}.title`, { defaultValue: lane });
  const counts = activityEntryCounts(data.feed.entries);
  return <div className="workspace-screen single-column activity-screen"><section className="feature-main">
    <Breadcrumbs items={[{ label: t("breadcrumb", { defaultValue: "Activity" }), href: activityHref(query, undefined, routeWorkspaceId) }, { label: title }]} />
    <div className="feature-header"><div><p className="eyebrow">{t("header.eyebrow", { defaultValue: "Operations timeline" })}</p><h1>{title}</h1><p className="activity-feed-counts">{activityCountLabel(counts.returned_entry_count, counts.represented_event_count)}</p><p className="activity-feed-scope">{t("feed.scope", { defaultValue: "Current query range" })}</p></div></div>
    {error ? <StatusBanner tone="warning">{error}<button className="icon-button text-button activity-feed-reload" onClick={reload} type="button">{t("feed.reload", { defaultValue: "Reload activity" })}</button></StatusBanner> : null}
    <ActivityFiltersPanel kind={query.kind} kindFilterLabel={t("kind.label", { defaultValue: "Type" })} subtype={query.subtype} subtypeFilterLabel={t("subtype.label", { defaultValue: "Sub-type" })} period={query.period} periodFilterLabel={t("period.label", { defaultValue: "Period" })} groupReads={query.group_reads} onGroupingChange={group_reads => update({ group_reads })} onKindChange={kind => update({ kind })} onPeriodChange={period => update({ period })} onSubtypeChange={subtype => update({ subtype })} onReset={reset} />
    <section className="feature-section activity-feed-panel">
      {data.feed.entries.length === 0 ? <ActivityLaneEmpty kind={query.kind} title={title} /> : <div className="operations-list activity-feed-list">{data.feed.entries.map(entry => <ActivityEntryRow entry={entry} query={query} loadGroup={loadGroup} key={entry.id} />)}</div>}
      {data.feed.next_cursor ? <div className="load-more-row"><button className="icon-button text-button" disabled={loadingMore} onClick={() => void loadMore()} type="button">{loadingMore ? t("loadingMore", { defaultValue: "Loading" }) : t("loadMore", { defaultValue: "Load more" })}</button></div> : !error ? <p className="activity-feed-scope" role="status">{t("feed.loadedAll", { defaultValue: "All matching activity in this snapshot is shown" })}</p> : null}
    </section>
  </section></div>;
}

function reconcileFeedGroup(current: FeedScreenState, token: string, events: ActivityFeedEvent[]): FeedScreenState {
  const entries = reconcileActivityReadGroup(current.feed.entries, token, events);
  const lanes = { ...current.feed.lanes };
  for (const { lane } of LANES) {
    lanes[lane] = { ...lanes[lane], ...activityEntryCounts(entries.filter(entry => entry.event.lane === lane)) };
  }
  return { ...current, feed: { ...current.feed, entries, lanes, ...activityEntryCounts(entries) } };
}

function ActivityLoadError({ error, reload }: { error: string; reload: () => void }) {
  const { t } = useTranslation("activity");
  return <div className="workspace-screen single-column activity-screen"><section className="feature-main">
    <h1>{t("header.title")}</h1>
    <StatusBanner tone="warning">{error}<button className="icon-button text-button activity-feed-reload" type="button" onClick={reload}>{t("feed.reload")}</button></StatusBanner>
  </section></div>;
}

function groupByLane(entries: ActivityFeedEntry[]) {
  const grouped = new Map<ActivityLane, ActivityFeedEntry[]>();
  for (const entry of entries) grouped.set(entry.event.lane, [...(grouped.get(entry.event.lane) ?? []), entry]);
  const pending = grouped.get("pending");
  if (pending) grouped.set("pending", [...pending].sort((a,b) => Number(!a.event.review_item_id) - Number(!b.event.review_item_id)));
  return grouped;
}

function labelFor(value: string, t: TFunction) {
  return t(`period.options.${value}`, { defaultValue: defaultPeriodLabel(value) });
}

function periodLabel(period: ActivityPeriod, t: TFunction) {
  return labelFor(period, t);
}

function kindLabel(kind: ActivityKind, t: TFunction) {
  if (kind === "workspace") {
    return t("kind.options.workspace", { defaultValue: "Workspace" });
  }
  if (kind === "system") {
    return t("kind.options.system", { defaultValue: "System" });
  }
  if (kind === "mcp") {
    return t("kind.options.mcp", { defaultValue: "MCP" });
  }
  return t("kind.options.all", { defaultValue: "All" });
}

function kindTitle(kind: ActivityKind, t: TFunction) {
  if (kind === "workspace") {
    return t("kind.titles.workspace", {
      defaultValue: "Workspace activity from pages, sources, review, Ask, and MCP tools.",
    });
  }
  if (kind === "system") {
    return t("kind.titles.system", {
      defaultValue: "System events such as sign-ins and security records.",
    });
  }
  if (kind === "mcp") {
    return t("kind.titles.mcp", {
      defaultValue: "External MCP traces from Codex CLI, Claude Code, Cursor, and other clients.",
    });
  }
  return t("kind.titles.all", { defaultValue: "All workspace and system activity." });
}

function periodTitle(period: ActivityPeriod, t: TFunction) {
  return t(`period.titles.${period}`, {
    defaultValue: `Show ${defaultPeriodLabel(period)} activity`,
  });
}

function defaultPeriodLabel(value: string) {
  if (value === "today") {
    return "Today";
  }
  if (value === "yesterday") {
    return "Yesterday";
  }
  if (value === "week") {
    return "Week";
  }
  if (value === "month") {
    return "Month";
  }
  return "All time";
}

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false,
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return undefined;
    }
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, [query]);

  return matches;
}

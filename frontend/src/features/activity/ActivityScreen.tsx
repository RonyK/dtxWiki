import { ChevronDown, ExternalLink, RotateCcw } from "lucide-react";
import type { TFunction } from "i18next";
import { useEffect, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";

import { apiErrorMessage } from "../../shared/api/client";
import { listActivity } from "../../shared/api/operations";
import type {
  ActivityItem,
  ActivityLane,
  ActivityPeriod,
} from "../../shared/api/operations";
import { getCurrentWorkspaceId } from "../../shared/api/workspace";
import { ActivityActorBadge } from "../../shared/components/ActivityActorBadge";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import {
  ACTIVITY_SUBTYPES,
  activitySubtype,
  activityTargetHref,
  isMcpActivityItem,
  isSystemActivityItem,
  presentActivityItem,
} from "../../shared/lib/activityLabels";
import { iconForActivityLane } from "../../shared/lib/activityLanes";
import {
  formatRelativeTimestamp,
  formatVersionTimestamp,
} from "../../shared/lib/datetime";
import { applyPeriodParam, parsePeriodParam } from "../../shared/lib/urlState";

const PERIODS: ActivityPeriod[] = ["today", "yesterday", "week", "month", "all"];
const ACTIVITY_KINDS = ["all", "workspace", "mcp", "system"] as const;
const LANES: Array<{ lane: ActivityLane; title: string; tone: string }> = [
  { lane: "pending", title: "Pending", tone: "Needs review" },
  { lane: "advisory", title: "Advisory", tone: "Needs attention" },
  { lane: "history", title: "History", tone: "Completed changes" },
];
type ActivityKind = (typeof ACTIVITY_KINDS)[number];

// The overview path returns no cursor, so a full fetch window is the cap signal:
// per-lane counts then read as lower bounds ("N+"), mirroring the Dashboard.
const OVERVIEW_LIMIT = 50;
// Dedicated lane pages load one page at a time (matches Review/Ask); "Load more"
// appends the next cursor page. Backend caps the activity limit at 100.
const LANE_PAGE_SIZE = 50;
type LanePageState = {
  workspaceId: string;
  items: ActivityItem[];
  nextCursor: string | null;
};

export function ActivityScreen() {
  const { t } = useTranslation("activity");
  const [searchParams, setSearchParams] = useSearchParams();
  const period = parsePeriod(searchParams.get("period"));
  const kind = parseActivityKind(searchParams.get("kind"));
  const subtype = parseActivitySubtype(kind, searchParams.get("subtype"));
  const serverSubTab = kind === "mcp" ? "traces" : undefined;
  const serverKind = serverActivityKind(kind);
  const { data: state, error } = useAsyncLoad<{
    items: ActivityItem[];
    unreadCount: number;
    hasMore: boolean;
  }>(
    async () => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new Error("Workspace not found");
      }
      const activity = await listActivity(workspaceId, {
        period,
        sub_tab: serverSubTab,
        kind: serverKind,
        limit: OVERVIEW_LIMIT,
      });
      return {
        items: activity.items,
        unreadCount: activity.unread_count,
        // No overview cursor exists, so a saturated fetch window means the
        // per-lane counts are lower bounds rather than exact totals.
        hasMore: activity.items.length >= OVERVIEW_LIMIT,
      };
    },
    [period, serverSubTab, serverKind],
    {
      errorMessage: t("errors.load", { defaultValue: "Failed to load Activity" }),
      fallbackData: { items: [], unreadCount: 0, hasMore: false },
    },
  );

  function updatePeriod(nextPeriod: ActivityPeriod) {
    setSearchParams(applyPeriodParam(searchParams, "period", nextPeriod, "all"), {
      replace: true,
    });
  }

  function updateKind(nextKind: ActivityKind) {
    const nextParams = new URLSearchParams(searchParams);
    // "all" is the default → keep it out of the URL; persist the others.
    if (nextKind === "all") {
      nextParams.delete("kind");
    } else {
      nextParams.set("kind", nextKind);
    }
    // Sub-types are per-Type, so switching Type resets the sub-type to "all".
    nextParams.delete("subtype");
    setSearchParams(nextParams, { replace: true });
  }

  function updateSubtype(nextSubtype: string) {
    const nextParams = new URLSearchParams(searchParams);
    if (nextSubtype === "all") {
      nextParams.delete("subtype");
    } else {
      nextParams.set("subtype", nextSubtype);
    }
    setSearchParams(nextParams, { replace: true });
  }

  function resetFilters() {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("period");
    nextParams.delete("kind");
    nextParams.delete("subtype");
    setSearchParams(nextParams, { replace: true });
  }

  const filteredItems = useMemo(
    () =>
      filterBySubtype(
        filterByActivityKind(state?.items ?? [], kind),
        kind,
        subtype,
      ),
    [kind, subtype, state?.items],
  );
  const grouped = useMemo(() => groupByLane(filteredItems), [filteredItems]);
  const lanes = useMemo(
    () =>
      LANES.map((entry) => ({
        ...entry,
        title: t(`lanes.${entry.lane}.title`, { defaultValue: entry.title }),
        tone: t(`lanes.${entry.lane}.tone`, { defaultValue: entry.tone }),
      })),
    [t],
  );
  const periodFilterLabel = t("period.label", {
    defaultValue: "Period",
  });
  const kindFilterLabel = t("kind.label", {
    defaultValue: "Type",
  });
  const subtypeFilterLabel = t("subtype.label", {
    defaultValue: "Sub-type",
  });

  if (!state) {
    return <RouteLoading />;
  }

  return (
    <div className="workspace-screen single-column activity-screen">
      <section className="feature-main">
        <Breadcrumbs items={[{ label: t("breadcrumb", { defaultValue: "Activity" }) }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{t("header.eyebrow", { defaultValue: "Operations timeline" })}</p>
            <h1>{t("header.title", { defaultValue: "Activity" })}</h1>
            <p className="feature-subtitle">
              {t("header.subtitle", {
                defaultValue:
                  "Pending, advisory, and completed workspace events.",
              })}
            </p>
          </div>
        </div>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <ActivityFiltersPanel
          kind={kind}
          kindFilterLabel={kindFilterLabel}
          onKindChange={updateKind}
          onPeriodChange={updatePeriod}
          onReset={resetFilters}
          onSubtypeChange={updateSubtype}
          period={period}
          periodFilterLabel={periodFilterLabel}
          subtype={subtype}
          subtypeFilterLabel={subtypeFilterLabel}
        />
        <section className="feature-section">
          <div className="metric-grid">
            {lanes.map(({ lane, title, tone }) => (
              <div className="metric-card" key={lane}>
                <div className="metric-card-title">
                  {iconForActivityLane(lane)}
                  <span>{title}</span>
                </div>
                <strong>
                  {grouped.get(lane)?.length ?? 0}
                </strong>
                <p>{tone}</p>
              </div>
            ))}
          </div>
          {state.hasMore ? (
            <p className="activity-window-notice" role="status">
              {t("overview.windowNotice", { count: OVERVIEW_LIMIT })}
            </p>
          ) : null}
        </section>
        {lanes.map(({ lane, title }) => (
          <ActivityLaneSection
            filterQuery={searchParams.toString()}
            hasMore={state.hasMore}
            kind={kind}
            key={lane}
            lane={lane}
            title={title}
            items={grouped.get(lane) ?? []}
          />
        ))}
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

function parsePeriod(value: string | null): ActivityPeriod {
  return parsePeriodParam(value, PERIODS, "all");
}

function parseActivityKind(value: string | null): ActivityKind {
  return ACTIVITY_KINDS.includes(value as ActivityKind)
    ? (value as ActivityKind)
    : "all";
}

// Map the UI kind to the server `kind` query param. The server applies the
// workspace/system split BEFORE its capped recency budget, so workspace failed
// jobs are not crowded out by newer system audit rows. "mcp" is requested via
// sub_tab=traces (not kind), and "all" omits the param.
function serverActivityKind(
  kind: ActivityKind,
): "workspace" | "system" | undefined {
  return kind === "workspace" || kind === "system" ? kind : undefined;
}

// Presentational backstop: the server already returns the kind slice (above),
// but this keeps the rendered list correct if the param is ever omitted and
// covers the in-memory "mcp" view, which is fetched via sub_tab.
function filterByActivityKind(items: ActivityItem[], kind: ActivityKind) {
  if (kind === "all") {
    return items;
  }
  if (kind === "mcp") {
    return items.filter(isMcpActivityItem);
  }
  return items.filter((item) =>
    kind === "system" ? isSystemActivityItem(item) : !isSystemActivityItem(item),
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

// The selected Type can carry a second-level sub-type. Validate against the
// per-Type set; unknown/absent → "all" (the default, kept out of the URL).
function parseActivitySubtype(kind: ActivityKind, value: string | null): string {
  const options = ACTIVITY_SUBTYPES[kind];
  return options && value && options.includes(value) ? value : "all";
}

function filterBySubtype(
  items: ActivityItem[],
  kind: ActivityKind,
  subtype: string,
) {
  if (subtype === "all" || !ACTIVITY_SUBTYPES[kind]) {
    return items;
  }
  return items.filter((item) => activitySubtype(item) === subtype);
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
      />
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
}: {
  kind: ActivityKind;
  subtype: string;
  period: ActivityPeriod;
  onReset: () => void;
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
  // No active filters means no chips and no Reset: rendering the bare
  // "Filtered" eyebrow over nothing would mislabel the default view.
  if (chips.length === 0) {
    return null;
  }
  return (
    <div className="activity-filter-summary">
      <span className="activity-filter-summary-label">
        {t("filterSummary.label", { defaultValue: "Filtered" })}
      </span>
      {chips.map((chip) => (
        <span className="activity-filter-chip" key={chip}>
          {chip}
        </span>
      ))}
      <button className="activity-filter-reset" onClick={onReset} type="button">
        <RotateCcw size={13} aria-hidden="true" />
        <span>{t("filterSummary.reset", { defaultValue: "Reset" })}</span>
      </button>
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
          <Link className="icon-button text-button" to="/settings/integrations">
            {t("actions.openMcpSetup", { defaultValue: "Open MCP setup" })}
          </Link>
          <Link className="icon-button text-button" to="/chats">
            {t("actions.openChats", { defaultValue: "Open Chats" })}
          </Link>
        </div>
      ) : (
        <div className="empty-state-actions">
          <Link className="icon-button text-button" to="/context">
            {t("actions.browseContext", { defaultValue: "Browse Context pages" })}
          </Link>
          <Link className="icon-button text-button" to="/context/new?from=context">
            {t("actions.createPage", { defaultValue: "Create a new page" })}
          </Link>
        </div>
      )}
    </div>
  );
}

const LANE_PREVIEW_LIMIT = 5;

function ActivityLaneSection({
  kind,
  lane,
  title,
  items,
  filterQuery,
  hasMore,
}: {
  kind: ActivityKind;
  lane: ActivityLane;
  title: string;
  items: ActivityItem[];
  filterQuery: string;
  hasMore: boolean;
}) {
  const { t } = useTranslation("activity");
  const laneHref = filterQuery
    ? `/activity/${lane}?${filterQuery}`
    : `/activity/${lane}`;
  return (
    <section className="feature-section">
      <ListPanelHeader
        title={title}
        count={items.length}
        // The overview endpoint does not identify which lane was truncated, so
        // never attach its global "+" signal to an individual lane count.
        approxMore={false}
        unit={t(items.length === 1 ? "units.event_one" : "units.event_other", {
          defaultValue: items.length === 1 ? "event" : "events",
        })}
      />
      {items.length === 0 ? (
        <ActivityLaneEmpty kind={kind} title={title} />
      ) : (
        <>
          <div className="operations-list">
            {items.slice(0, LANE_PREVIEW_LIMIT).map((item) => (
              <ActivityRow item={item} key={item.id} />
            ))}
          </div>
          {items.length > LANE_PREVIEW_LIMIT ? (
            <Link className="icon-button text-button activity-see-all" to={laneHref}>
              {/* When the overview fetch window is saturated, items.length is
                  only a lower bound, so drop the exact number from "See all". */}
              {hasMore
                ? t("actions.seeAllUnbounded", { defaultValue: "See all" })
                : t("actions.seeAll", {
                    defaultValue: "See all {{count}}",
                    count: items.length,
                  })}
            </Link>
          ) : null}
        </>
      )}
    </section>
  );
}

function ActivityRow({ item }: { item: ActivityItem }) {
  const { t, i18n } = useTranslation("activity");
  // A pending review row repeats decision_summary in title, body and
  // target_title, so the label mapping has to run on the resolved presentation
  // (presentActivityItem) rather than on item.title, which the server sets to
  // "Review required: <review_type>" and never to the summary prose.
  const presented = presentActivityItem(item);
  const targetHref = activityTargetHref(item);
  const targetKind = item.target?.target_kind;
  const targetKindLabel = targetKind
    ? t(`targetKinds.${targetKind}`, {
        defaultValue: targetKind.replace(/_/g, " "),
      })
    : null;
  // Show the real target name whenever it says something the headline
  // doesn't. For an event_key row (publish/move/rename/join-request) the
  // headline is a bare verb phrase -- "Published page" -- naming no page at
  // all, so the title here is the only place the reader learns which one.
  // For a page-create-style row the headline already IS the target name
  // (presentActivityItem prefers target_title as the title itself), so
  // repeating it would just be the same string twice; fall back to the kind
  // label there instead of duplicating it.
  const targetTitle = item.target_title?.trim() || null;
  const chipText =
    targetTitle && targetTitle !== presented.title
      ? targetTitle
      : targetKindLabel;
  return (
    <article className="activity-row">
      <div className="activity-row__main">
        <div className="activity-row__body">
          <p className="activity-row__title">
            <ActivityActorBadge actor={item.actor} />
            <span>{presented.title}</span>
          </p>
          {presented.body ? (
            <p className="activity-row__detail">{presented.body}</p>
          ) : null}
          {targetHref && chipText ? (
            <span className="row-badges activity-row__chips">
              {/* An explicit "Open target X" name, not just relying on the
                  visible text, matches Lint's chip: it stays correct even
                  when the visible label is the generic kind, not the title. */}
              <Link
                aria-label={t("actions.openTarget", {
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
          <time
            className="activity-relative-time"
            dateTime={item.created_at}
            title={formatVersionTimestamp(item.created_at, "long")}
          >
            {formatRelativeTimestamp(item.created_at, i18n.language)}
          </time>
          {item.review_item_id ? (
            // Deep-link to the exact review item: ReviewScreen restores
            // ?review=<id> via getReview even when it is beyond the first page.
            <Link
              className="icon-button text-button"
              to={`/review?review=${item.review_item_id}`}
            >
              {t("actions.review", { defaultValue: "Review" })}
            </Link>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function parseLane(value: string | undefined): ActivityLane | null {
  return LANES.some((entry) => entry.lane === value)
    ? (value as ActivityLane)
    : null;
}

/**
 * Dedicated full-list page for one lane (Pending / Advisory / History), reached
 * from the overview "See all" link. Carries period + kind via the URL so the
 * same filters apply, and pages the lane with a cursor-based "Load more"
 * (server next_cursor), matching the Review/Ask pattern.
 */
export function ActivityLanePage() {
  const { t } = useTranslation("activity");
  const { lane: laneParam } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const period = parsePeriod(searchParams.get("period"));
  const kind = parseActivityKind(searchParams.get("kind"));
  const subtype = parseActivitySubtype(kind, searchParams.get("subtype"));
  const serverSubTab = kind === "mcp" ? "traces" : undefined;
  const serverKind = serverActivityKind(kind);
  const lane = parseLane(laneParam);

  const [loadingMore, setLoadingMore] = useState(false);
  const {
    data,
    error,
    setData: setState,
    setError,
  } = useAsyncLoad<LanePageState>(
    async () => {
      if (!lane) {
        return { workspaceId: "", items: [], nextCursor: null };
      }
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new Error("Workspace not found");
      }
      const activity = await listActivity(workspaceId, {
        period,
        lane,
        sub_tab: serverSubTab,
        kind: serverKind,
        limit: LANE_PAGE_SIZE,
      });
      return {
        workspaceId,
        items: activity.items,
        nextCursor: activity.next_cursor,
      };
    },
    [lane, period, serverSubTab, serverKind],
    {
      errorMessage: t("errors.load", { defaultValue: "Failed to load Activity" }),
      fallbackData: { workspaceId: "", items: [], nextCursor: null },
    },
  );

  async function loadMore() {
    if (!data?.workspaceId || !data.nextCursor || !lane || loadingMore) {
      return;
    }
    setLoadingMore(true);
    try {
      const nextPage = await listActivity(data.workspaceId, {
        period,
        lane,
        sub_tab: serverSubTab,
        kind: serverKind,
        limit: LANE_PAGE_SIZE,
        cursor: data.nextCursor,
      });
      setState((current) =>
        current
          ? {
              ...current,
              items: [...current.items, ...nextPage.items],
              nextCursor: nextPage.next_cursor,
            }
          : current,
      );
    } catch (exc) {
      setError(
        apiErrorMessage(
          exc,
          t("errors.loadMore", { defaultValue: "Failed to load more" }),
        ),
      );
    } finally {
      setLoadingMore(false);
    }
  }

  function updatePeriod(nextPeriod: ActivityPeriod) {
    setSearchParams(applyPeriodParam(searchParams, "period", nextPeriod, "all"), {
      replace: true,
    });
  }

  function updateKind(nextKind: ActivityKind) {
    const nextParams = new URLSearchParams(searchParams);
    if (nextKind === "all") {
      nextParams.delete("kind");
    } else {
      nextParams.set("kind", nextKind);
    }
    nextParams.delete("subtype");
    setSearchParams(nextParams, { replace: true });
  }

  function updateSubtype(nextSubtype: string) {
    const nextParams = new URLSearchParams(searchParams);
    if (nextSubtype === "all") {
      nextParams.delete("subtype");
    } else {
      nextParams.set("subtype", nextSubtype);
    }
    setSearchParams(nextParams, { replace: true });
  }

  function resetFilters() {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("period");
    nextParams.delete("kind");
    nextParams.delete("subtype");
    setSearchParams(nextParams, { replace: true });
  }

  if (!lane) {
    return <Navigate replace to="/activity" />;
  }
  if (!data) {
    return <RouteLoading />;
  }

  const laneEntry = LANES.find((entry) => entry.lane === lane);
  const title = t(`lanes.${lane}.title`, {
    defaultValue: laneEntry?.title ?? lane,
  });
  const items = filterBySubtype(
    filterByActivityKind(data.items, kind),
    kind,
    subtype,
  );
  const periodFilterLabel = t("period.label", {
    defaultValue: "Period",
  });
  const kindFilterLabel = t("kind.label", {
    defaultValue: "Type",
  });
  const subtypeFilterLabel = t("subtype.label", {
    defaultValue: "Sub-type",
  });

  return (
    <div className="workspace-screen single-column activity-screen">
      <section className="feature-main">
        <Breadcrumbs
          items={[
            { label: t("breadcrumb", { defaultValue: "Activity" }), href: "/activity" },
            { label: title },
          ]}
        />
        <div className="feature-header">
          <div>
            <p className="eyebrow">
              {t("header.eyebrow", { defaultValue: "Operations timeline" })}
            </p>
            <h1>{title}</h1>
          </div>
          <CountChip
            value={items.length}
            label={t("header.eventUnit", {
              count: items.length,
              defaultValue: "events",
            })}
            compact
          />
        </div>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <ActivityFiltersPanel
          kind={kind}
          kindFilterLabel={kindFilterLabel}
          onKindChange={updateKind}
          onPeriodChange={updatePeriod}
          onReset={resetFilters}
          onSubtypeChange={updateSubtype}
          period={period}
          periodFilterLabel={periodFilterLabel}
          subtype={subtype}
          subtypeFilterLabel={subtypeFilterLabel}
        />
        <section className="feature-section">
          {items.length === 0 ? (
            <ActivityLaneEmpty
              filtered={data.items.length > 0}
              kind={kind}
              title={title}
            />
          ) : (
            <div className="operations-list">
              {items.map((item) => (
                <ActivityRow item={item} key={item.id} />
              ))}
            </div>
          )}
          {/* Load more lives OUTSIDE the empty branch: a kind/subtype filter can
              hide every row on this page while later pages still hold matches. */}
          {data.nextCursor ? (
            <div className="load-more-row">
              <button
                className="icon-button text-button"
                disabled={loadingMore}
                onClick={() => void loadMore()}
                type="button"
              >
                {loadingMore
                  ? t("loadingMore", { defaultValue: "Loading" })
                  : t("loadMore", { defaultValue: "Load more" })}
              </button>
            </div>
          ) : null}
        </section>
      </section>
    </div>
  );
}

function groupByLane(items: ActivityItem[]) {
  const grouped = new Map<ActivityLane, ActivityItem[]>();
  for (const item of items) {
    grouped.set(item.lane, [...(grouped.get(item.lane) ?? []), item]);
  }
  // The pending lane mixes review decisions and failed jobs and the API feed is
  // recency-ordered, so a burst of newer failed jobs could bury an older review
  // decision past the preview cut-off. Pin review decisions to the top of the
  // pending lane (stable sort preserves recency within each group); other lanes
  // keep the feed's recency order.
  const pending = grouped.get("pending");
  if (pending) {
    grouped.set(
      "pending",
      [...pending].sort(
        (a, b) => (a.review_item_id ? 0 : 1) - (b.review_item_id ? 0 : 1),
      ),
    );
  }
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

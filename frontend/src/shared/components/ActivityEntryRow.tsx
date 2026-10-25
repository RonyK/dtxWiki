import { useRouteWorkspaceId } from "../lib/workspaceRoutes";
import { Bot, ChevronRight, ClipboardCheck, FileText, Library, ListChecks, MessagesSquare, Unlink, type LucideIcon } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { ActivityFeedEntry, ActivityFeedEvent, ActivityGroupLoader, ActivityQuery } from "../lib/activityFeed";
import { activityNavigation, DEFAULT_ACTIVITY_QUERY, isMcpExecution, presentActivityFeedEvent } from "../lib/activityFeed";
import { isMcpActivityItem } from "../lib/activityLabels";
import { formatVersionTimestamp } from "../lib/datetime";
import { ActivityActionBadge } from "./ActivityActionBadge";
import { ActivityActorBadge } from "./ActivityActorBadge";
import { ActivityReadGroup } from "./ActivityReadGroup";
import { RelativeTime } from "./RelativeTime";

// The sidebar's glyph for each section, so a row's type reads like its menu.
const TYPE_ICONS: Record<string, LucideIcon> = { context: FileText, library: Library, agent: Bot, review: ListChecks, mcp: MessagesSquare };

/** A same-day group range drops the repeated date: "16:46 – 16:49". */
function rangeEnds(first: string, last: string): { from: string; to: string; spansDays: boolean } {
  const from = formatVersionTimestamp(first);
  const to = formatVersionTimestamp(last);
  const spansDays = from.slice(0, 10) !== to.slice(0, 10);
  return { from: spansDays ? from : from.slice(11), to: spansDays ? to : to.slice(11), spansDays };
}

export function ActivityEntryRow({ entry, query = DEFAULT_ACTIVITY_QUERY, loadGroup }: {
  entry: ActivityFeedEntry;
  query?: ActivityQuery;
  loadGroup?: ActivityGroupLoader;
}) {
  return (
    <article className={`activity-feed-entry${entry.entry_type === "read_group" ? " activity-feed-entry--group" : ""}`} data-entry-type={entry.entry_type}>
      <ActivityEventContent event={entry.event} query={query} count={entry.event_count} firstOccurredAt={entry.first_occurred_at} lastOccurredAt={entry.last_occurred_at} />
      {entry.entry_type === "read_group" && <ActivityReadGroup
        entry={entry}
        loadGroup={loadGroup}
        renderEvent={(event) => <ActivityEntryRow entry={{ entry_type: "event", id: event.id, event, event_count: 1, first_occurred_at: event.created_at, last_occurred_at: event.created_at }} query={query} />}
      />}
    </article>
  );
}

function ActivityEventContent({ event, query, count, firstOccurredAt, lastOccurredAt }: {
  event: ActivityFeedEvent; query: ActivityQuery; count: number; firstOccurredAt: string; lastOccurredAt: string;
}) {
  const { t, i18n } = useTranslation("activity");
  const descriptionId = useId();
  const presentation = presentActivityFeedEvent(event, count);
  const workspaceId = useRouteWorkspaceId();
  const navigation = activityNavigation(event, query, workspaceId);
  const { from: rangeFrom, to: rangeTo, spansDays } = rangeEnds(firstOccurredAt, lastOccurredAt);
  const TypeIcon = TYPE_ICONS[presentation.typeKey];
  const times = <div className="activity-entry__times" data-range-spans-days={count > 1 && spansDays ? true : undefined}>
    <RelativeTime value={lastOccurredAt} locale={i18n.language} />
    {count > 1 && <span className="activity-entry__time-range" aria-label={t("feed.group.timeRange")}>
      <time dateTime={firstOccurredAt} title={formatVersionTimestamp(firstOccurredAt, "long")}>{rangeFrom}</time>
      <span aria-hidden="true"> – </span>
      <time dateTime={lastOccurredAt} title={formatVersionTimestamp(lastOccurredAt, "long")}>{rangeTo}</time>
    </span>}
  </div>;
  const heading = <span className="activity-entry__heading"><span>{presentation.title}</span>{navigation.href && <ChevronRight aria-hidden="true" size={14} />}</span>;
  return (
    <div className={`activity-entry__layout${presentation.pendingReview ? " activity-entry__layout--review" : ""}`}>
      <div className="activity-entry__body">
        <div className="activity-entry__identity">
          <span className="activity-entry__type" data-kind={presentation.typeKey}>
            {TypeIcon && <TypeIcon className="activity-entry__type-icon" aria-hidden="true" size={14} />}
            <span>{presentation.typeLabel}</span>
          </span>
          <ActivityActorBadge actor={event.actor} />
        </div>
        {navigation.href ? <Link className="activity-entry__title-link" to={navigation.href} aria-label={presentation.pendingReview ? t("feed.openReview", { title: presentation.title }) : presentation.title} aria-describedby={presentation.body ? descriptionId : undefined}>
          {heading}
          {presentation.body && <span id={descriptionId} className="activity-entry__description">{presentation.body}</span>}
        </Link> : <div className="activity-entry__plain-title">{heading}{presentation.body && <p className="activity-entry__description">{presentation.body}</p>}</div>}
        {!navigation.href && <div className="activity-entry__fallback"><span><Unlink aria-hidden="true" size={12} />{t("feed.noTarget")}</span>
          {navigation.fallbackHref && <Link to={navigation.fallbackHref}>{t(isMcpActivityItem(event) ? "feed.openMcpActivity" : "feed.openActivity")}<ChevronRight aria-hidden="true" size={14} /></Link>}
        </div>}
        {isMcpExecution(event) && <ActivityTechnicalDetails event={event} />}
        {presentation.pendingReview && navigation.href && <div className="activity-entry__review-footer">
          <Link className="activity-entry__review-link" to={navigation.href} aria-label={t("feed.reviewRequiredFor", { title: presentation.title })}>
            <span className="activity-action-badge" data-action="pending"><ClipboardCheck aria-hidden="true" size={12} /><span>{t("feed.reviewRequired")}</span><ChevronRight aria-hidden="true" size={14} /></span>
          </Link>{times}
        </div>}
      </div>
      {!presentation.pendingReview && <div className="activity-entry__trailing"><ActivityActionBadge action={presentation.action} count={count} />{times}</div>}
    </div>
  );
}

function ActivityTechnicalDetails({ event }: { event: ActivityFeedEvent }) {
  const { t } = useTranslation("activity");
  const duration = typeof event.duration_ms === "number" && Number.isFinite(event.duration_ms) ? event.duration_ms : null;
  if (!event.tool_name && !event.result_status && !event.public_error_code && duration === null) return null;
  return <details className="activity-entry__technical">
    <summary>{t("feed.technical.title")}</summary>
    <dl>
      {event.tool_name && <><dt>{t("feed.technical.tool")}</dt><dd>{event.tool_name}</dd></>}
      {event.result_status && <><dt>{t("feed.technical.result")}</dt><dd>{event.result_status}</dd></>}
      {duration !== null && <><dt>{t("feed.technical.duration")}</dt><dd>{t("feed.technical.milliseconds", { count: duration })}</dd></>}
      {event.public_error_code && <><dt>{t("feed.technical.error")}</dt><dd>{event.public_error_code}</dd></>}
    </dl>
  </details>;
}

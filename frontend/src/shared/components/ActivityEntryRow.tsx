import { useRouteWorkspaceId } from "../lib/workspaceRoutes";
import { Bot, ChevronDown, ChevronRight, CircleAlert, CircleCheck, CircleHelp, ClipboardCheck, FileText, Library, Lightbulb, ListChecks, MessagesSquare, Timer, TriangleAlert, Unlink, Wrench, type LucideIcon } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { ActivityFeedEntry, ActivityFeedEvent, ActivityGroupLoader, ActivityQuery } from "../lib/activityFeed";
import { activityNavigation, DEFAULT_ACTIVITY_QUERY, isMcpExecution, presentActivityFeedEvent } from "../lib/activityFeed";
import { isMcpActivityItem } from "../lib/activityLabels";
import { formatVersionTimestamp } from "../lib/datetime";
import { isKnownMcpErrorCode, mcpErrorCodeLabel, mcpErrorGuidance, mcpStatusLabel, mcpStatusTone, mcpToolTitle, type McpStatusTone } from "../lib/mcpCallVocabulary";
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

/**
 * "summary" (Dashboard) shows what happened to which document; "detail"
 * (Activity) also offers the MCP call's technical fields, which the feed row
 * already carries, so neither density costs another request.
 */
export type ActivityEntryDensity = "summary" | "detail";

export function ActivityEntryRow({ entry, query = DEFAULT_ACTIVITY_QUERY, loadGroup, density = "detail" }: {
  entry: ActivityFeedEntry;
  query?: ActivityQuery;
  loadGroup?: ActivityGroupLoader;
  density?: ActivityEntryDensity;
}) {
  return (
    <article className={`activity-feed-entry${entry.entry_type === "read_group" ? " activity-feed-entry--group" : ""}`} data-entry-type={entry.entry_type}>
      <ActivityEventContent event={entry.event} query={query} count={entry.event_count} firstOccurredAt={entry.first_occurred_at} lastOccurredAt={entry.last_occurred_at} density={density} />
      {entry.entry_type === "read_group" && <ActivityReadGroup
        entry={entry}
        loadGroup={loadGroup}
        renderEvent={(event) => <ActivityEntryRow entry={{ entry_type: "event", id: event.id, event, event_count: 1, first_occurred_at: event.created_at, last_occurred_at: event.created_at }} query={query} density={density} />}
      />}
    </article>
  );
}

function ActivityEventContent({ event, query, count, firstOccurredAt, lastOccurredAt, density }: {
  event: ActivityFeedEvent; query: ActivityQuery; count: number; firstOccurredAt: string; lastOccurredAt: string; density: ActivityEntryDensity;
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
  const heading = <span className="activity-entry__heading"><span>{presentation.title}</span></span>;
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
        {density === "detail" && isMcpExecution(event) && <ActivityTechnicalDetails event={event} />}
        {presentation.pendingReview && <div className="activity-entry__review-footer">
          {/* A status, not a second link: the whole card already opens the review. */}
          <span className="activity-action-badge" data-action="pending"><ClipboardCheck aria-hidden="true" size={12} /><span>{t("feed.reviewRequired")}</span></span>{times}
        </div>}
      </div>
      {!presentation.pendingReview && <div className="activity-entry__trailing"><ActivityActionBadge action={presentation.action} count={count} />{times}</div>}
      {/* The title link stretches over the card; this only marks it as one target. */}
      {navigation.href && <ChevronRight className="activity-entry__go" aria-hidden="true" size={18} />}
    </div>
  );
}

const RESULT_ICONS: Record<McpStatusTone, LucideIcon> = { success: CircleCheck, partial: CircleAlert, failure: TriangleAlert, neutral: CircleHelp };

/**
 * The call's own fields in the words MCP Chats uses for them: the tool's
 * title before its ID, the status by name and tone, and a failure's meaning
 * and next step before its code. IDs and codes stay visible as quiet chips
 * for support and log searches.
 */
function ActivityTechnicalDetails({ event }: { event: ActivityFeedEvent }) {
  const { t } = useTranslation("activity");
  const { t: tChats } = useTranslation("chats");
  const duration = typeof event.duration_ms === "number" && Number.isFinite(event.duration_ms) && event.duration_ms >= 0 ? event.duration_ms : null;
  const toolName = event.tool_name ?? null;
  const status = event.result_status ?? null;
  const errorCode = event.public_error_code ?? null;
  if (!toolName && !status && !errorCode && duration === null) return null;
  const toolTitle = toolName ? mcpToolTitle(toolName, tChats) : null;
  const tone = status ? mcpStatusTone(status) : "neutral";
  const ResultIcon = RESULT_ICONS[tone];
  const errorKnown = errorCode !== null && isKnownMcpErrorCode(errorCode);
  const guidance = errorCode ? mcpErrorGuidance(errorCode, tChats) : null;
  return <details className="activity-entry__technical">
    <summary>
      <ChevronDown className="activity-call-details__chevron" aria-hidden="true" size={14} />
      <span className="activity-call-details__summary-label">{t("feed.technical.title")}</span>
      {toolName && <span className="activity-call-details__summary-tool">· {toolTitle ?? toolName}</span>}
    </summary>
    <div className="activity-call-details">
      <dl>
        {toolName && <div className="activity-call-details__row">
          <dt><Wrench aria-hidden="true" size={14} />{t("feed.technical.tool")}</dt>
          <dd>{toolTitle}<code className="activity-call-details__code">{toolName}</code></dd>
        </div>}
        {status && <div className="activity-call-details__row">
          <dt><ResultIcon aria-hidden="true" size={14} />{t("feed.technical.result")}</dt>
          <dd><span className="activity-call-details__status" data-tone={tone}>{mcpStatusLabel(status, tChats)}</span></dd>
        </div>}
        {duration !== null && <div className="activity-call-details__row">
          <dt><Timer aria-hidden="true" size={14} />{t("feed.technical.duration")}</dt>
          <dd>{duration < 1000 ? t("feed.technical.milliseconds", { count: duration }) : t("feed.technical.seconds", { value: (duration / 1000).toFixed(1) })}</dd>
        </div>}
      </dl>
      {errorCode && <div className="activity-call-details__failure">
        <Lightbulb aria-hidden="true" size={15} />
        {errorKnown && <strong>{mcpErrorCodeLabel(errorCode, tChats)}</strong>}
        {guidance && <p>{guidance}</p>}
        <span>{t("feed.technical.error")}<code className="activity-call-details__code">{errorCode}</code></span>
      </div>}
    </div>
  </details>;
}

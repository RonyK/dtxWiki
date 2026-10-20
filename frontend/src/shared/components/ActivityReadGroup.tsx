import { ChevronDown, ChevronUp } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ActivityFeedEntry, ActivityFeedEvent, ActivityGroupLoader } from "../lib/activityFeed";

export function ActivityReadGroup({ entry, loadGroup, renderEvent }: {
  entry: ActivityFeedEntry;
  loadGroup?: ActivityGroupLoader;
  renderEvent: (event: ActivityFeedEvent) => ReactNode;
}) {
  const { t } = useTranslation("activity");
  const id = useId();
  const toggle = useRef<HTMLButtonElement>(null);
  const active = useRef(true);
  const [expanded, setExpanded] = useState(false);
  const [events, setEvents] = useState<ActivityFeedEvent[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);

  const load = async () => {
    if (!loadGroup || !entry.group_token || loading) return;
    setLoading(true);
    setError(false);
    try {
      const result = await loadGroup(entry.group_token);
      if (active.current) setEvents(result.slice(0, 50));
    } catch {
      if (active.current) setError(true);
    } finally {
      if (active.current) setLoading(false);
    }
  };
  const collapse = () => { setExpanded(false); toggle.current?.focus(); };
  return (
    <div className="activity-read-group">
      <button
        ref={toggle}
        className="activity-read-group__toggle"
        type="button"
        aria-expanded={expanded}
        aria-controls={id}
        disabled={!entry.group_token || !loadGroup}
        onClick={() => {
          if (expanded) collapse();
          else { setExpanded(true); if (!events && !loading) void load(); }
        }}
      >
        {expanded ? <ChevronUp aria-hidden="true" size={14} /> : <ChevronDown aria-hidden="true" size={14} />}
        {t(expanded ? "feed.group.collapse" : "feed.group.expand", { count: entry.event_count })}
      </button>
      <div id={id} className="activity-read-group__members" hidden={!expanded}>
        {loading && <p className="activity-read-group__message" role="status">{t("feed.group.loading")}</p>}
        {error && <div className="activity-read-group__message"><p role="alert">{t("feed.group.error")}</p><button className="activity-read-group__retry" type="button" onClick={() => void load()}>{t("feed.group.retry")}</button></div>}
        {events && <>
          <ol className="activity-read-group__list">{events.map((event) => <li key={event.id}>{renderEvent(event)}</li>)}</ol>
          <button className="activity-read-group__collapse" type="button" onClick={collapse}><ChevronUp aria-hidden="true" size={14} />{t("feed.group.collapse")}</button>
        </>}
      </div>
    </div>
  );
}

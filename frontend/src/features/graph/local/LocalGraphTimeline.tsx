import { Activity, ArrowUpRight, Check, ChevronLeft, ChevronRight, Clock3, Info, MessageSquare, Minus, Pause, Play, Plus, SkipForward, Terminal, Triangle } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { useTranslation } from "react-i18next";
import type { GraphQueryPayload } from "../../../shared/api/graph";
import { formatVersionTimestamp } from "../../../shared/lib/datetime";
import { eventInvolves, GraphPreviewBadge, type LocalTimelineEvent } from "./LocalGraphChrome";
import type { TimelineMarkKind } from "./timelinePresentation";
import { nearestTimelineIndex, timelineLayout } from "./timelineLayout";
import { sessionActivityDigest } from "../sessionActivity";
import "./LocalGraphTimeline.css";

// A version mark is the canvas version glyph (a filled triangle); every
// activity mark shares the chat/activity colour and differs by icon.
const icons = { version: Triangle, review: Check, ask: MessageSquare, tool: Terminal, added: Plus, removed: Minus, activity: Activity };
function MarkIcon({ kind }: { kind: TimelineMarkKind }) {
  const Icon = icons[kind];
  return <Icon size={14} strokeWidth={2.5} aria-hidden="true" />;
}

/** One timeline mark glyph, shared with the history legend. */
export function LocalTimelineMark({ kind }: { kind: TimelineMarkKind }) {
  return <span aria-hidden="true" className={`local-history-symbol is-${kind}`}><MarkIcon kind={kind} /></span>;
}

export function LocalGraphTimeline({ cursor, onCursorChange, onPause, onPlay, onSpeedChange, onStep,
  onShowEvent, onHoverEvent, payload, playing, reducedMotion, selectedId, speed, timeline, restoredLatest }: {
  onHoverEvent: (id: string | null) => void;
  cursor: number; onCursorChange: (cursor: number) => void; onPause: () => void; onPlay: () => void;
  onSpeedChange: (speed: number) => void; onStep: (direction: -1 | 1) => void; onShowEvent: () => void;
  payload: GraphQueryPayload; playing: boolean; reducedMotion: boolean; selectedId: string | null;
  speed: number; timeline: LocalTimelineEvent[]; restoredLatest: boolean;
}) {
  const { t, i18n } = useTranslation("graph");
  const plotRef = useRef<HTMLDivElement>(null);
  const datesRef = useRef<HTMLDivElement>(null);
  const [metrics, setMetrics] = useState({ width: 400, unit: 32, gap: 12, pad: 4, inset: 22 });
  const [labelWidths, setLabelWidths] = useState<Record<string, number>>({});
  const [more, setMore] = useState({ left: false, right: false });
  const [dragging, setDragging] = useState(false);
  useEffect(() => {
    const plot = plotRef.current;
    if (!plot || typeof ResizeObserver === "undefined") return;
    // Column width, day gap and band pad come from the stylesheet, per layout.
    const observer = new ResizeObserver(() => {
      const style = getComputedStyle(plot);
      const px = (name: string, fallback: number) => Number.parseFloat(style.getPropertyValue(name)) || fallback;
      const inset = px("--history-plot-inset", 22);
      const next = { width: Math.max(1, plot.clientWidth - 2 * inset), unit: px("--history-mark-width", 32),
        gap: px("--history-day-gap", 12), pad: px("--history-day-pad", 4), inset };
      setMetrics((current) => (Object.keys(next) as (keyof typeof next)[]).every((key) => current[key] === next[key]) ? current : next);
    });
    observer.observe(plot);
    return () => observer.disconnect();
  }, [timeline.length]);
  const layout = useMemo(() => timelineLayout(timeline, { width: metrics.width, unit: metrics.unit, gap: metrics.gap, minWidth: labelWidths }),
    [labelWidths, metrics, timeline]);
  const dayFormats = useMemo(() => ({
    day: new Intl.DateTimeFormat(i18n.language, { month: "2-digit", day: "2-digit" }),
    year: new Intl.DateTimeFormat(i18n.language, { year: "numeric", month: "2-digit", day: "2-digit" }),
  }), [i18n.language]);
  const dayLabels = useMemo(() => layout.days.map((day) => (day.newYear ? dayFormats.year : dayFormats.day).format(new Date(`${day.key}T12:00:00`))),
    [dayFormats, layout.days]);
  // A day is never narrower than its date label, so labels never collide.
  useLayoutEffect(() => {
    const spans = datesRef.current?.children;
    if (!spans) return;
    const next = Object.fromEntries(layout.days.map((day, index) => [day.key, Math.ceil(spans[index]?.getBoundingClientRect().width ?? 0)]));
    setLabelWidths((current) => Object.keys(current).length === layout.days.length
      && layout.days.every((day) => current[day.key] === next[day.key]) ? current : next);
  }, [dayLabels, layout.days]);
  const event = timeline[cursor];
  const playhead = event ? layout.x[cursor] : 0;
  const currentDay = event ? layout.days.find((day) => day.indices.includes(cursor)) : undefined;
  const updateMore = useCallback(() => {
    const plot = plotRef.current;
    if (!plot) return;
    const left = plot.scrollLeft > 1;
    const right = plot.scrollLeft + plot.clientWidth < plot.scrollWidth - 1;
    setMore((current) => current.left === left && current.right === right ? current : { left, right });
  }, []);
  // While the days scroll, keep the current day whole in view (or the
  // playhead, when the day is wider than the view). Stepping inside a visible
  // day never scrolls, and nothing moves under a dragged scrubber.
  useLayoutEffect(() => {
    const plot = plotRef.current;
    if (plot && layout.scroll && !dragging) {
      const view = plot.clientWidth - 2 * metrics.inset;
      const [from, to] = currentDay && currentDay.width <= view
        ? [currentDay.left, currentDay.left + currentDay.width]
        : [playhead - metrics.unit / 2, playhead + metrics.unit / 2];
      if (from < plot.scrollLeft || to > plot.scrollLeft + view) {
        plot.scrollTo({ left: Math.max(0, (from + to - view) / 2), behavior: reducedMotion ? "auto" : "smooth" });
      }
    }
    updateMore();
  }, [currentDay, dragging, layout, metrics, playhead, reducedMotion, updateMore]);
  const lanes = payload.history.mode === "action" ? ["authoring", "tools"] : ["versions", "relations"];
  const markLabel = (item: LocalTimelineEvent, index: number) => `${index + 1}/${timeline.length} · ${formatVersionTimestamp(item.occurred_at)} · ${item.summary} · ${item.detail}`;
  const seekPointer = (e: PointerEvent<HTMLInputElement>) => {
    onCursorChange(nearestTimelineIndex(layout.x, e.clientX - e.currentTarget.getBoundingClientRect().left));
  };
  const digest = payload.history.mode === "action" ? sessionActivityDigest(payload, t) : null;
  return <section className="local-history" aria-labelledby="graph-timeline-heading" data-testid="graph-timeline">
    <header className="local-history-header">
      <div className="local-history-heading">
        <h4 id="graph-timeline-heading"><Clock3 size={16} aria-hidden="true" />{t("graph.timeline.heading")} <GraphPreviewBadge /></h4>
        <p>{payload.history.coverage_from ? formatVersionTimestamp(payload.history.coverage_from).split(" ")[0] : t("graph.timeline.unknown")}
          {" – "}{payload.history.watermark ? formatVersionTimestamp(payload.history.watermark).split(" ")[0] : t("graph.timeline.now")}
          <span aria-hidden="true"> · </span>{t("graph.historyUi.records", { count: timeline.length })}</p>
        {payload.history.status !== "complete" ? <p className="local-history-partial"><Info size={12} aria-hidden="true" />{t("graph.historyUi.partial")}</p> : null}
        {digest ? <p className="local-history-digest" data-testid="graph-session-digest">{digest}</p> : null}
      </div>
      <div className="local-history-controls" role="group" aria-label={t("graph.timeline.heading")}>
        <div className="local-history-transport">
          <button type="button" disabled={cursor < 0 || !timeline.length} aria-label={t("graph.timeline.previous")} onClick={() => onStep(-1)}><ChevronLeft size={18} aria-hidden="true" /></button>
          <button className="local-history-play" type="button" disabled={!timeline.length} aria-label={t(playing ? "graph.timeline.pause" : "graph.timeline.play")} onClick={playing ? onPause : onPlay}>{playing ? <Pause size={17} aria-hidden="true" /> : <Play size={17} aria-hidden="true" />}</button>
          <button type="button" disabled={cursor >= timeline.length - 1 || !timeline.length} aria-label={t("graph.timeline.next")} onClick={() => onStep(1)}><ChevronRight size={18} aria-hidden="true" /></button>
        </div>
        <button className="local-history-latest" type="button" disabled={!timeline.length} onClick={() => onCursorChange(timeline.length - 1)}>{t("graph.historyUi.latest")} <SkipForward size={14} aria-hidden="true" /></button>
        <select aria-label={t("graph.timeline.speed")} disabled={reducedMotion || !timeline.length} onChange={(e) => onSpeedChange(Number(e.target.value))} value={speed}>
          {[0.5, 1, 2].map((value) => <option key={value} value={value}>{value}×</option>)}
        </select>
      </div>
    </header>
    {restoredLatest ? <p className="local-history-restored" role="status">{t("graph.historyUi.restoredLatest")}</p> : null}
    {!timeline.length ? <p className="local-history-empty">{t("graph.timeline.noEvents")}</p> : <>
      <div className="local-history-chart">
        <div className="local-history-lane-labels">{lanes.map((lane) => <span key={lane}>{t(`graph.historyUi.${lane}`)}</span>)}</div>
        <div className={`local-history-plot${layout.scroll ? " is-scroll" : ""}${layout.scroll && more.left ? " has-more-left" : ""}${layout.scroll && more.right ? " has-more-right" : ""}`}
          ref={plotRef} onScroll={updateMore}>
          <div className="local-history-track" style={{ width: layout.width }}>
            <div className="local-history-axis">
              <div className="local-history-future" style={event ? { left: playhead } : undefined} aria-hidden="true" />
              <div className="local-history-playhead" style={{ left: playhead }} aria-hidden="true" />
              {lanes.map((lane, laneIndex) => <div key={lane} className="local-history-lane" role="group" aria-label={t(`graph.historyUi.${lane}`)}>
                {/* Each day is its own band; playback fills the bands purple from the first record up to the playhead. */}
                {layout.days.map((day) => <span key={day.key} className="local-history-day" style={{ left: day.left - metrics.pad, width: day.width + 2 * metrics.pad }} aria-hidden="true">
                  {event ? <span className="local-history-day-progress"
                    style={{ width: Math.max(0, Math.min(day.width + 2 * metrics.pad, playhead - day.left + metrics.pad)) }} /> : null}
                </span>)}
                {timeline.map((item, index) => item.lane !== laneIndex ? null : <button key={item.id} type="button"
                  className={`local-history-mark ${index === cursor ? "is-current" : ""} ${selectedId ? eventInvolves(item, selectedId, payload.edges) ? "is-related" : "is-quiet" : ""} ${index > cursor ? "is-future" : ""}`}
                  style={{ left: layout.x[index] }} title={markLabel(item, index)} aria-label={markLabel(item, index)}
                  aria-current={index === cursor ? "step" : undefined}
                  onMouseEnter={() => onHoverEvent(item.id)} onMouseLeave={() => onHoverEvent(null)}
                  onFocus={() => onHoverEvent(item.id)} onBlur={() => onHoverEvent(null)}
                  onClick={() => onCursorChange(index)}>
                  <span className={`local-history-symbol is-${item.mark}`}><MarkIcon kind={item.mark} /></span>
                </button>)}
              </div>)}
              <input className="local-history-scrubber" type="range" min={0} max={timeline.length} value={Math.max(0, cursor + 1)}
                aria-label={t("graph.timeline.scrubber")} aria-valuetext={event ? markLabel(event, cursor) : t("graph.timeline.start")}
                onChange={(e) => onCursorChange(Number(e.target.value) - 1)}
                onPointerDown={(e) => {
                  e.preventDefault();
                  e.currentTarget.focus({ preventScroll: true });
                  e.currentTarget.setPointerCapture(e.pointerId);
                  // Hold the days still under the pointer: stop a smooth scroll in flight.
                  const plot = plotRef.current;
                  if (plot && layout.scroll) plot.scrollTo({ left: plot.scrollLeft, behavior: "auto" });
                  setDragging(true);
                  seekPointer(e);
                }}
                onPointerMove={(e) => { if (e.currentTarget.hasPointerCapture(e.pointerId)) seekPointer(e); }}
                onPointerUp={(e) => { if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId); }}
                onLostPointerCapture={() => setDragging(false)} />
            </div>
            <div className="local-history-dates" ref={datesRef} aria-hidden="true">
              {layout.days.map((day, index) => <span key={day.key} className={day === currentDay ? "is-current" : undefined}
                data-label={dayLabels[index]} style={{ left: day.left + day.width / 2 }}>{dayLabels[index]}</span>)}
            </div>
          </div>
        </div>
      </div>
      <div className="local-history-event" aria-live={playing ? "off" : "polite"}>
        {event ? <>
          <span className={`local-history-symbol is-${event.mark}`}><MarkIcon kind={event.mark} /></span>
          <div className="local-history-event-copy"><strong title={event.summary}>{event.summary}</strong>
            <p title={event.detail}>{event.detail}</p><time dateTime={event.occurred_at}>{formatVersionTimestamp(event.occurred_at)}</time>
            <span className="local-history-event-position">{cursor + 1} / {timeline.length}</span>
            {!event.visible_change ? <small>{t("graph.timeline.filteredEvent")}</small> : null}</div>
          <button type="button" disabled={!event.visible_change} onClick={onShowEvent} aria-label={t("graph.historyUi.showInGraph")}>
            <span className="local-history-label-long">{t("graph.historyUi.showInGraph")}</span>
            <span className="local-history-label-short">{t("graph.historyUi.show")}</span><ArrowUpRight size={14} aria-hidden="true" /></button>
        </> : <p className="local-history-start"><span className="local-history-position">0 / {timeline.length}</span>{t("graph.timeline.start")}</p>}
      </div>
      {reducedMotion ? <p className="local-history-note">{t("graph.timeline.reducedMotion")}</p> : null}
    </>}
  </section>;
}

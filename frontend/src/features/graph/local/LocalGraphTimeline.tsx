import { Activity, ArrowUpRight, Check, ChevronLeft, ChevronRight, Clock3, Info, MessageSquare, Minus, Pause, Play, Plus, SkipForward, Terminal, Triangle } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { useTranslation } from "react-i18next";
import type { GraphQueryPayload, GraphTimelineEvent } from "../../../shared/api/graph";
import { formatVersionTimestamp } from "../../../shared/lib/datetime";
import { eventInvolves, GraphPreviewBadge, type LocalTimelineEvent } from "./LocalGraphChrome";
import type { TimelineMarkKind } from "./timelinePresentation";
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
  const { t } = useTranslation("graph");
  const axisRef = useRef<HTMLDivElement>(null);
  const [axis, setAxis] = useState({ width: 400, markWidth: 44 });
  const [openGroup, setOpenGroup] = useState<string | null>(null);
  useEffect(() => {
    if (!axisRef.current || typeof ResizeObserver === "undefined") return;
    // Marks closer than one hit width (--history-mark-width, set per layout in
    // the stylesheet) merge into a group so their buttons never overlap.
    const observer = new ResizeObserver(([entry]) => {
      const markWidth = Number.parseFloat(getComputedStyle(entry.target).getPropertyValue("--history-mark-width"));
      setAxis({ width: Math.max(1, entry.contentRect.width), markWidth: markWidth > 0 ? markWidth : 44 });
    });
    observer.observe(axisRef.current);
    return () => observer.disconnect();
  }, [timeline.length]);
  const event = timeline[cursor];
  const first = timeline.length ? Date.parse(timeline[0].occurred_at) : 0;
  const last = timeline.length ? Date.parse(timeline[timeline.length - 1].occurred_at) : first;
  const position = (item: GraphTimelineEvent) => last > first ? (Date.parse(item.occurred_at) - first) / (last - first) * 100 : 50;
  const playhead = event ? position(event) : 0;
  const lanes = payload.history.mode === "action" ? ["authoring", "tools"] : ["versions", "relations"];
  const groups = lanes.map((_, lane) => {
    const result: Array<{ id: string; left: number; indices: number[] }> = [];
    timeline.forEach((item, index) => {
      if (item.lane !== lane) return;
      const left = position(item);
      const previous = result[result.length - 1];
      if (previous && (left - previous.left) / 100 * axis.width < axis.markWidth) previous.indices.push(index);
      else result.push({ id: item.id, left, indices: [index] });
    });
    return result;
  });
  const expanded = groups.flat().find((group) => group.id === openGroup);
  const closeGroup = () => {
    axisRef.current?.querySelector<HTMLButtonElement>('[aria-expanded="true"]')?.focus();
    setOpenGroup(null);
  };
  const closeOnEscape = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "Escape" && expanded) { e.preventDefault(); e.stopPropagation(); closeGroup(); }
  };
  const markLabel = (item: LocalTimelineEvent, index: number) => `${index + 1}/${timeline.length} · ${formatVersionTimestamp(item.occurred_at)} · ${item.summary} · ${item.detail}`;
  const seekPointer = (e: PointerEvent<HTMLInputElement>) => {
    const bounds = e.currentTarget.getBoundingClientRect();
    const at = first + Math.max(0, Math.min(1, (e.clientX - bounds.left) / bounds.width)) * (last - first);
    const closest = timeline.reduce((best, item, index) => Math.abs(Date.parse(item.occurred_at) - at) < Math.abs(Date.parse(timeline[best].occurred_at) - at) ? index : best, 0);
    onCursorChange(closest);
  };
  const digest = payload.history.mode === "action" ? sessionActivityDigest(payload, t) : null;
  const shortDate = (at: number) => new Intl.DateTimeFormat(undefined, { month: "2-digit", day: "2-digit" }).format(at);
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
        <div className="local-history-plot">
          <div className="local-history-axis" ref={axisRef}>
            <div className="local-history-future" style={event ? { left: `${playhead}%` } : undefined} aria-hidden="true" />
            <div className="local-history-playhead" style={{ left: `${playhead}%` }} aria-hidden="true" />
            {groups.map((lane, laneIndex) => <div key={lanes[laneIndex]} className="local-history-lane" role="group" aria-label={t(`graph.historyUi.${lanes[laneIndex]}`)}>
              {lane.map((group) => {
                const index = group.indices.includes(cursor) ? cursor : group.indices[0];
                const item = timeline[index];
                const clustered = group.indices.length > 1;
                const related = group.indices.some((i) => eventInvolves(timeline[i], selectedId, payload.edges));
                const current = group.indices.includes(cursor);
                return <button key={group.id} type="button" className={`local-history-mark ${current ? "is-current" : ""} ${selectedId ? related ? "is-related" : "is-quiet" : ""} ${index > cursor ? "is-future" : ""}`}
                  style={{ left: `${group.left}%` }} title={clustered ? t("graph.historyUi.group", { count: group.indices.length }) : markLabel(item, index)}
                  aria-label={clustered ? t("graph.historyUi.group", { count: group.indices.length }) : markLabel(item, index)}
                  aria-current={current ? "step" : undefined} aria-expanded={clustered ? openGroup === group.id : undefined}
                  onKeyDownCapture={closeOnEscape}
                  onMouseEnter={() => !clustered && onHoverEvent(item.id)} onMouseLeave={() => onHoverEvent(null)}
                  onFocus={() => !clustered && onHoverEvent(item.id)} onBlur={() => onHoverEvent(null)}
                  onClick={() => { if (clustered) { onPause(); setOpenGroup(openGroup === group.id ? null : group.id); } else { setOpenGroup(null); onCursorChange(index); } }}>
                  <span className={`local-history-symbol is-${clustered ? "cluster" : item.mark}`}>{clustered ? group.indices.length : <MarkIcon kind={item.mark} />}</span>
                </button>;
              })}
            </div>)}
            <input className="local-history-scrubber" type="range" min={0} max={timeline.length} value={Math.max(0, cursor + 1)}
              aria-label={t("graph.timeline.scrubber")} aria-valuetext={event ? markLabel(event, cursor) : t("graph.timeline.start")}
              onChange={(e) => onCursorChange(Number(e.target.value) - 1)}
              onPointerDown={(e) => { e.preventDefault(); e.currentTarget.focus(); e.currentTarget.setPointerCapture(e.pointerId); seekPointer(e); }}
              onPointerMove={(e) => { if (e.currentTarget.hasPointerCapture(e.pointerId)) seekPointer(e); }}
              onPointerUp={(e) => { if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId); }} />
          </div>
          <div className="local-history-dates" aria-hidden="true"><span>{shortDate(first)}</span><span>{last > first ? shortDate(first + (last - first) / 2) : ""}</span><span>{shortDate(last)}</span></div>
        </div>
      </div>
      {expanded ? <div className="local-history-group" role="group" aria-label={t("graph.historyUi.group", { count: expanded.indices.length })}>
        <div className="local-history-group-heading"><strong>{t("graph.historyUi.groupRecords", { count: expanded.indices.length })}</strong><button type="button" onClick={closeGroup} onKeyDownCapture={closeOnEscape}>{t("graph.historyUi.closeGroup")}</button></div>
        {expanded.indices.map((i) => <button className="local-history-record" key={timeline[i].id} type="button" aria-current={cursor === i ? "step" : undefined}
          onKeyDownCapture={closeOnEscape}
          onMouseEnter={() => onHoverEvent(timeline[i].id)} onMouseLeave={() => onHoverEvent(null)}
          onFocus={() => onHoverEvent(timeline[i].id)} onBlur={() => onHoverEvent(null)}
          onClick={() => onCursorChange(i)}>
          <span className={`local-history-symbol is-${timeline[i].mark}`}><MarkIcon kind={timeline[i].mark} /></span>
          <span><strong>{timeline[i].summary}</strong><small>{timeline[i].detail}</small></span><time>{formatVersionTimestamp(timeline[i].occurred_at)}</time>
        </button>)}
      </div> : null}
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

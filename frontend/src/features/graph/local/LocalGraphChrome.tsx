import { Activity, Clock3, History, Info, Network, Pause, Play, Scan, StepBack, StepForward, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import type { GraphEdge, GraphHistoryMode, GraphQueryPayload, GraphRange, GraphTimelineEvent } from "../../../shared/api/graph";
import { formatVersionTimestamp } from "../../../shared/lib/datetime";
import { graphActionFamily } from "../graphModel";

export function GraphPreviewBadge() {
  const { t } = useTranslation("shell");
  return <span className="nav-preview-badge" title={t("preview.description")}>
    {t("preview.badge")}
  </span>;
}

function navigateChoices(event: KeyboardEvent<HTMLButtonElement>) {
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  const choices = [...(event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
  if (!choices.length) return;
  event.preventDefault();
  const index = choices.indexOf(event.target as HTMLButtonElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? choices.length - 1
    : (index + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1) + choices.length) % choices.length;
  choices[next].focus();
  choices[next].click();
}

export function GraphTypeSelector({ mode, busy, onChange }: {
  mode: GraphHistoryMode; busy: boolean; onChange: (mode: GraphHistoryMode) => void;
}) {
  const { t } = useTranslation("graph");
  const modes = ["current", "appearance", "action"] as const;
  const icons = { current: Network, appearance: History, action: Activity };
  return <div className="graph-type-section">
    <div className="graph-type-row">
      <div className="graph-type-options" role="group" aria-label={t("graph.view.heading")} aria-busy={busy}>
        {modes.map((value) => {
          const Icon = icons[value];
          return <button key={value} type="button" aria-pressed={mode === value} disabled={busy}
            onClick={() => onChange(value)} onKeyDown={navigateChoices}>
            <Icon size={17} aria-hidden="true" /><span>{t(`graph.view.${value}.name`)}</span>
            {value !== "current" ? <GraphPreviewBadge /> : null}
          </button>;
        })}
      </div>
      <details className="graph-type-comparison">
        <summary><Info size={16} aria-hidden="true" />{t("graph.view.compare")}</summary>
        {/* Keyboard users can scroll the comparison table horizontally. */}
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
        <div className="graph-type-table" tabIndex={0} role="region" aria-label={t("graph.view.compare")}>
          <table><thead><tr><th>{t("graph.view.heading")}</th>{modes.map((value) => <th key={value}>
            {t(`graph.view.${value}.name`)} {value !== "current" ? <GraphPreviewBadge /> : null}
          </th>)}</tr></thead><tbody>
            {(["question", "nodes", "edges", "time", "scope", "limits"] as const).map((row) => <tr key={row}>
              <th scope="row">{t(`graph.view.rows.${row}`)}</th>
              {modes.map((value) => <td key={value}>{t(`graph.view.${value}.${row}`)}</td>)}
            </tr>)}
          </tbody></table>
        </div>
      </details>
    </div>
    <p className="graph-type-description"><strong>{t(`graph.view.${mode}.question`)}</strong>{" — "}{t(`graph.view.${mode}.description`)}</p>
  </div>;
}

function RangeIcon({ range }: { range: GraphRange }) {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12 12 4 5M12 12 4 19M12 12 20 5M12 12 20 19" stroke="currentColor" fill="none" />
    {range !== "direct" ? <path d="M4 5V19M20 5V19M4 5H20" stroke="currentColor" fill="none" /> : null}
    <circle cx="12" cy="12" r="3" /><circle cx="4" cy="5" r="2" /><circle cx="4" cy="19" r="2" />
    <circle cx="20" cy="5" r="2" /><circle cx="20" cy="19" r="2" />
    {range === "extended" ? <><path d="M4 5 2 12M20 5 22 12" stroke="currentColor" /><circle cx="2" cy="12" r="1.5" /><circle cx="22" cy="12" r="1.5" /></> : null}
  </svg>;
}

export function GraphScopeSelector({ range, historical, busy, onChange }: {
  range: GraphRange; historical: boolean; busy: boolean; onChange: (range: GraphRange) => void;
}) {
  const { t } = useTranslation("graph");
  return <div className="graph-scope-control" role="group" aria-label={t("graph.range.heading")} aria-busy={busy}>
    <span className="graph-local-toolbar-title">{t("graph.range.heading")}</span>
    <div className="graph-scope-options">{(["direct", "neighbors", "extended"] as const).map((value) => <button
      key={value} type="button" aria-label={t(`graph.range.${value}.name`)} aria-pressed={range === value}
      disabled={busy || historical && value !== "direct"}
      title={historical && value !== "direct" ? t("graph.range.historyOnly") : t(`graph.range.${value}.description`)}
      onClick={() => onChange(value)} onKeyDown={navigateChoices}>
      <RangeIcon range={value} /><span className="graph-scope-full">{t(`graph.range.${value}.name`)}</span>
      <span className="graph-scope-short">{t(`graph.range.${value}.short`)}</span>
    </button>)}</div>
  </div>;
}

export function GraphCameraControls({ onZoomIn, onZoomOut, onFit, disabled }: {
  disabled: boolean;
  onZoomIn: () => void; onZoomOut: () => void; onFit: () => void;
}) {
  const { t } = useTranslation("graph");
  return <div className="graph-camera-controls" role="toolbar" aria-label={t("graph.canvasControls")}>
    {[["zoomIn", ZoomIn, onZoomIn], ["zoomOut", ZoomOut, onZoomOut], ["fit", Scan, onFit]].map(([key, Icon, onClick]) => {
      const ButtonIcon = Icon as typeof Scan;
      return <button className="icon-button" type="button" key={key as string} disabled={disabled}
        aria-label={t(`graph.${key as string}`)} title={t(`graph.${key as string}`)} onClick={onClick as () => void}>
        <ButtonIcon size={17} aria-hidden="true" />
      </button>;
    })}
  </div>;
}

export type LocalTimelineEvent = GraphTimelineEvent & { visible_change: boolean };
export function eventInvolves(event: GraphTimelineEvent, id: string | null, edges: readonly GraphEdge[] = []): boolean {
  return !!id && ([...(event.node_ids ?? []), ...(event.edge_ids ?? []),
    ...(event.node_snapshots ?? []).map((node) => node.id),
    ...(event.edge_snapshots ?? []).flatMap((edge) => [edge.id, edge.source, edge.target]),
    ...edges.filter((edge) => event.edge_ids?.includes(edge.id)).flatMap((edge) => [edge.source, edge.target])].includes(id));
}

export function LocalGraphTimeline({ cursor, onCursorChange, onPause, onPlay, onSpeedChange, onStep,
  onShowEvent, onHoverEvent, payload, playing, reducedMotion, selectedId, speed, timeline }: {
  onHoverEvent: (id: string | null) => void;
  cursor: number; onCursorChange: (cursor: number) => void; onPause: () => void; onPlay: () => void;
  onSpeedChange: (speed: number) => void; onStep: (direction: -1 | 1) => void; onShowEvent: () => void;
  payload: GraphQueryPayload; playing: boolean; reducedMotion: boolean; selectedId: string | null;
  speed: number; timeline: LocalTimelineEvent[];
}) {
  const { t } = useTranslation("graph");
  const axisRef = useRef<HTMLDivElement>(null);
  const [axisWidth, setAxisWidth] = useState(400);
  useEffect(() => {
    if (!axisRef.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => setAxisWidth(Math.max(1, entry.contentRect.width)));
    observer.observe(axisRef.current);
    return () => observer.disconnect();
  }, []);
  const event = timeline[cursor];
  const first = timeline.length ? Date.parse(timeline[0].occurred_at) : 0;
  const last = timeline.length ? Date.parse(timeline[timeline.length - 1].occurred_at) : first;
  const position = (item: GraphTimelineEvent) => last > first ? (Date.parse(item.occurred_at) - first) / (last - first) * 100 : 50;
  const playhead = event ? position(event) : 0;
  const lanes = payload.history.mode === "action" ? ["authoring", "tools"] : ["versions", "relations"];
  const laneFor = (item: GraphTimelineEvent) => payload.history.mode === "action"
    ? (["authoring", "governance"].includes(graphActionFamily(item.action_kind)) ? 0 : 1)
    : item.operation === "node_created" ? 0 : 1;
  const groups = lanes.map((_, lane) => {
    const result: Array<{ left: number; indices: number[] }> = [];
    timeline.forEach((item, index) => {
      if (laneFor(item) !== lane) return;
      const left = position(item);
      const previous = result[result.length - 1];
      if (previous && (left - previous.left) / 100 * axisWidth < 42) previous.indices.push(index);
      else result.push({ left, indices: [index] });
    });
    return result;
  });
  const markLabel = (item: LocalTimelineEvent, index: number) => `${index + 1}/${timeline.length} · ${formatVersionTimestamp(item.occurred_at)} · ${item.label}`;
  return <section className="graph-timeline" aria-labelledby="graph-timeline-heading">
    <div className="graph-timeline-heading-row">
      <h4 id="graph-timeline-heading"><Clock3 size={17} aria-hidden="true" />{t("graph.timeline.heading")} <GraphPreviewBadge /></h4>
      <span className="graph-timeline-coverage">{payload.history.coverage_from ? formatVersionTimestamp(payload.history.coverage_from).split(" ")[0] : t("graph.timeline.unknown")}
        {" – "}{payload.history.watermark ? formatVersionTimestamp(payload.history.watermark).split(" ")[0] : t("graph.timeline.now")}
        {" · "}{t("graph.historyUi.records", { count: timeline.length })}</span>
      {payload.history.status !== "complete" ? <span className="graph-history-partial">{t("graph.historyUi.partial")}</span> : null}
      <div className="graph-timeline-controls" role="group" aria-label={t("graph.timeline.heading")}>
        <button className="icon-button" type="button" disabled={cursor < 0 || !timeline.length} aria-label={t("graph.timeline.previous")} onClick={() => onStep(-1)}><StepBack size={17} aria-hidden="true" /></button>
        <button className="icon-button graph-timeline-play" type="button" disabled={!timeline.length} aria-label={t(playing ? "graph.timeline.pause" : "graph.timeline.play")} onClick={playing ? onPause : onPlay}>{playing ? <Pause size={17} aria-hidden="true" /> : <Play size={17} aria-hidden="true" />}</button>
        <button className="icon-button" type="button" disabled={cursor >= timeline.length - 1 || !timeline.length} aria-label={t("graph.timeline.next")} onClick={() => onStep(1)}><StepForward size={17} aria-hidden="true" /></button>
        <button className="icon-button text-button" type="button" disabled={!timeline.length} onClick={() => onCursorChange(timeline.length - 1)}>{t("graph.historyUi.latest")} <StepForward size={15} aria-hidden="true" /></button>
        <select aria-label={t("graph.timeline.speed")} disabled={reducedMotion || !timeline.length} onChange={(e) => onSpeedChange(Number(e.target.value))} value={speed}>
          {[0.5, 1, 2].map((value) => <option key={value} value={value}>{value}×</option>)}
        </select>
      </div>
    </div>
    {!timeline.length ? <p className="graph-timeline-event">{t("graph.timeline.noEvents")}</p> : <>
      {reducedMotion ? <p className="graph-timeline-motion-note">{t("graph.timeline.reducedMotion")}</p> : null}
      <div className="graph-history-axis-grid">
        <div className="graph-history-lane-names">{lanes.map((lane) => <span key={lane}>{t(`graph.historyUi.${lane}`)}</span>)}</div>
        <div className="graph-history-axis" ref={axisRef}>
          <div className="graph-history-future" style={{ left: `${playhead}%` }} />
          <div className="graph-history-playhead" style={{ left: `${playhead}%` }}><span>{event ? formatVersionTimestamp(event.occurred_at) : t("graph.timeline.start")}</span></div>
          {groups.map((lane, laneIndex) => <div key={lanes[laneIndex]} className="graph-history-lane" role="group" aria-label={t(`graph.historyUi.${lanes[laneIndex]}`)}>
            {lane.map((group) => {
              const index = group.indices.includes(cursor) ? cursor : group.indices[0];
              const item = timeline[index];
              const related = group.indices.some((i) => eventInvolves(timeline[i], selectedId, payload.edges));
              const className = `graph-history-mark ${laneIndex === 0 ? "is-version" : ""} ${item.operation === "edge_removed" ? "is-removed" : ""} ${group.indices.includes(cursor) ? "is-current" : ""} ${selectedId ? related ? "is-related" : "is-quiet" : ""} ${index > cursor ? "is-future" : ""}`;
              const symbol = laneIndex === 0 ? "◆" : item.operation === "edge_removed" ? "−" : "+";
              return group.indices.length === 1 ? <button key={item.id} type="button" className={className}
                style={{ left: `${group.left}%` }} title={markLabel(item, index)} aria-label={markLabel(item, index)} aria-current={cursor === index ? "step" : undefined}
                onMouseEnter={() => onHoverEvent(item.id)} onMouseLeave={() => onHoverEvent(null)}
                onFocus={() => onHoverEvent(item.id)} onBlur={() => onHoverEvent(null)}
                onClick={() => onCursorChange(index)}>{symbol}</button>
                : <details key={item.id} className={`graph-history-cluster ${className}`} style={{ left: `${group.left}%` }}>
                  <summary aria-label={t("graph.historyUi.group", { count: group.indices.length })}>{group.indices.length}</summary>
                  <div>{group.indices.map((i) => <button key={timeline[i].id} type="button" aria-current={cursor === i ? "step" : undefined}
                    onClick={(e) => { onCursorChange(i); e.currentTarget.closest("details")?.removeAttribute("open"); }}>{markLabel(timeline[i], i)}</button>)}</div>
                </details>;
            })}
          </div>)}
          <input className="graph-history-scrubber"
            onPointerDown={(e) => {
              e.preventDefault(); e.currentTarget.focus(); e.currentTarget.setPointerCapture(e.pointerId);
              const bounds = e.currentTarget.getBoundingClientRect();
              const fraction = Math.max(0, Math.min(1, (e.clientX - bounds.left) / bounds.width));
              const at = first + fraction * (last - first);
              const closest = timeline.reduce((best, item, index) => Math.abs(Date.parse(item.occurred_at) - at) < Math.abs(Date.parse(timeline[best].occurred_at) - at) ? index : best, 0);
              onCursorChange(closest);
            }}
            onPointerMove={(e) => {
              if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
              const bounds = e.currentTarget.getBoundingClientRect();
              const at = first + Math.max(0, Math.min(1, (e.clientX - bounds.left) / bounds.width)) * (last - first);
              const closest = timeline.reduce((best, item, index) => Math.abs(Date.parse(item.occurred_at) - at) < Math.abs(Date.parse(timeline[best].occurred_at) - at) ? index : best, 0);
              onCursorChange(closest);
            }}
            onPointerUp={(e) => { if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId); }} aria-label={t("graph.timeline.scrubber")} aria-valuetext={event ? markLabel(event, cursor) : t("graph.timeline.start")}
            type="range" min={0} max={timeline.length} value={Math.max(0, cursor + 1)} onChange={(e) => onCursorChange(Number(e.target.value) - 1)} />
          <div className="graph-history-dates"><span>{formatVersionTimestamp(timeline[0].occurred_at).split(" ")[0]}</span><span>{formatVersionTimestamp(timeline[timeline.length - 1].occurred_at).split(" ")[0]}</span></div>
        </div>
      </div>
      <div className="graph-timeline-event" aria-live={playing ? "off" : "polite"}>
        <strong className="graph-timeline-position">{Math.max(0, cursor + 1)} / {timeline.length}</strong>
        {event ? <><time dateTime={event.occurred_at}>{formatVersionTimestamp(event.occurred_at)}</time><span>{event.label}</span>
          {!event.visible_change ? <em>{t("graph.timeline.filteredEvent")}</em> : null}
          <button type="button" disabled={!event.visible_change} onClick={onShowEvent}>{t("graph.historyUi.showInGraph")}</button></> : t("graph.timeline.start")}
      </div>
    </>}
  </section>;
}

import { Activity, History, Info, Network, Ratio, Scan, ZoomIn, ZoomOut } from "lucide-react";
import { useId, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import type { GraphEdge, GraphHistoryMode, GraphRange, GraphTimelineEvent } from "../../../shared/api/graph";
import { GRAPH_CANVAS_RATIOS, type GraphCanvasRatio } from "./canvasRatio";
import { PreviewBadge } from "../../../shared/components/PreviewBadge";

export function GraphPreviewBadge() {
  const { t } = useTranslation("shell");
  return <PreviewBadge description={t("preview.description")} label={t("preview.badge")} />;
}

function navigateChoices(event: KeyboardEvent<HTMLButtonElement>) {
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  const choices = Array.from(event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('button[role="radio"]:not([data-unavailable="true"])') ?? []);
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
      <div className="graph-type-options" role="radiogroup" aria-label={t("graph.view.heading")} aria-busy={busy}>
        {modes.map((value) => {
          const Icon = icons[value];
          return <button key={value} type="button" role="radio" aria-checked={mode === value} aria-disabled={busy} tabIndex={mode === value ? 0 : -1}
            onClick={() => { if (!busy) onChange(value); }} onKeyDown={navigateChoices}>
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
  const reasonId = useId();
  return <div className="graph-scope-control">
    <span className="graph-local-toolbar-title">{t("graph.range.heading")}</span>
    <div className="graph-scope-options" role="radiogroup" aria-label={t("graph.range.heading")} aria-busy={busy} aria-describedby={historical ? reasonId : undefined}>{(["direct", "neighbors", "extended"] as const).map((value) => <button
      key={value} type="button" role="radio" aria-label={t(`graph.range.${value}.name`)} aria-checked={range === value} tabIndex={range === value ? 0 : -1}
      aria-disabled={busy || historical && value !== "direct"} data-unavailable={historical && value !== "direct" ? "true" : undefined} aria-describedby={historical && value !== "direct" ? reasonId : undefined}
      title={historical && value !== "direct" ? t("graph.range.historyOnly") : t(`graph.range.${value}.description`)}
      onClick={() => { if (!busy && (!historical || value === "direct")) onChange(value); }} onKeyDown={navigateChoices}>
      <RangeIcon range={value} /><span className="graph-scope-full">{t(`graph.range.${value}.name`)}</span>
      <span className="graph-scope-short">{t(`graph.range.${value}.short`)}</span>
    </button>)}</div>
    {historical ? <span className="visually-hidden" id={reasonId}>{t("graph.range.historyOnly")}</span> : null}
  </div>;
}

export function GraphCameraControls({ onZoomIn, onZoomOut, onFit, disabled, canvasRatio, heightDisabled, onHeightChange }: {
  canvasRatio: GraphCanvasRatio; heightDisabled: boolean; onHeightChange: (ratio: GraphCanvasRatio) => void;
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
    {!heightDisabled ? <details className="graph-height-picker">
      <summary title={t("graph.view.canvasHeight")}><Ratio size={16} aria-hidden="true" />
        {t("graph.view.height")} · {canvasRatio}
      </summary>
      <div className="graph-height-options" role="group" aria-label={t("graph.view.canvasHeight")}>
        <p>{t("graph.view.canvasAspectRatio")}</p>
        {GRAPH_CANVAS_RATIOS.map((ratio) => <button key={ratio} type="button"
          aria-pressed={canvasRatio === ratio} onClick={(event) => {
            onHeightChange(ratio);
            const picker = event.currentTarget.closest("details");
            picker?.removeAttribute("open");
            picker?.querySelector("summary")?.focus();
          }}>{ratio}</button>)}
      </div>
    </details> : null}
  </div>;
}

export type LocalTimelineEvent = GraphTimelineEvent & { visible_change: boolean } & import("./timelinePresentation").TimelinePresentation;
export function eventInvolves(event: GraphTimelineEvent, id: string | null, edges: readonly GraphEdge[] = []): boolean {
  return !!id && ([...(event.node_ids ?? []), ...(event.edge_ids ?? []),
    ...(event.node_snapshots ?? []).map((node) => node.id),
    ...(event.edge_snapshots ?? []).flatMap((edge) => [edge.id, edge.source, edge.target]),
    ...edges.filter((edge) => event.edge_ids?.includes(edge.id)).flatMap((edge) => [edge.source, edge.target])].includes(id));
}

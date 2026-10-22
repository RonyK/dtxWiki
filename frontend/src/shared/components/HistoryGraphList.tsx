import { Fragment, useMemo, type CSSProperties, type ReactNode } from "react";

import type { HistoryGraph, HistoryGraphRow } from "../lib/historyGraph";

// Paints a version history laid out by shared/lib/historyGraph.ts: the lane
// gutter of every row (dots, edges, bends) plus the row shell the surface's
// own card goes into. Context, Agent and Library all render their History tab
// through this, so the graph looks and behaves the same everywhere; routing
// rules live in historyGraph.ts (its HISTORY GRAPH CONTRACT), never here.

// Lane colouring follows the convention every git graph viewer uses: a lane
// owns a colour, and every mark on it -- dot, line, and the curves entering or
// leaving it -- wears that colour, so a branch is followed by eye instead of by
// tracing geometry. Lane 0 is the trunk and keeps the section accent
// (`--reader-accent`, so Context/Agent/Library each stay themselves); the rest
// cycle. The values live in CSS (09-context-audit-fixes.css) rather than here,
// so they stay themeable; this only picks the index.
// 8 base hues plus a darkened repeat of each, so the palette only wraps once a
// page holds 16 lanes at the SAME time -- which lane recycling makes far harder
// to reach than the old never-freed counter did.
const HISTORY_LANE_COLOR_COUNT = 16;

function historyLaneTint(lane: number): string {
  const index = ((lane % HISTORY_LANE_COLOR_COUNT) + HISTORY_LANE_COLOR_COUNT)
    % HISTORY_LANE_COLOR_COUNT;
  return `var(--history-lane-color-${index})`;
}

// The graph gutter is bounded so it cannot eat the cards beside it, so lane
// spacing has to give instead: a fixed 18px pitch put lane 5 past the old 96px
// cap, and `overflow: hidden` then deleted its dot outright. Pitch (and the dot
// with it) shrinks only as far as the lane count actually demands, so ordinary
// pages look exactly as before and only a genuinely busy one tightens up.
const HISTORY_LANE_PAD = 7;
const HISTORY_LANE_PITCH_MAX = 18;
const HISTORY_LANE_PITCH_MIN = 10;
const HISTORY_LANE_DOT_MAX = 12;
const HISTORY_LANE_GUTTER_MAX = 132;
const HISTORY_LANE_GUTTER_MIN = 32;

type HistoryLaneMetrics = { pitch: number; dot: number; gutter: number };

function historyLaneMetrics(laneCount: number): HistoryLaneMetrics {
  const span = Math.max(1, laneCount - 1);
  const budget = HISTORY_LANE_GUTTER_MAX - HISTORY_LANE_PAD * 2;
  const pitch = Math.max(
    HISTORY_LANE_PITCH_MIN,
    Math.min(HISTORY_LANE_PITCH_MAX, Math.floor(budget / span)),
  );
  const dot = Math.min(HISTORY_LANE_DOT_MAX, pitch - 1);
  const gutter = Math.max(
    HISTORY_LANE_GUTTER_MIN,
    Math.min(
      HISTORY_LANE_GUTTER_MAX,
      HISTORY_LANE_PAD * 2 + span * pitch + dot,
    ),
  );
  return { pitch, dot, gutter };
}

function historyLaneCentre(lane: number, metrics: HistoryLaneMetrics): number {
  return lane * metrics.pitch + HISTORY_LANE_PAD + metrics.dot / 2;
}

// Height of the zone in which an edge changes lane: at the bottom of the row
// directly above its target (`|/`), or right below a merge node (`|\`). A
// fixed height instead of the whole row keeps the bend next to the node it
// belongs to on a 150px+ card, so it cannot read as leaving a different row.
// Mirrored into CSS as --history-lane-bend so lines meet the curve exactly.
const HISTORY_LANE_BEND = 24;

function HistoryGraphBend({
  className,
  from,
  metrics,
  tintLane,
  to,
}: {
  className: string;
  from: number;
  metrics: HistoryLaneMetrics;
  tintLane: number;
  to: number;
}) {
  const fromX = historyLaneCentre(from, metrics);
  const toX = historyLaneCentre(to, metrics);
  const half = HISTORY_LANE_BEND / 2;
  return (
    <svg
      aria-hidden="true"
      className={className}
      preserveAspectRatio="none"
      style={{ "--history-lane-tint": historyLaneTint(tintLane) } as CSSProperties}
      viewBox={`0 0 ${metrics.gutter} ${HISTORY_LANE_BEND}`}
    >
      <path
        d={`M ${fromX} 0 C ${fromX} ${half}, ${toX} ${half}, ${toX} ${HISTORY_LANE_BEND}`}
      />
    </svg>
  );
}

function historySegmentStyle(lane: number, tintLane = lane): CSSProperties {
  return {
    "--history-segment-lane": lane,
    "--history-lane-tint": historyLaneTint(tintLane),
  } as CSSProperties;
}

// Paints one row's share of the graph from the segments historyGraph.ts laid
// out. Each edge keeps one colour -- the lane it travels in -- across every row
// it crosses, including the bend that lands it on its target.
function HistoryGraphSegments({
  metrics,
  row,
}: {
  metrics: HistoryLaneMetrics;
  row: HistoryGraphRow<unknown>;
}) {
  return (
    <>
      {row.segments.map((segment, index) => {
        const key = `${segment.kind}-${index}`;
        if (segment.kind === "in") {
          return (
            <span
              className="history-graph-incoming-line"
              key={key}
              style={historySegmentStyle(row.lane, segment.tintLane)}
            />
          );
        }
        if (segment.kind === "stub") {
          return (
            <span
              className="history-graph-stub"
              key={key}
              style={historySegmentStyle(row.lane)}
            />
          );
        }
        const bends = segment.to !== segment.lane;
        const departs = segment.kind === "out" && segment.lane !== row.lane;
        const lineClass = [
          segment.kind === "through"
            ? "history-graph-pass-line"
            : "history-graph-line",
          departs ? "history-graph-after-departure" : "",
          bends ? "history-graph-before-bend" : "",
          segment.unresolved ? "history-graph-unresolved" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <Fragment key={key}>
            {departs ? (
              <HistoryGraphBend
                className="history-graph-departure"
                from={row.lane}
                metrics={metrics}
                tintLane={segment.lane}
                to={segment.lane}
              />
            ) : null}
            <span
              className={lineClass}
              style={historySegmentStyle(segment.lane)}
            />
            {bends ? (
              <HistoryGraphBend
                className="history-graph-bend"
                from={segment.lane}
                metrics={metrics}
                tintLane={segment.lane}
                to={segment.to}
              />
            ) : null}
          </Fragment>
        );
      })}
    </>
  );
}

export type HistoryGraphRowView = {
  ariaCurrent?: "page" | "true";
  className?: string;
  // Extra `data-*` hooks a surface (or its tests) addresses rows by.
  dataAttributes?: Record<`data-${string}`, string>;
  tabIndex?: number;
  tone?: string;
};

export function HistoryGraphList<T>({
  accent,
  graph,
  renderCard,
  rowView,
}: {
  accent: "context" | "agent" | "library";
  graph: HistoryGraph<T>;
  renderCard: (row: HistoryGraphRow<T>) => ReactNode;
  rowView?: (row: HistoryGraphRow<T>) => HistoryGraphRowView;
}) {
  const metrics = useMemo(
    () => historyLaneMetrics(graph.laneCount),
    [graph.laneCount],
  );
  return (
    <ul
      className="history-timeline history-graph-list"
      data-accent={accent}
      style={
        {
          "--history-lane-pitch": `${metrics.pitch}px`,
          "--history-lane-dot": `${metrics.dot}px`,
          "--history-lane-gutter": `${metrics.gutter}px`,
          "--history-lane-bend": `${HISTORY_LANE_BEND}px`,
        } as CSSProperties
      }
    >
      {graph.rows.map((row) => {
        const view = rowView?.(row) ?? {};
        return (
          <li
            {...view.dataAttributes}
            aria-current={view.ariaCurrent}
            className={["history-card history-graph-row", view.className]
              .filter(Boolean)
              .join(" ")}
            data-history-lane={row.lane}
            data-parent-relation={row.relation}
            data-tone={view.tone}
            key={row.node.id}
            style={
              {
                "--history-lane": row.lane,
                "--history-lane-count": graph.laneCount,
                // The dot's colour. Every edge segment sets its own tint
                // from the lane that edge travels in.
                "--history-lane-tint": historyLaneTint(row.lane),
              } as CSSProperties
            }
            tabIndex={view.tabIndex}
          >
            <div aria-hidden="true" className="history-graph-visual">
              <HistoryGraphSegments metrics={metrics} row={row} />
              <span className="history-graph-node" />
            </div>
            <div className="history-graph-card-content">{renderCard(row)}</div>
          </li>
        );
      })}
    </ul>
  );
}

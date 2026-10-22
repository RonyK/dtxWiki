import {
  Fragment,
  useCallback,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  type CSSProperties,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";

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

// Keep the cards' gutter bounded. Fit both padding edges AND the last dot,
// shrinking only to a readable pitch. A busier graph uses a wider canvas and
// one shared horizontal scroll control, rather than clipping off its lanes
// or making arbitrarily many dots smaller than a pixel.
const HISTORY_LANE_PAD = 7;
const HISTORY_LANE_PITCH_MAX = 18;
const HISTORY_LANE_PITCH_MIN = 10;
const HISTORY_LANE_DOT_MAX = 12;
const HISTORY_LANE_GUTTER_MAX = 132;
const HISTORY_LANE_GUTTER_MIN = 32;

type HistoryLaneMetrics = {
  pitch: number;
  dot: number;
  gutter: number;
  canvasWidth: number;
};

function historyLaneMetrics(laneCount: number): HistoryLaneMetrics {
  const span = Math.max(1, laneCount - 1);
  const budget = HISTORY_LANE_GUTTER_MAX - HISTORY_LANE_PAD * 2;
  const fullDotPitch = (budget - HISTORY_LANE_DOT_MAX) / span;
  // Above 13px the dot is capped at 12px; below that it is pitch - 1.
  // Solve for the available width in each range, without integer rounding.
  const fittingPitch =
    fullDotPitch >= HISTORY_LANE_DOT_MAX + 1
      ? fullDotPitch
      : (budget + 1) / (span + 1);
  const pitch = Math.max(
    HISTORY_LANE_PITCH_MIN,
    Math.min(HISTORY_LANE_PITCH_MAX, fittingPitch),
  );
  const dot = Math.min(HISTORY_LANE_DOT_MAX, pitch - 1);
  const canvasWidth = HISTORY_LANE_PAD * 2 + span * pitch + dot;
  const gutter = Math.max(
    HISTORY_LANE_GUTTER_MIN,
    Math.min(HISTORY_LANE_GUTTER_MAX, canvasWidth),
  );
  return { pitch, dot, gutter, canvasWidth };
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
      viewBox={`0 0 ${metrics.canvasWidth} ${HISTORY_LANE_BEND}`}
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
  const { t } = useTranslation("shell");
  const scrollLabelId = useId();
  const scrollHelpId = useId();
  const listRef = useRef<HTMLUListElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const newestVersionId = graph.rows[0]?.node.id;
  const previousNewestId = useRef(newestVersionId);
  const metrics = useMemo(
    () => historyLaneMetrics(graph.laneCount),
    [graph.laneCount],
  );
  const scrollable = metrics.canvasWidth > metrics.gutter;
  const syncScroll = useCallback(
    (requestedOffset: number) => {
      const offset = Math.max(
        0,
        Math.min(requestedOffset, metrics.canvasWidth - metrics.gutter),
      );
      if (scrollRef.current && scrollRef.current.scrollLeft !== offset) {
        scrollRef.current.scrollLeft = offset;
      }
      // A single offset moves all the rows, without re-rendering their cards
      // for each native scroll event.
      listRef.current?.style.setProperty("--history-lane-offset", `${offset}px`);
    },
    [metrics.canvasWidth, metrics.gutter],
  );
  useLayoutEffect(() => {
    // Loading a different history or fewer lanes must not leave the canvas
    // shifted beyond its new end. Removing the scroll control resets to zero.
    syncScroll(
      previousNewestId.current === newestVersionId
        ? scrollRef.current?.scrollLeft ?? 0
        : 0,
    );
    previousNewestId.current = newestVersionId;
  }, [newestVersionId, syncScroll]);
  return (
    <div
      className="history-graph"
      style={
        {
          "--history-lane-pitch": `${metrics.pitch}px`,
          "--history-lane-dot": `${metrics.dot}px`,
          "--history-lane-gutter": `${metrics.gutter}px`,
          "--history-lane-canvas": `${metrics.canvasWidth}px`,
          "--history-lane-bend": `${HISTORY_LANE_BEND}px`,
        } as CSSProperties
      }
    >
      {scrollable ? (
        <div className="history-graph-scroll-controls">
          <span id={scrollLabelId}>
            {t("history.scrollGraph", { defaultValue: "Scroll history graph" })}
          </span>
          <div
            aria-describedby={scrollHelpId}
            aria-labelledby={scrollLabelId}
            className="history-graph-scroll"
            onScroll={(event) => syncScroll(event.currentTarget.scrollLeft)}
            ref={scrollRef}
            role="region"
            tabIndex={0 /* eslint-disable-line jsx-a11y/no-noninteractive-tabindex -- Keyboard access to native horizontal scrolling. */}
          >
            <div aria-hidden="true" className="history-graph-scroll-track">
              <span>↔</span>
            </div>
          </div>
          <span className="history-graph-scroll-help" id={scrollHelpId}>
            {t("history.scrollGraphHelp", {
              defaultValue: "Swipe or use the left and right arrow keys.",
            })}
          </span>
        </div>
      ) : null}
      <ul
        className="history-timeline history-graph-list"
        data-accent={accent}
        ref={listRef}
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
                <div className="history-graph-lanes">
                  <HistoryGraphSegments metrics={metrics} row={row} />
                  <span className="history-graph-node" />
                </div>
              </div>
              <div className="history-graph-card-content">{renderCard(row)}</div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

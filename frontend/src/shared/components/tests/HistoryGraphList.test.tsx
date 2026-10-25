import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { HistoryGraph } from "../../lib/historyGraph";
import { HistoryGraphList } from "../HistoryGraphList";

// Two sentinel rows exercise the renderer's first and last lane without
// creating 1,000 cards. Graph topology has its own existing contract tests.
function graphWithLanes(laneCount: number): HistoryGraph<number> {
  const lanes = laneCount === 0 ? [] : laneCount === 1 ? [0] : [0, laneCount - 1];
  return {
    laneCount,
    rows: lanes.map((lane, index) => ({
      node: {
        entry: lane,
        id: `sentinel-${index}`,
        versionNo: index + 1,
        parentId: null,
        parentRelation: "root",
      },
      entry: lane,
      lane,
      parent: null,
      relation: "root",
      segments: [],
    })),
  };
}

describe("HistoryGraphList", () => {
  it("keeps boundary lanes reachable at readable sizes with one shared, clamped scroll offset", () => {
    const renderGraph = (laneCount: number) => (
      <HistoryGraphList
        accent="context"
        graph={graphWithLanes(laneCount)}
        renderCard={(row) => <span>Card {row.node.id}</span>}
      />
    );
    const view = render(renderGraph(0));
    const root = () => view.container.querySelector<HTMLElement>(".history-graph")!;
    const list = () => view.container.querySelector<HTMLElement>(".history-graph-list")!;
    const size = (name: string) => Number.parseFloat(root().style.getPropertyValue(name));
    const control = () => screen.queryByRole("region", { name: "Scroll history graph" });
    expect(control()).toBeNull();
    expect(list().children).toHaveLength(0);

    for (const laneCount of [1, 2, 6, 7, 8, 9, 10, 11, 12, 13, 14, 80, 1000]) {
      view.rerender(renderGraph(laneCount));
      const pitch = size("--history-lane-pitch");
      const dot = size("--history-lane-dot");
      const gutter = size("--history-lane-gutter");
      const canvas = size("--history-lane-canvas");
      // Both ends reserve 7px for the dot and its shadow. These inequalities
      // catch the old 10/13-lane partial clipping and 14+ fully missing dots.
      const lastDotRight = (laneCount - 1) * pitch + 7 + dot;
      expect(lastDotRight, `${laneCount} lanes fit the canvas`).toBeLessThanOrEqual(canvas - 7 + 1e-7);
      expect(pitch).toBeGreaterThanOrEqual(10);
      expect(dot).toBeGreaterThanOrEqual(9);
      expect(gutter).toBeLessThanOrEqual(132);
      if (laneCount <= 6) {
        expect(pitch).toBe(18);
        expect(dot).toBe(12);
      }

      if (laneCount <= 11) {
        expect(control()).toBeNull();
        expect(lastDotRight).toBeLessThanOrEqual(gutter - 7 + 1e-7);
      } else {
        const scroller = control()!;
        expect(scroller).toHaveAttribute("tabindex", "0");
        expect(scroller).toHaveAccessibleDescription("Swipe or use the left and right arrow keys.");
        expect(screen.getAllByRole("region", { name: "Scroll history graph" })).toHaveLength(1);
        fireEvent.scroll(scroller, { target: { scrollLeft: Number.MAX_SAFE_INTEGER } });
        const offset = Number.parseFloat(list().style.getPropertyValue("--history-lane-offset"));
        expect(offset).toBeCloseTo(canvas - gutter);
        expect(scroller.scrollLeft).toBeCloseTo(offset);
        expect(lastDotRight - offset).toBeLessThanOrEqual(gutter - 7 + 1e-7);
        // Every row inherits the same offset; no card is part of the moving
        // lane canvas. Real pixel containment is checked in browser evidence.
        for (const row of list().children) {
          const lanes = row.querySelector(".history-graph-lanes")!;
          expect(lanes.closest(".history-graph-list")).toBe(list());
          expect(lanes.querySelector(".history-graph-card-content")).toBeNull();
        }
      }
    }

    // Clamp an existing busy graph, then remove scrolling entirely. This
    // protects against a stale offset hiding all the lanes after a rerender.
    view.rerender(renderGraph(14));
    expect(control()!.scrollLeft).toBeCloseTo(size("--history-lane-canvas") - size("--history-lane-gutter"));
    view.rerender(renderGraph(6));
    expect(control()).toBeNull();
    expect(list().style.getPropertyValue("--history-lane-offset")).toBe("0px");
    view.rerender(renderGraph(0));
    expect(list().children).toHaveLength(0);
    expect(control()).toBeNull();
  });
});

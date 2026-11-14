import { describe, expect, it } from "vitest";
import { nearestTimelineIndex, timelineLayout } from "../timelineLayout";

// Records are built from local times, so the expected days hold in any time zone.
const local = (year: number, month: number, day: number, hour: number, minute = 0) => new Date(year, month - 1, day, hour, minute);

describe("timeline layout", () => {
  it("gives every record its own column under its local day and spreads the days across the axis", () => {
    const busyDay = Array.from({ length: 7 }, (_, index) => ({ occurred_at: local(2026, 10, 20, 9 + index).toISOString() }));
    const events = [
      { occurred_at: local(2026, 10, 6, 9).toISOString() },
      { occurred_at: local(2026, 10, 6, 23, 59).toISOString() },
      // SQLite returns UTC without an offset; one minute past local midnight is the next day.
      { occurred_at: local(2026, 10, 7, 0, 1).toISOString().slice(0, -1) },
      ...busyDay,
      { occurred_at: local(2027, 1, 2, 10).toISOString() },
    ];
    const layout = timelineLayout(events, { width: 1000, unit: 32, gap: 12, minWidth: { "2026-10-07": 50 } });

    expect(layout.days.map(({ key, indices, newYear }) => ({ key, indices, newYear }))).toEqual([
      { key: "2026-10-06", indices: [0, 1], newYear: false },
      { key: "2026-10-07", indices: [2], newYear: false },
      { key: "2026-10-20", indices: [3, 4, 5, 6, 7, 8, 9], newYear: false },
      { key: "2027-01-02", indices: [10], newYear: true },
    ]);
    // Seven records on one day still take seven columns; a short day is as wide as its label.
    expect(layout.days.map((day) => day.width)).toEqual([64, 50, 224, 32]);
    // Columns follow timeline order across both lanes, so the playhead only moves right.
    expect(layout.x.every((x, index) => index === 0 || x > layout.x[index - 1])).toBe(true);
    expect(layout.x[3]).toBe(layout.days[2].left + 16);
    // The days fit: the first starts at the left edge, the last ends at the right, equal gaps between.
    expect(layout.scroll).toBe(false);
    expect(layout.days[0].left).toBe(0);
    expect(layout.days[3].left + layout.days[3].width).toBeCloseTo(1000);
    const gaps = layout.days.slice(1).map((day, index) => day.left - layout.days[index].left - layout.days[index].width);
    gaps.forEach((gap) => expect(gap).toBeCloseTo(gaps[0]));
  });

  it("scrolls a day wider than the axis from the left edge and seeks the nearest record or the start", () => {
    const events = Array.from({ length: 5 }, (_, index) => ({ occurred_at: local(2026, 11, 13, 9 + index).toISOString() }));
    const layout = timelineLayout(events, { width: 150, unit: 40, gap: 12 });

    expect(layout.scroll).toBe(true);
    expect(layout.width).toBe(200);
    expect(layout.x).toEqual([20, 60, 100, 140, 180]);
    // Left of half the way to the first record is the start state; ties keep the earlier position.
    expect([5, 10, 40, 41, 999].map((at) => nearestTimelineIndex(layout.x, at))).toEqual([-1, -1, 0, 1, 4]);
  });
});

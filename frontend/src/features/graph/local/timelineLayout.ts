import { formatVersionTimestamp } from "../../../shared/lib/datetime";

/** One calendar day that has records; days without records take no space. */
export type TimelineDay = {
  /** The local date the record times display with, `YYYY-MM-DD`. */
  key: string;
  /** Timeline indices of the day's records, in timeline order. */
  indices: number[];
  /** The previous day falls in another year, so its label names the year. */
  newYear: boolean;
  left: number;
  width: number;
};

export type TimelineLayout = {
  days: TimelineDay[];
  /** The column centre of every record, by timeline index. */
  x: number[];
  /** The days are wider than the axis, which then scrolls sideways. */
  scroll: boolean;
  width: number;
};

/**
 * Lays the records out by day: each record gets a column one mark wide, in
 * timeline order across both lanes, so the playhead only moves rightwards.
 * Spare width goes between the days; when the days do not fit, they keep the
 * minimum gap and the axis scrolls.
 */
export function timelineLayout(
  events: readonly { occurred_at: string }[],
  { width, unit, gap, minWidth = {} }: { width: number; unit: number; gap: number; minWidth?: Readonly<Record<string, number>> },
): TimelineLayout {
  const days: TimelineDay[] = [];
  events.forEach((event, index) => {
    const key = formatVersionTimestamp(event.occurred_at).split(" ")[0];
    const previous = days[days.length - 1];
    if (previous?.key === key) previous.indices.push(index);
    else days.push({ key, indices: [index], newYear: !!previous && previous.key.slice(0, 4) !== key.slice(0, 4), left: 0, width: 0 });
  });
  for (const day of days) day.width = Math.max(day.indices.length * unit, minWidth[day.key] ?? 0);
  const total = days.reduce((sum, day) => sum + day.width, 0);
  const natural = total + Math.max(0, days.length - 1) * gap;
  const scroll = natural > width;
  const spacing = !scroll && days.length > 1 ? (width - total) / (days.length - 1) : gap;
  let left = days.length === 1 && !scroll ? (width - days[0].width) / 2 : 0;
  const x = new Array<number>(events.length);
  for (const day of days) {
    day.left = left;
    const start = left + (day.width - day.indices.length * unit) / 2;
    day.indices.forEach((index, column) => { x[index] = start + (column + 0.5) * unit; });
    left += day.width + spacing;
  }
  return { days, x, scroll, width: scroll ? natural : width };
}

/** The record nearest to `at`, or -1 for the start state at the left edge. */
export function nearestTimelineIndex(x: readonly number[], at: number): number {
  let best = -1;
  let distance = Math.abs(at);
  x.forEach((position, index) => {
    if (Math.abs(position - at) < distance) {
      best = index;
      distance = Math.abs(position - at);
    }
  });
  return best;
}

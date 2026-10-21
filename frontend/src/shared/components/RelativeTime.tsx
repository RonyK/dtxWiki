import { useEffect, useState } from "react";

import { formatRelativeTimestamp, formatVersionTimestamp } from "../lib/datetime";

// One shared interval drives every live relative label, so a list of N
// timestamps costs a single timer rather than N. 30s keeps "just now" / "N
// minutes ago" honest without re-rendering the tree too aggressively. The timer
// is created lazily on the first subscriber and torn down when the last one
// leaves, so an idle app holds no interval.
const TICK_MS = 30_000;
const subscribers = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(notify: () => void): () => void {
  subscribers.add(notify);
  if (timer === null) {
    timer = setInterval(() => {
      for (const fn of subscribers) {
        fn();
      }
    }, TICK_MS);
  }
  return () => {
    subscribers.delete(notify);
    if (subscribers.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

/** Re-render the caller roughly every TICK_MS so relative labels stay current. */
export function useNowTick(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => subscribe(() => setNow(Date.now())), []);
  return now;
}

/**
 * A <time> rendering a localized relative label ("3 minutes ago") that stays
 * live via the shared tick, with the absolute timestamp in the title tooltip —
 * the canonical recency convention shared by Ask, Activity, and Chats. Invalid
 * or missing values render the neutral "—" marker (via formatRelativeTimestamp).
 */
export function RelativeTime({
  value,
  locale,
  className,
}: {
  value: string | null | undefined;
  locale: string;
  className?: string;
}) {
  useNowTick();
  return (
    <time
      className={className}
      dateTime={value ?? undefined}
      title={value ? formatVersionTimestamp(value, "long") : undefined}
    >
      {formatRelativeTimestamp(value, locale)}
    </time>
  );
}

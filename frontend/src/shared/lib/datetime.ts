import { translate } from "../i18n";

const DATE_TIME_PARTS = new Intl.DateTimeFormat("en-CA", {
  day: "2-digit",
  hour: "2-digit",
  hour12: false,
  minute: "2-digit",
  month: "2-digit",
  year: "numeric",
});

const LONG_DATE_TIME_PARTS = new Intl.DateTimeFormat("en-CA", {
  day: "2-digit",
  hour: "2-digit",
  hour12: false,
  minute: "2-digit",
  month: "2-digit",
  second: "2-digit",
  timeZoneName: "short",
  year: "numeric",
});

// I18N-11: these three formatters previously pinned locale to `undefined`,
// which resolves to the *browser's* locale rather than the app's active
// i18next language. A ko UI on an en-US browser then mixed "Feb 08, 2026"
// into otherwise-Korean text. Cache one formatter per locale instead of a
// single global one, mirroring the relativeFormatters pattern below.
function cachedDateTimeFormatter(
  cache: Map<string, Intl.DateTimeFormat>,
  locale: string,
  options: Intl.DateTimeFormatOptions,
) {
  let formatter = cache.get(locale);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(locale, options);
    cache.set(locale, formatter);
  }
  return formatter;
}

const displayDateTimeFormatters = new Map<string, Intl.DateTimeFormat>();
const displayDateFormatters = new Map<string, Intl.DateTimeFormat>();
const bucketDateFormatters = new Map<string, Intl.DateTimeFormat>();

const displayDateTimeFormatterFor = (locale: string) =>
  cachedDateTimeFormatter(displayDateTimeFormatters, locale, {
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    month: "short",
    timeZoneName: "short",
    year: "numeric",
  });

const displayDateFormatterFor = (locale: string) =>
  cachedDateTimeFormatter(displayDateFormatters, locale, {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });

const bucketDateFormatterFor = (locale: string) =>
  cachedDateTimeFormatter(bucketDateFormatters, locale, {
    day: "numeric",
    month: "short",
  });

const relativeFormatters = new Map<string, Intl.RelativeTimeFormat>();

export function formatVersionTimestamp(
  value: string | null | undefined,
  density: "compact" | "long" = "compact",
) {
  const date = parseDate(value);
  if (!date) {
    return "—";
  }
  const parts = partsFor(date, density === "long" ? LONG_DATE_TIME_PARTS : DATE_TIME_PARTS);
  const base = `${parts.year}-${parts.month}-${parts.day} ${normalizeHour(parts.hour)}:${parts.minute}`;
  if (density === "compact") {
    return base;
  }
  const seconds = parts.second ? `:${parts.second}` : "";
  const zone = parts.timeZoneName ? ` ${parts.timeZoneName}` : "";
  return `${base}${seconds}${zone}`;
}

export function formatDisplayDateTime(
  value: string | null | undefined,
  locale: string,
  fallback = translate("shell:time.unknownTime", "Unknown time"),
) {
  const date = parseDate(value);
  return date ? displayDateTimeFormatterFor(locale).format(date) : fallback;
}

export function formatDisplayDate(
  value: string | null | undefined,
  locale: string,
  fallback = translate("shell:time.unknownDate", "Unknown date"),
) {
  const date = parseDate(value);
  return date ? displayDateFormatterFor(locale).format(date) : fallback;
}

// Relative row text (e.g. "3 minutes ago"). Within 7 days the wording is
// localized via Intl.RelativeTimeFormat; at >=7 days, or when the runtime lacks
// Intl.RelativeTimeFormat, it falls back to the canonical local 24h compact
// shape so callers never see a second absolute convention. Invalid input -> "—".
export function formatRelativeTimestamp(
  value: string | null | undefined,
  locale: string,
  options?: { now?: number; alwaysRelative?: boolean },
): string {
  const date = parseDate(value);
  if (!date) {
    return "—";
  }
  const diffMs = date.getTime() - (options?.now ?? Date.now());
  const absDays = Math.abs(diffMs) / 86_400_000;
  if ((absDays < 7 || options?.alwaysRelative) && typeof Intl.RelativeTimeFormat === "function") {
    const [amount, unit] = bestRelativeUnit(diffMs);
    return relativeFormatterFor(locale).format(amount, unit);
  }
  return formatVersionTimestamp(value);
}

export type TimeBucketKey =
  | "today"
  | "yesterday"
  | "last_7_days"
  | "last_30_days"
  | "older";

// Calendar-day bucket for a timestamp, relative to the viewer's local day.
// Lifted from the Chats list so Ask/Chats/Activity share one group model.
export function timeBucketFor(value: string): TimeBucketKey {
  const timestamp = new Date(normalizeTimestamp(value));
  if (Number.isNaN(timestamp.getTime())) {
    return "older";
  }
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfDate = new Date(
    timestamp.getFullYear(),
    timestamp.getMonth(),
    timestamp.getDate(),
  );
  const diffDays = Math.floor(
    (startOfToday.getTime() - startOfDate.getTime()) / 86_400_000,
  );
  if (diffDays <= 0) {
    return "today";
  }
  if (diffDays === 1) {
    return "yesterday";
  }
  if (diffDays < 7) {
    return "last_7_days";
  }
  if (diffDays < 30) {
    return "last_30_days";
  }
  return "older";
}

// Localized group header for a bucket key. Reads the chats-namespace buckets.*
// keys; last_7_days/last_30_days interpolate a rolling {{range}} label.
export function formatGroupHeader(
  key: TimeBucketKey,
  t: (key: string, options?: Record<string, unknown>) => string,
  locale: string,
): string {
  if (key === "today") {
    return t("buckets.today");
  }
  if (key === "yesterday") {
    return t("buckets.yesterday");
  }
  if (key === "last_7_days") {
    return t("buckets.last7", { range: rollingDateRangeLabel(6, locale) });
  }
  if (key === "last_30_days") {
    return t("buckets.last30", { range: rollingDateRangeLabel(29, locale) });
  }
  return t("buckets.older");
}

// Duration label for a call/session span. null -> localized "unknown".
// Unit spacing is unspaced ("5s"/"3m"/"2h") to match the compact metadata rows.
export function formatDurationMs(
  value: number | null,
  t: (key: string, options?: Record<string, unknown>) => string,
): string {
  if (value === null) {
    return t("units.unknown");
  }
  if (value < 60_000) {
    return `${Math.max(1, Math.round(value / 1000))}s`;
  }
  if (value < 3_600_000) {
    return `${Math.round(value / 60_000)}m`;
  }
  return `${Math.round(value / 3_600_000)}h`;
}

// Latency label. Always numeric (callers guard null upstream), so no i18n
// string is needed; unit spacing is unified to a single leading space ("123 ms"
// / "1.2 s") so it no longer disagrees with the duration convention.
export function formatLatencyMs(value: number): string {
  if (value < 1000) {
    return `${value} ms`;
  }
  return `${(value / 1000).toFixed(1)} s`;
}

function relativeFormatterFor(locale: string): Intl.RelativeTimeFormat {
  let formatter = relativeFormatters.get(locale);
  if (!formatter) {
    formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
    relativeFormatters.set(locale, formatter);
  }
  return formatter;
}

function bestRelativeUnit(
  diffMs: number,
): [number, Intl.RelativeTimeFormatUnit] {
  const diffSeconds = Math.round(diffMs / 1000);
  const absSeconds = Math.abs(diffSeconds);
  if (absSeconds < 60) {
    return [diffSeconds, "second"];
  }
  const diffMinutes = Math.round(diffMs / 60_000);
  if (Math.abs(diffMinutes) < 60) {
    return [diffMinutes, "minute"];
  }
  const diffHours = Math.round(diffMs / 3_600_000);
  if (Math.abs(diffHours) < 24) {
    return [diffHours, "hour"];
  }
  return [Math.round(diffMs / 86_400_000), "day"];
}

function rollingDateRangeLabel(daysBack: number, locale: string) {
  const end = new Date();
  const start = new Date(end);
  start.setDate(end.getDate() - daysBack);
  const formatter = bucketDateFormatterFor(locale);
  return `${formatter.format(start)}-${formatter.format(end)}`;
}

// The backend stores UTC instants but, on the SQLite tier, returns them without
// a timezone offset (SQLite has no tz type), e.g. "2026-01-01T12:00:00.119886".
// JS would parse an offset-less ISO timestamp as *local* time, shifting every
// displayed time by the viewer's UTC offset. Treat such values as UTC by
// appending "Z"; offset-aware values (Postgres "+00:00", or already-"Z") and
// non-ISO strings pass through unchanged.
export function normalizeTimestamp(value: string): string {
  const isoDateTime = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(value);
  const hasOffset = /([zZ]|[+-]\d{2}:?\d{2})$/.test(value);
  return isoDateTime && !hasOffset ? `${value.replace(" ", "T")}Z` : value;
}

function parseDate(value: string | null | undefined) {
  if (!value) {
    return null;
  }
  const date = new Date(normalizeTimestamp(value));
  return Number.isNaN(date.getTime()) ? null : date;
}

function partsFor(date: Date, formatter: Intl.DateTimeFormat) {
  const parts = formatter.formatToParts(date);
  return Object.fromEntries(
    parts
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  ) as Partial<Record<Intl.DateTimeFormatPartTypes, string>>;
}

function normalizeHour(hour: string | undefined) {
  if (!hour) {
    return "00";
  }
  return hour === "24" ? "00" : hour.padStart(2, "0");
}

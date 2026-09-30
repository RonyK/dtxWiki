import { afterEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../i18n";
import {
  formatDisplayDate,
  formatDisplayDateTime,
  formatDurationMs,
  formatGroupHeader,
  formatLatencyMs,
  formatRelativeTimestamp,
  formatVersionTimestamp,
  normalizeTimestamp,
  timeBucketFor,
  type TimeBucketKey,
} from "../datetime";
import * as dateFormatting from "../dateFormatting";

const NOW = new Date("2026-06-13T12:00:00Z");

it("keeps the dateFormatting compatibility exports wired to datetime", () => {
  expect(dateFormatting.formatDisplayDate).toBe(formatDisplayDate);
  expect(dateFormatting.formatDisplayDateTime).toBe(formatDisplayDateTime);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("formatVersionTimestamp", () => {
  it("renders compact and long version timestamps with a stable date-time shape", () => {
    expect(formatVersionTimestamp("2026-05-23T05:40:00Z")).toMatch(
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/,
    );
    expect(formatVersionTimestamp("2026-05-23T05:40:00Z", "long")).toMatch(
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/,
    );
  });

  it("uses a neutral fallback for invalid or missing timestamps", () => {
    expect(formatVersionTimestamp("not-a-date")).toBe("—");
    expect(formatVersionTimestamp(null)).toBe("—");
    expect(formatVersionTimestamp(undefined, "long")).toBe("—");
  });
});

describe("formatDisplayDateTime / formatDisplayDate (re-exported via dateFormatting)", () => {
  afterEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("formats valid timestamps without exposing raw invalid-date text", () => {
    expect(formatDisplayDateTime("2026-05-23T05:40:00Z", "en")).not.toMatch(
      /Invalid Date|Unknown time/,
    );
    expect(formatDisplayDate("2026-05-23T05:40:00Z", "en")).not.toMatch(
      /Invalid Date|Unknown date/,
    );
  });

  it("uses stable fallbacks for missing or invalid timestamps", () => {
    expect(formatDisplayDateTime("not-a-date", "en")).toBe("Unknown time");
    expect(formatDisplayDateTime(null, "en")).toBe("Unknown time");
    expect(formatDisplayDate("not-a-date", "en")).toBe("Unknown date");
    expect(formatDisplayDate(undefined, "en")).toBe("Unknown date");
  });

  // I18N-11: DISPLAY_DATE_TIME_FORMATTER/DISPLAY_DATE_FORMATTER previously
  // pinned Intl locale to `undefined` (the *browser's* locale), so a ko UI
  // on an en-US browser mixed "Feb 08, 2026" into Korean text. Asserting the
  // two locales render differently is the oracle that would fail if that
  // regressed — a single-locale test would pass either way.
  it("renders a different date shape for ko vs en", () => {
    const en = formatDisplayDateTime("2026-02-08T05:40:00Z", "en");
    const ko = formatDisplayDateTime("2026-02-08T05:40:00Z", "ko");
    expect(en).not.toBe(ko);

    const enDate = formatDisplayDate("2026-02-08T05:40:00Z", "en");
    const koDate = formatDisplayDate("2026-02-08T05:40:00Z", "ko");
    expect(enDate).not.toBe(koDate);
  });

  // The default fallback resolves via the shared `translate()` helper, which
  // reads the *global* i18next language, not the `locale` argument. Import
  // without `translate` being wired up throws a ReferenceError immediately,
  // so this also guards against that regression.
  it("falls back to a localized (not English) missing-time label when the UI language is ko", async () => {
    await i18n.changeLanguage("ko");
    expect(formatDisplayDateTime(null, "ko")).not.toBe("Unknown time");
    expect(formatDisplayDateTime(null, "ko")).toBe("시간 정보 없음");
    expect(formatDisplayDate(null, "ko")).not.toBe("Unknown date");
    expect(formatDisplayDate(null, "ko")).toBe("날짜 정보 없음");
  });
});

describe("formatRelativeTimestamp", () => {
  it("returns a relative string for instants within 7 days", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const tenMinutesAgo = new Date(NOW.getTime() - 10 * 60_000).toISOString();
    const result = formatRelativeTimestamp(tenMinutesAgo, "en");
    expect(result).toBe("10 minutes ago");
  });

  it("uses a seconds bucket under a minute", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const fiveSecondsAgo = new Date(NOW.getTime() - 5_000).toISOString();
    expect(formatRelativeTimestamp(fiveSecondsAgo, "en")).toBe(
      "5 seconds ago",
    );
  });

  it("falls back to the local 24h compact shape at >= 7 days", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const eightDaysAgo = new Date(
      NOW.getTime() - 8 * 86_400_000,
    ).toISOString();
    const result = formatRelativeTimestamp(eightDaysAgo, "en");
    expect(result).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    expect(result).toBe(formatVersionTimestamp(eightDaysAgo));
  });

  it("falls back to the absolute shape when Intl.RelativeTimeFormat is unavailable", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const intl = Intl as unknown as {
      RelativeTimeFormat?: typeof Intl.RelativeTimeFormat;
    };
    const original = intl.RelativeTimeFormat;
    try {
      delete intl.RelativeTimeFormat;
      const oneMinuteAgo = new Date(NOW.getTime() - 60_000).toISOString();
      expect(formatRelativeTimestamp(oneMinuteAgo, "en")).toBe(
        formatVersionTimestamp(oneMinuteAgo),
      );
    } finally {
      intl.RelativeTimeFormat = original;
    }
  });

  it("returns the neutral marker for invalid or missing input", () => {
    expect(formatRelativeTimestamp("not-a-date", "en")).toBe("—");
    expect(formatRelativeTimestamp(null, "en")).toBe("—");
    expect(formatRelativeTimestamp(undefined, "en")).toBe("—");
  });
});

describe("normalizeTimestamp", () => {
  it("treats an offset-less SQLite timestamp as UTC instead of local time", () => {
    // The backend's SQLite tier returns UTC instants without an offset. Parsed
    // as-is, JS would read this as local time and shift the displayed value by
    // the viewer's UTC offset. normalizeTimestamp must pin it to UTC.
    expect(normalizeTimestamp("2026-06-13T03:00:00.119886")).toBe(
      "2026-06-13T03:00:00.119886Z",
    );
    expect(new Date(normalizeTimestamp("2026-06-13T03:00:00")).toISOString()).toBe(
      "2026-06-13T03:00:00.000Z",
    );
  });

  it("normalizes a space-separated naive timestamp to ISO UTC", () => {
    expect(normalizeTimestamp("2026-06-13 03:00:00")).toBe(
      "2026-06-13T03:00:00Z",
    );
  });

  it("passes offset-aware and Z-suffixed values through unchanged", () => {
    expect(normalizeTimestamp("2026-06-13T03:00:00Z")).toBe(
      "2026-06-13T03:00:00Z",
    );
    expect(normalizeTimestamp("2026-06-13T03:00:00+00:00")).toBe(
      "2026-06-13T03:00:00+00:00",
    );
    expect(normalizeTimestamp("2026-06-13T03:00:00-05:00")).toBe(
      "2026-06-13T03:00:00-05:00",
    );
  });

  it("leaves non-ISO strings untouched so they still fail to parse", () => {
    expect(normalizeTimestamp("not-a-date")).toBe("not-a-date");
  });
});

describe("timeBucketFor", () => {
  it("classifies instants at the local-day boundaries", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const buckets: Array<[string, TimeBucketKey]> = [
      [new Date(NOW.getTime() - 60_000).toISOString(), "today"],
      [new Date(NOW.getTime() - 1 * 86_400_000).toISOString(), "yesterday"],
      [new Date(NOW.getTime() - 3 * 86_400_000).toISOString(), "last_7_days"],
      [new Date(NOW.getTime() - 15 * 86_400_000).toISOString(), "last_30_days"],
      [new Date(NOW.getTime() - 45 * 86_400_000).toISOString(), "older"],
    ];
    for (const [value, expected] of buckets) {
      expect(timeBucketFor(value)).toBe(expected);
    }
  });

  it("treats invalid input as older", () => {
    expect(timeBucketFor("not-a-date")).toBe("older");
  });
});

describe("formatGroupHeader", () => {
  const t = (key: string, options?: Record<string, unknown>) =>
    options && "range" in options ? `${key}:${String(options.range)}` : key;

  it("maps each bucket key to its buckets.* translation key", () => {
    expect(formatGroupHeader("today", t, "en")).toBe("buckets.today");
    expect(formatGroupHeader("yesterday", t, "en")).toBe("buckets.yesterday");
    expect(formatGroupHeader("older", t, "en")).toBe("buckets.older");
  });

  it("interpolates a rolling range for the last7/last30 buckets", () => {
    expect(formatGroupHeader("last_7_days", t, "en")).toMatch(/^buckets\.last7:.+-.+$/);
    expect(formatGroupHeader("last_30_days", t, "en")).toMatch(
      /^buckets\.last30:.+-.+$/,
    );
  });

  // I18N-11: rollingDateRangeLabel used to format the {{range}} span with a
  // single global (browser-locale) formatter. If BUCKET_DATE_FORMATTER were
  // reverted to a locale-less global, this is the only case that would catch
  // it — the two calls above pass regardless of locale.
  it("formats the rolling {{range}} span in the requested locale", () => {
    const en = formatGroupHeader("last_7_days", t, "en");
    const ko = formatGroupHeader("last_7_days", t, "ko");
    expect(en).not.toBe(ko);
  });
});

describe("formatDurationMs", () => {
  const t = (key: string) => (key === "units.unknown" ? "unknown" : key);

  it("returns the localized unknown label for null", () => {
    expect(formatDurationMs(null, t)).toBe("unknown");
  });

  it("formats sub-minute, minute and hour spans with unspaced units", () => {
    expect(formatDurationMs(500, t)).toBe("1s");
    expect(formatDurationMs(45_000, t)).toBe("45s");
    expect(formatDurationMs(120_000, t)).toBe("2m");
    expect(formatDurationMs(7_200_000, t)).toBe("2h");
  });
});

describe("formatLatencyMs", () => {
  it("uses a single leading space for both ms and s", () => {
    expect(formatLatencyMs(123)).toBe("123 ms");
    expect(formatLatencyMs(1500)).toBe("1.5 s");
  });
});

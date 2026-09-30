type CountChipTone = "neutral" | "danger" | "calls" | "writes";

type CountChipProps = {
  value: number | string;
  /**
   * Already-pluralized noun, e.g. "call" / "calls". Omit when the adjacent
   * panel title already names the unit.
   */
  label?: string;
  /**
   * Pre-composed count text, for copy that fuses the number and the unit with
   * no separator ("3개"). Replaces the default "<count> <label>" composition;
   * `value` still drives the tone.
   */
  formatted?: string;
  tone?: CountChipTone;
  compact?: boolean;
  /**
   * Authoritative backend total when known. When `total` exceeds the loaded
   * `value` the chip renders the total (a truthful count) instead of the
   * loaded-page length.
   */
  total?: number | null;
  /**
   * Render the displayed count as an "N+" lower bound (Dashboard pattern) when
   * the loaded page is capped and no backend `total` is available.
   */
  approxMore?: boolean;
  /**
   * Whole the displayed count is a subset of, rendered as "value/outOf" (a
   * filtered tree rail showing how many of its nodes survived the query).
   * Distinct from `total`, which replaces the value rather than qualifying it.
   */
  outOf?: number | null;
};

/** Tones whose tint deepens with the numeric value (calls = amber, writes = violet). */
const GRADED_TONES = new Set<CountChipTone>(["calls", "writes"]);

/**
 * Map a count to an intensity bucket. 0 (or non-numeric) stays neutral — a zero
 * count must not be tinted (Q3). 1-2 / 3-5 / 6-10 / 11+ deepen the tint.
 */
function intensityFor(value: number | string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return 0;
  }
  if (value <= 2) return 1;
  if (value <= 5) return 2;
  if (value <= 10) return 3;
  return 4;
}

/**
 * Neutral metric chip for plain counts ("3 calls", "10 sessions").
 *
 * Distinct from `StatusPill`: a count is not a status, so it must NOT render a
 * status dot. `tone="danger"` flags a non-zero error count. `tone="calls"` /
 * `tone="writes"` apply a value-graded amber / violet tint (0 → neutral).
 */
/**
 * Resolve the count to display. A known backend `total` greater than the loaded
 * `value` wins (truthful total); otherwise `approxMore` renders an "N+" lower
 * bound. Falls back to the raw `value` so existing callers are unaffected.
 *
 * Exported so other count surfaces (e.g. RecentItemsList's compact badge) share
 * one precedence and never diverge into a misleading "N+" over an exact total.
 */
export function resolveDisplayCount(
  value: number | string,
  total: number | null | undefined,
  approxMore: boolean,
): number | string {
  if (typeof total === "number" && typeof value === "number" && total > value) {
    return total;
  }
  if (approxMore) {
    return `${value}+`;
  }
  return value;
}

export function CountChip({
  value,
  label,
  formatted,
  tone = "neutral",
  compact = false,
  total = null,
  approxMore = false,
  outOf = null,
}: CountChipProps) {
  const graded = GRADED_TONES.has(tone);
  const intensity = graded ? intensityFor(value) : 0;
  // A graded chip with a zero count renders neutral (no tint).
  const toneClass = graded && intensity === 0 ? "neutral" : tone;
  const resolved = resolveDisplayCount(value, total, approxMore);
  const display = typeof outOf === "number" ? `${resolved}/${outOf}` : resolved;
  return (
    <span
      className={`count-chip count-chip-${toneClass}${compact ? " compact" : ""}`}
      data-intensity={intensity > 0 ? intensity : undefined}
    >
      {formatted ?? (label ? `${display} ${label}` : display)}
    </span>
  );
}

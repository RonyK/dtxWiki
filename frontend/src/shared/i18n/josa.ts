/**
 * Korean josa (조사) particle selection for i18next interpolation.
 *
 * Which of a pair (을/를, 이/가, 은/는, 으로/로) reads correctly depends on
 * whether the interpolated value ends in a Hangul syllable with a final
 * consonant (받침/batchim). The translator can't know that at authoring
 * time when the value is a runtime interpolation (a version number, a
 * title, a UUID), so strings previously shipped both forms side by side,
 * e.g. "Agent v{{version}}을(를) 현재와 비교" — see I18N-06.
 *
 * Usage in a locale value: "Agent v{{version, josaEulReul}} 현재와 비교".
 *
 * When the interpolated value is wrapped in punctuation (a quoted name,
 * for example) and the particle must render after the closing mark, use
 * the `...Only` variant, which resolves the same particle without
 * repeating the value: `"{{saved}}"{{saved, josaEuRoOnly}}` renders the
 * value once inside the quotes and only the particle after them.
 */
const PAIRS: Record<string, [string, string]> = {
  josaEulReul: ["을", "를"],
  josaIGa: ["이", "가"],
  josaEunNeun: ["은", "는"],
  josaEuRo: ["으로", "로"],
};

const ONLY_SUFFIX = "Only";

// i18next's built-in Formatter must be told about each custom format name
// individually (there is no wildcard registration) — see the
// `services.formatter.add(...)` loop in `shared/i18n/index.ts`.
export const JOSA_FORMAT_NAMES: readonly string[] = [
  ...Object.keys(PAIRS),
  ...Object.keys(PAIRS).map((name) => `${name}${ONLY_SUFFIX}`),
];
const HANGUL_SYLLABLE_START = 0xac00;
const HANGUL_SYLLABLE_END = 0xd7a3;
const HANGUL_SYLLABLE_COUNT = 28; // 종성(batchim) variants per syllable block

export function attachJosa(value: string, fmt: string): string {
  const particleOnly = fmt.endsWith(ONLY_SUFFIX);
  const pairKey = particleOnly ? fmt.slice(0, -ONLY_SUFFIX.length) : fmt;
  const pair = PAIRS[pairKey];
  if (!pair) {
    return particleOnly ? "" : value;
  }
  const [withBatchim, withoutBatchim] = pair;
  const last = value.at(-1) ?? "";
  const code = last.charCodeAt(0);
  // Not a Hangul syllable (version numbers, UUIDs, Latin titles) — fall
  // back to the parenthesized pair that was already in use so behavior
  // for non-Korean tails never regresses.
  if (code < HANGUL_SYLLABLE_START || code > HANGUL_SYLLABLE_END) {
    const bracketed = `${withBatchim}(${withoutBatchim})`;
    return particleOnly ? bracketed : `${value}${bracketed}`;
  }
  const hasBatchim = (code - HANGUL_SYLLABLE_START) % HANGUL_SYLLABLE_COUNT !== 0;
  const particle = hasBatchim ? withBatchim : withoutBatchim;
  return particleOnly ? particle : `${value}${particle}`;
}

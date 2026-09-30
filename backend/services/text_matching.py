"""Locate ``replace_text``'s ``old_text`` in a scope without guessing.

An agent that copies a sentence with one wrong character used to get
``text_occurrence_mismatch`` even with ``accept_fuzzy`` set, because that flag
only ever loosened deprecated anchor addresses. Loosening the match naively is
worse than failing: the most similar other sentence would be edited silently.
So matching runs in tiers, and a tier is used only when it proves its answer:

1. exact -- byte-for-byte, the count must equal ``expected_occurrences``;
2. normalized -- a view of the scope that undoes differences which never
   change what a reader sees (CRLF, Unicode composition, runs of horizontal
   whitespace, typographic quotes and dashes, no-break spaces), with every
   normalized character mapped back to its original span;
3. fuzzy -- only inside a block or section the caller named, only for a single
   occurrence of a long enough ``old_text``, only when exactly one candidate
   is within a small edit distance, the next candidate is clearly worse, and
   no edit touches a digit, an identifier-like token, code, a URL or
   frontmatter.

Anything short of that returns evidence (where the nearest candidates are,
by line and block, never their text) instead of writing. The matcher returns
original-body spans only; the caller splices ``new_text`` into exactly those
spans, so nothing outside them changes.

The thresholds are part of the edit contract, versioned as ``MATCH_POLICY``.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

MATCH_POLICY = "safe-v1"

MatchKind = Literal["exact", "normalized", "fuzzy"]

# Fuzzy matching is refused below this many normalized characters: a short
# phrase is too likely to be one character away from a different phrase.
FUZZY_MIN_LENGTH = 12
# Cost bounds. Past them fuzzy matching is skipped (and says so), never slowed.
FUZZY_MAX_SCOPE_CHARS = 60_000
FUZZY_MAX_PATTERN_CHARS = 4_000
DIAGNOSTIC_MAX_SCOPE_CHARS = 120_000
MAX_REPORTED_CANDIDATES = 3
MAX_REFINED_REGIONS = 6
# Block kinds (backend/utils/block_map.py) matched exactly or not at all.
PROTECTED_BLOCK_KINDS = frozenset({"code", "html", "frontmatter"})
# The runner-up must be at least this many edits worse than the chosen match.
FUZZY_MARGIN = 2
# And the chosen match must be at least this similar (1 - distance / length):
# one slip in fewer than 25 characters is already too large a share of it.
FUZZY_MIN_SIMILARITY_SHORT = 0.96  # old_text shorter than 32 characters
FUZZY_MIN_SIMILARITY = 0.94

# Written as code points: several of these characters are invisible.
_HORIZONTAL_SPACE = frozenset(
    chr(code)
    for code in (
        0x0020,
        0x0009,
        0x00A0,
        0x1680,
        0x2000,
        0x2001,
        0x2002,
        0x2003,
        0x2004,
        0x2005,
        0x2006,
        0x2007,
        0x2008,
        0x2009,
        0x200A,
        0x202F,
        0x205F,
        0x3000,
    )
)
_TRANSLATE = {
    **{chr(code): "'" for code in (0x2018, 0x2019, 0x201A, 0x201B, 0x2032)},
    **{chr(code): '"' for code in (0x201C, 0x201D, 0x201E, 0x201F, 0x2033)},
    **{
        chr(code): "-"
        for code in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)
    },
    chr(0x2026): "...",
}
_WORD_BREAK = re.compile(r"[\s\-\u2010-\u2015.,;:!?()\[\]{}<>\"'`*_#|/\\~=+&%@^$]")
_CODE_LIKE = re.compile(
    r"[A-Za-z0-9]*_[A-Za-z0-9_]*"  # snake_case, __dunder__
    r"|[A-Za-z]+[0-9][A-Za-z0-9]*|[0-9]+[A-Za-z][A-Za-z0-9]*"  # mixed alnum
    r"|[a-z]+[A-Z][A-Za-z0-9]*"  # camelCase
    r"|(?:\.{1,2}|~)?/[\w.\-/]+"  # file paths
    r"|v?\d+(?:\.\d+)+(?:[-+][\w.\-]+)?"  # versions, including prerelease tags
)
# Markdown syntax characters. A fuzzy edit touching one changes markup (a
# link, emphasis, a table cell, a code span, an escape), never just a slip.
_MARKUP_CHARS = frozenset("[]()!`*_~|<>#\\")
# What a line may start with before its content: indentation and block
# markers (headings, quotes, bullets, ordered-list numbers). This prefix is
# structure -- two spaces or a no-break space there changes nesting or block
# type -- so it is kept exactly as written.
_LINE_PREFIX_MARKERS = frozenset("#>-*+.)")
# Inline constructs whose text must match exactly. Code spans are found by a
# scanner (_code_span_zones), not a regex: a regex pairs a stray or escaped
# backtick with the next real one and unprotects every later code span.
_BARE_URL = re.compile(
    r"(?:https?://|www\.)(?:[^\s()<>\[\]]|\((?:[^\s()<>]|\([^\s()<>]*\))*\))+",
    re.IGNORECASE,
)
_AUTOLINK = re.compile(
    r"<(?:[a-z][a-z0-9+.\-]{1,31}:[^<>\s]*|[^<>\s@]+@[^<>\s@]+\.[^<>\s@]+)>",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_LINK_DESTINATION = re.compile(
    r"\]\((?:\s*<[^>\n]*>|[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?\s*\)"
)
_REFERENCE_DEFINITION = re.compile(r"^[ \t>]*\[[^\]\n]+\]:[ \t]*\S.*$", re.MULTILINE)
_REFERENCE_LABEL = re.compile(r"\]\[[^\]\n]*\]")
_WIKILINK = re.compile(r"\[\[[^\]\n]+\]\]")
_FOOTNOTE = re.compile(r"\[\^[^\]\n]+\]")
_HTML_CODE_ELEMENT = re.compile(
    r"<(code|pre|kbd|samp|tt|var)\b[^>]*>[\s\S]*?</\1\s*>", re.IGNORECASE
)
_HTML_TAG = re.compile(r"<!--[\s\S]*?-->|</?[A-Za-z][A-Za-z0-9\-]*(?:\s[^<>]*)?/?>")
_DISPLAY_MATH = re.compile(r"\$\$[\s\S]+?\$\$")
_INLINE_MATH = re.compile(r"(?<![\\$\w])\$(?=\S)[^$\n]+?(?<=\S)\$(?![\w$])")
# A delimiter row is a whole line. `^`/`$` see only LF, so on a CRLF page the
# row never matched (its CR sits before the `$`) and on a CR-only page no row
# after the first did: the row was then fuzzily "corrected" like prose.
_TABLE_DELIMITER = re.compile(
    r"(?:^|(?<=\r))[ \t>]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*"
    r"(?=\r|$)"
    r"|(?:^|(?<=\r))[ \t>]*\|[ \t]*:?-+:?[ \t]*\|?[ \t]*(?=\r|$)",
    re.MULTILINE,
)
_INLINE_EXACT_PATTERNS = (
    _BARE_URL,
    _AUTOLINK,
    _EMAIL,
    _LINK_DESTINATION,
    _REFERENCE_DEFINITION,
    _REFERENCE_LABEL,
    _WIKILINK,
    _FOOTNOTE,
    _HTML_CODE_ELEMENT,
    _HTML_TAG,
    _DISPLAY_MATH,
    _INLINE_MATH,
    _TABLE_DELIMITER,
)


def min_fuzzy_similarity(length: int) -> float:
    """Lowest similarity the fuzzy tier may accept for this length."""

    return FUZZY_MIN_SIMILARITY_SHORT if length < 32 else FUZZY_MIN_SIMILARITY


def max_fuzzy_distance(length: int) -> int:
    """Largest edit distance the fuzzy tier may accept for this length."""

    if length < FUZZY_MIN_LENGTH:
        return 0
    if length < 50:
        return 1
    if length < 100:
        return 2
    return 3


@dataclass(frozen=True)
class MatchCandidate:
    start: int
    end: int
    distance: int
    similarity: float
    kind: MatchKind


@dataclass(frozen=True)
class MatchOutcome:
    """Result of one match attempt in scope coordinates.

    ``status`` is "matched" (``spans`` hold the targets), "count_mismatch"
    (a tier found matches, but not ``expected_occurrences`` of them),
    "not_found", "ambiguous" or "low_confidence". ``candidates`` are evidence
    for a refusal; ``reason`` names why a near candidate was not used.
    """

    status: Literal[
        "matched", "count_mismatch", "not_found", "ambiguous", "low_confidence"
    ]
    kind: MatchKind | None = None
    spans: tuple[tuple[int, int], ...] = ()
    similarity: float | None = None
    candidates: tuple[MatchCandidate, ...] = ()
    reason: str | None = None
    fuzzy_skipped: str | None = None


# --------------------------------------------------------------------------
# Normalized view


@dataclass
class NormalizedView:
    text: str
    starts: list[int] = field(default_factory=list)
    ends: list[int] = field(default_factory=list)
    source: str = ""
    # True when normalization changed nothing, so normalized index i IS
    # original index i and the two offset lists are not built at all. On a
    # 1 MB page building them cost ~1 s and ~50 MB per operation.
    identity: bool = False

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        if self.identity:
            return start, end
        return self.starts[start], self.ends[end - 1]

    def covers_whole_units(self, start: int, end: int) -> bool:
        """Whether normalized ``start:end`` maps to whole original units.

        One original unit can become several normalized characters (an
        ellipsis becomes "...", a cluster may compose to two code points).
        A match over only some of them maps back to the WHOLE unit, so three
        "." matches inside one ellipsis would each claim the same original
        character and new_text would be spliced in three times. Such a match
        is not a real occurrence and is dropped. Protected zones keep one unit
        per code point, so the original span must also sit on grapheme edges.
        """

        if start >= end:
            return False
        if not self.identity:
            if start > 0 and self.starts[start] == self.starts[start - 1]:
                return False
            if end < len(self.starts) and self.starts[end] == self.starts[end - 1]:
                return False
        original_start, original_end = self.original_span(start, end)
        return _is_cluster_edge(self.source, original_start) and _is_cluster_edge(
            self.source, original_end
        )


def _is_combining(char: str) -> bool:
    if unicodedata.category(char).startswith("M"):
        return True
    code = ord(char)
    # Hangul conjoining vowels/finals continue the syllable an initial began
    # (NFD Korean, e.g. text pasted from macOS).
    return 0x1160 <= code <= 0x11FF or 0xD7B0 <= code <= 0xD7FF


_ZWJ = chr(0x200D)


def _extends_cluster(char: str) -> bool:
    # Marks and Hangul continuations (``_is_combining``), emoji skin-tone
    # modifiers and emoji tag characters all belong to the cluster before
    # them; so does a zero-width joiner.
    code = ord(char)
    return (
        _is_combining(char)
        or char == _ZWJ
        or 0x1F3FB <= code <= 0x1F3FF
        or 0xE0020 <= code <= 0xE007F
    )


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def _is_cluster_edge(text: str, position: int) -> bool:
    """Whether ``position`` falls between two grapheme clusters of ``text``.

    Never inside a CRLF pair, before a mark/modifier/joiner, right after a
    zero-width joiner (the next emoji continues the sequence), or between
    the two regional indicators of one flag. A stdlib approximation of
    UAX #29 that errs toward keeping characters together.
    """

    if position <= 0 or position >= len(text):
        return True
    before, char = text[position - 1], text[position]
    if before == "\r" and char == "\n":
        return False
    if before in "\r\n":
        return True
    if _extends_cluster(char) or before == _ZWJ:
        return False
    if _is_regional_indicator(before) and _is_regional_indicator(char):
        run = 0
        index = position - 1
        while index >= 0 and _is_regional_indicator(text[index]):
            run += 1
            index -= 1
        # Flags pair up left to right: an odd run before us is half a flag.
        return run % 2 == 0
    return True


is_cluster_edge = _is_cluster_edge


def _clusters(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for position in range(1, len(text)):
        if _is_cluster_edge(text, position):
            spans.append((start, position))
            start = position
    if text:
        spans.append((start, len(text)))
    return spans


# Every character this module rewrites or collapses, plus the run patterns: a
# repeated space, a space before a line break, and a last line of only spaces
# (a blank line, which the per-character pass drops even with no line break
# after it).
_NORMALIZES = re.compile(
    "["
    + re.escape("".join(sorted(_HORIZONTAL_SPACE - {" "})))
    + re.escape("".join(sorted(_TRANSLATE)))
    + "\r]|  |[ ]\n|(?:\\A|\n)[ ]+\\Z"
)


def _normalizes_nothing(text: str) -> bool:
    """Whether the normalized view of ``text`` would be ``text`` itself.

    Three C-speed checks (a scan for rewritten characters, the NFC quick
    check, and the distinct characters of the string) stand in for the
    per-character pass, which is the expensive part of building a view.
    Regional indicators are excluded as well: they group into flags by a
    pair rule rather than by extending, so their mapping is not the identity.
    """

    return (
        _NORMALIZES.search(text) is None
        and unicodedata.is_normalized("NFC", text)
        and not any(
            _extends_cluster(char) or _is_regional_indicator(char) for char in set(text)
        )
    )


def normalized_view(
    text: str, protected: Sequence[tuple[int, int]] = ()
) -> NormalizedView:
    """Normalize ``text`` for matching, keeping a map back to its offsets.

    Inside ``protected`` spans (code, URLs, frontmatter) characters are kept
    as written: only line endings are unified there, so a match can still pass
    through a protected span but only if it is identical there.
    """

    if _normalizes_nothing(text):
        # Protected zones only ever suppress normalization, so they cannot
        # change a view that normalizes nothing.
        return NormalizedView(text, source=text, identity=True)
    return _full_normalized_view(text, protected)


def _full_normalized_view(
    text: str, protected: Sequence[tuple[int, int]] = ()
) -> NormalizedView:
    """``normalized_view`` without the shortcut: one pass per character."""

    view = NormalizedView("", source=text)
    out: list[str] = []
    zones = sorted(protected)
    zone_index = 0

    def in_zone(position: int) -> bool:
        nonlocal zone_index
        while zone_index < len(zones) and zones[zone_index][1] <= position:
            zone_index += 1
        return zone_index < len(zones) and zones[zone_index][0] <= position

    last_was_space = False
    # At a line start, until the first content character, text is structure
    # (indentation, block markers): kept verbatim, one unit per character.
    in_prefix = True
    blank_line = _spaces_only_line(text, 0)
    for start, end in _clusters(text):
        raw = text[start:end]
        if raw in ("\r\n", "\r", "\n"):
            if (
                last_was_space
                and out
                and not _is_hard_break(text, view.starts[-1], view.ends[-1], end)
            ):
                # Trailing spaces before a line break are not visible text --
                # unless they are a Markdown hard break, which renders.
                out.pop()
                view.starts.pop()
                view.ends.pop()
            out.append("\n")
            view.starts.append(start)
            view.ends.append(end)
            last_was_space = False
            in_prefix = True
            blank_line = _spaces_only_line(text, end)
            continue
        if in_prefix:
            if blank_line:
                # A line of only spaces and tabs is a blank line: it renders
                # as nothing, so it normalizes to nothing.
                continue
            if len(raw) == 1 and _is_prefix_char(raw):
                out.append(raw)
                view.starts.append(start)
                view.ends.append(end)
                last_was_space = False
                continue
            in_prefix = False
        if in_zone(start):
            for offset, char in enumerate(raw):
                out.append(char)
                view.starts.append(start + offset)
                view.ends.append(start + offset + 1)
            last_was_space = False
            continue
        if raw in _HORIZONTAL_SPACE:
            if last_was_space:
                view.ends[-1] = end
            else:
                out.append(" ")
                view.starts.append(start)
                view.ends.append(end)
                last_was_space = True
            continue
        last_was_space = False
        composed = unicodedata.normalize("NFC", raw)
        for char in composed:
            for mapped in _TRANSLATE.get(char, char):
                out.append(mapped)
                view.starts.append(start)
                view.ends.append(end)
    view.text = "".join(out)
    return view


def _is_hard_break(text: str, run_start: int, run_end: int, line_end: int) -> bool:
    """Whether the trailing run ``text[run_start:run_end]`` before the line
    break ending at ``line_end`` is a Markdown hard break: two or more spaces
    followed by a line that is not blank. (Spaces before the end of a
    paragraph render as nothing, so those still normalize away.)

    When the text ends right after the line break -- the end of old_text, or
    of a scope cut out of the page -- the next line is not visible from here,
    so the run is kept as if it were a hard break: guessing "end of
    paragraph" deleted real breaks at scope edges.
    """

    if text.count(" ", run_start, run_end) < 2:
        return False
    position = line_end
    while position < len(text) and text[position] in " \t":
        position += 1
    return position >= len(text) or text[position] not in "\r\n"


def _is_prefix_char(char: str) -> bool:
    return (
        char in _HORIZONTAL_SPACE
        or char in _LINE_PREFIX_MARKERS
        or char.isdigit()
        or unicodedata.category(char) == "Pd"
    )


def _spaces_only_line(text: str, line_start: int) -> bool:
    position = line_start
    while position < len(text) and text[position] in " \t":
        position += 1
    return position > line_start and (position >= len(text) or text[position] in "\r\n")


def _in_line_prefix(text: str, index: int) -> bool:
    """Whether ``text[index]`` is part of its line's structural prefix."""

    line_start = max(text.rfind("\n", 0, index), text.rfind("\r", 0, index)) + 1
    return all(_is_prefix_char(char) for char in text[line_start : index + 1])


def _is_structural(text: str, index: int) -> bool:
    """Characters a fuzzy match may never edit, as "copy slips": a line
    break, the space a normalized view keeps for a hard break (the only kind
    of space that survives right before a line break), a backslash hard
    break, and a line's prefix (indentation and block markers), where one
    space or one marker changes nesting or block type."""

    if index >= len(text):
        return False
    char = text[index]
    if char == "\n":
        return True
    if char in " \\" and index + 1 < len(text) and text[index + 1] == "\n":
        return True
    return _in_line_prefix(text, index)


def normalized_pattern(text: str) -> str:
    return normalized_view(text).text


# --------------------------------------------------------------------------
# Edit distance


_INF = 1 << 30


def _banded_rows(pattern: str, window: str, radius: int) -> list[dict[int, int]]:
    """Edit-distance rows of ``pattern`` against prefixes of ``window``,
    restricted to the diagonal band |j - i| <= radius (Ukkonen). Row i maps a
    window prefix length j to the distance between pattern[:i] and window[:j].
    Cost is O(len(pattern) * radius), never quadratic."""

    width = len(window)
    rows: list[dict[int, int]] = [{j: j for j in range(0, min(width, radius) + 1)}]
    for i in range(1, len(pattern) + 1):
        previous = rows[-1]
        current: dict[int, int] = {}
        char = pattern[i - 1]
        for j in range(max(0, i - radius), min(width, i + radius) + 1):
            if j == 0:
                current[0] = i
                continue
            best = previous.get(j, _INF) + 1
            left = current.get(j - 1, _INF) + 1
            if left < best:
                best = left
            diagonal = previous.get(j - 1, _INF) + (0 if char == window[j - 1] else 1)
            if diagonal < best:
                best = diagonal
            current[j] = best
        rows.append(current)
    return rows


def _alignment(
    pattern: str, text: str, radius: int
) -> tuple[int, list[int], list[int]]:
    """Distance between ``pattern`` and all of ``text`` plus the positions an
    optimal alignment edits, or (_INF, [], []) if it exceeds ``radius``."""

    distance, touched_text, touched_pattern, _kept = _aligned(pattern, text, radius)
    return distance, touched_text, touched_pattern


def _aligned(
    pattern: str, text: str, radius: int
) -> tuple[int, list[int], list[int], dict[int, int]]:
    """``_alignment`` plus where each kept character went: text index ->
    pattern index for every character the alignment leaves unedited."""

    radius = max(radius, abs(len(text) - len(pattern)))
    rows = _banded_rows(pattern, text, radius)
    distance = rows[-1].get(len(text), _INF)
    if distance > radius:
        return _INF, [], [], {}
    touched_text: list[int] = []
    touched_pattern: list[int] = []
    kept: dict[int, int] = {}
    i, j = len(pattern), len(text)
    while i > 0 or j > 0:
        here = rows[i].get(j, _INF)
        if i > 0 and j > 0:
            cost = 0 if pattern[i - 1] == text[j - 1] else 1
            if here == rows[i - 1].get(j - 1, _INF) + cost:
                if cost:
                    touched_text.append(j - 1)
                    touched_pattern.append(i - 1)
                else:
                    kept[j - 1] = i - 1
                i, j = i - 1, j - 1
                continue
        if i > 0 and here == rows[i - 1].get(j, _INF) + 1:
            touched_pattern.append(i - 1)
            touched_text.append(max(j - 1, 0))
            i -= 1
            continue
        touched_text.append(j - 1)
        j -= 1
    return distance, touched_text, touched_pattern, kept


def best_end_distances(pattern: str, text: str) -> list[int]:
    """For each end position j, the smallest edit distance between
    ``pattern`` and any substring of ``text`` ending at j (inclusive).

    Myers' bit-parallel algorithm (Hyyro's formulation) for approximate
    search: O(len(text)) big-integer steps, no quadratic table.
    """

    length = len(pattern)
    if length == 0:
        return [0] * len(text)
    mask = (1 << length) - 1
    high = 1 << (length - 1)
    peq: dict[str, int] = {}
    for index, char in enumerate(pattern):
        peq[char] = peq.get(char, 0) | (1 << index)
    pv = mask
    mv = 0
    score = length
    scores: list[int] = []
    for char in text:
        eq = peq.get(char, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | (~(xh | pv) & mask)
        mh = pv & xh
        if ph & high:
            score += 1
        elif mh & high:
            score -= 1
        ph = (ph << 1) & mask
        mh = (mh << 1) & mask
        pv = mh | (~(xv | ph) & mask)
        mv = ph & xv
        scores.append(score)
    return scores


# --------------------------------------------------------------------------
# Tiers


def _find_whole(
    haystack: str, needle: str, is_whole: Callable[[int, int], bool]
) -> list[int]:
    """Left-to-right, non-overlapping occurrences that ``is_whole`` accepts.

    Every start is tried (not only those after the previous hit), so a hit
    rejected for splitting a unit cannot hide an overlapping real one.
    """

    found: list[int] = []
    width = max(1, len(needle))
    cursor = haystack.find(needle)
    while cursor != -1:
        if (not found or cursor >= found[-1] + width) and is_whole(
            cursor, cursor + len(needle)
        ):
            found.append(cursor)
        cursor = haystack.find(needle, cursor + 1)
    return found


def _disjoint(spans: Sequence[tuple[int, int]]) -> bool:
    return all(
        left[1] <= right[0] for left, right in zip(spans, spans[1:], strict=False)
    )


def _is_boundary(text: str, position: int) -> bool:
    if position <= 0 or position >= len(text):
        return True
    return bool(
        _WORD_BREAK.match(text[position - 1]) or _WORD_BREAK.match(text[position])
    )


def _token_spans(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end())
        for match in pattern.finditer(text)
        if match.end() > match.start()
    ]


def _touches(indexes: Sequence[int], spans: Sequence[tuple[int, int]]) -> bool:
    return any(start <= index < end for index in indexes for start, end in spans)


def _edits_are_safe(
    pattern: str,
    view_text: str,
    candidate_start: int,
    candidate_end: int,
    protected_norm: Sequence[tuple[int, int]],
) -> tuple[bool, str | None]:
    candidate = view_text[candidate_start:candidate_end]
    candidate_offset = candidate_start
    _distance, touched_text, touched_pattern = _alignment(
        pattern, candidate, max_fuzzy_distance(len(pattern)) + FUZZY_MARGIN
    )
    if any(
        _is_structural(view_text, candidate_offset + index)
        for index in touched_text
        if index < len(candidate)
    ) or any(_is_structural(pattern, index) for index in touched_pattern):
        # A line break -- or a hard break's trailing spaces -- is structure
        # (it ends a paragraph, a list item, a rendered line), not a copy
        # slip: "correcting" one would merge or split what the reader sees.
        # Checked against the whole view, so a hard-break space at the very
        # edge of the candidate still counts.
        return False, "a line break or a line's indentation or marker differs"
    if any(
        candidate[index] in _MARKUP_CHARS
        for index in touched_text
        if index < len(candidate)
    ) or any(pattern[index] in _MARKUP_CHARS for index in touched_pattern):
        return False, "Markdown markup differs"
    if any(
        candidate[index].isdigit() for index in touched_text if index < len(candidate)
    ) or any(pattern[index].isdigit() for index in touched_pattern):
        return False, "a digit differs"
    if _touches(touched_text, _token_spans(candidate, _CODE_LIKE)) or _touches(
        touched_pattern, _token_spans(pattern, _CODE_LIKE)
    ):
        return False, "an identifier-like token differs"
    absolute = [candidate_offset + index for index in touched_text]
    if _touches(absolute, protected_norm):
        return False, "the difference is inside code, a URL or frontmatter"
    return True, None


def _fuzzy_regions(pattern: str, text: str, radius: int) -> list[tuple[int, int]]:
    """(end index, distance) at every local minimum within ``radius``.

    One region per contiguous run of close ends was not enough: in
    short-period repetitive text ("yes, yes, yes, ...") every repetition
    falls inside one long run, so the run produced a single candidate, no
    rival was ever seen, and the first repetition was edited. Each valley
    of the distance curve is now a region of its own (a flat valley counts
    once), so equally close repetitions become rivals.
    """

    regions: list[tuple[int, int]] = []
    scores = best_end_distances(pattern, text)
    outside = radius + 1
    for end, score in enumerate(scores):
        if score > radius:
            continue
        before = scores[end - 1] if end > 0 else outside
        after = scores[end + 1] if end + 1 < len(scores) else outside
        if score < before and score <= after:
            regions.append((end, score))
    return regions


def _best_bounded_candidate(
    pattern: str,
    text: str,
    end_index: int,
    radius: int,
    is_whole: Callable[[int, int], bool] | None = None,
) -> MatchCandidate | None:
    """The word-bounded substring ending near ``end_index`` closest to
    ``pattern``: one banded pass per candidate start covers every end."""

    length = len(pattern)
    target_end = end_index + 1
    # distance, edge mismatches, |len diff|, start, end. Among equally close
    # spans, prefer the one whose first and last characters agree with
    # old_text's: "log tea." is as close to "log team" as to "log team.",
    # and picking the former would leave the period behind the new text.
    best: tuple[int, int, int, int, int] | None = None
    for start in range(
        max(0, target_end - length - radius), max(0, target_end - length + radius) + 1
    ):
        if not _is_boundary(text, start):
            continue
        window = text[start : start + length + radius]
        last_row = _banded_rows(pattern, window, radius)[-1]
        for prefix, distance in last_row.items():
            end = start + prefix
            if (
                distance > radius
                or prefix == 0
                or abs(end - target_end) > radius
                or not _is_boundary(text, end)
                or (is_whole is not None and not is_whole(start, end))
            ):
                continue
            edges = (text[start] != pattern[0]) + (text[end - 1] != pattern[-1])
            key = (distance, edges, abs(prefix - length), start, end)
            if best is None or key < best:
                best = key
    if best is None:
        return None
    distance, _edges, _diff, start, end = best
    similarity = 1 - distance / max(length, end - start)
    return MatchCandidate(start, end, distance, round(similarity, 4), "fuzzy")


def _fuzzy_candidates(
    pattern: str,
    text: str,
    radius: int,
    is_whole: Callable[[int, int], bool] | None = None,
) -> tuple[list[MatchCandidate], int | None]:
    """Refined candidates, plus the raw distance of the best region left
    unrefined (None when every region was refined).

    Only the ``MAX_REFINED_REGIONS`` closest regions are refined: a raw
    distance is a lower bound on any word-bounded candidate there, so an
    unrefined region can still be counted as a rival without paying for it.
    A repetitive page otherwise turned every repetition into a banded
    alignment (5.5 s on a 58 KB block).
    """

    regions = sorted(
        _fuzzy_regions(pattern, text, radius), key=lambda item: (item[1], item[0])
    )
    candidates: list[MatchCandidate] = []
    for end_index, _score in regions[:MAX_REFINED_REGIONS]:
        candidate = _best_bounded_candidate(pattern, text, end_index, radius, is_whole)
        if candidate is None:
            continue
        # Overlapping candidates are kept: in repetitive text two overlapping
        # spans can be two different targets (words 1-5 vs words 2-6), and
        # dropping the second hid exactly the ambiguity the margin exists for.
        if any(
            (candidate.start, candidate.end) == (other.start, other.end)
            for other in candidates
        ):
            continue
        candidates.append(candidate)
    unrefined = (
        regions[MAX_REFINED_REGIONS][1] if len(regions) > MAX_REFINED_REGIONS else None
    )
    return sorted(candidates, key=lambda item: (item.distance, item.start)), unrefined


def _other_site(candidate: MatchCandidate, best: MatchCandidate) -> bool:
    """Whether ``candidate`` is a different target, not a variant of ``best``.

    A span sharing an edge with the best one is the same place with a
    character more or less at the other edge ("fox" vs "fox."), which the
    tie-breaks already settled. A span with both edges elsewhere -- disjoint
    or shifted by whole words -- is a second place old_text could mean.
    """

    return candidate.start != best.start and candidate.end != best.end


def match_text(
    scope: str,
    old_text: str,
    *,
    expected_occurrences: int,
    mode: Literal["exact", "safe"],
    allow_fuzzy: bool,
    protected: (
        Sequence[tuple[int, int]] | Callable[[], Sequence[tuple[int, int]]]
    ) = (),
    diagnose: bool = True,
) -> MatchOutcome:
    """Locate ``old_text`` in ``scope`` under the tiered policy.

    ``protected`` spans are in ``scope`` coordinates, or a function returning
    them, called only when a tier past exact needs them. ``allow_fuzzy`` is
    the caller's statement that ``scope`` is one named block or section; fuzzy
    matching never runs over an unscoped document. With ``diagnose`` a refused
    call still reports the nearest candidates (by position, never text).
    """

    exact = _find_whole(
        scope,
        old_text,
        lambda start, end: (
            _is_cluster_edge(scope, start) and _is_cluster_edge(scope, end)
        ),
    )
    if exact:
        spans = tuple((start, start + len(old_text)) for start in exact)
        if len(spans) == expected_occurrences:
            return MatchOutcome("matched", "exact", spans, 1.0)
        return MatchOutcome(
            "count_mismatch",
            "exact",
            spans,
            candidates=tuple(
                MatchCandidate(start, end, 0, 1.0, "exact") for start, end in spans
            ),
        )
    if callable(protected):
        protected = protected()
    if mode == "exact":
        return _diagnosed_not_found(scope, old_text, protected, diagnose)

    view = normalized_view(scope, protected)
    # old_text keeps its own exact-only zones (code, URLs, ...) as written, as
    # the page side does: normalizing them as prose made code copied exactly
    # fail to match whenever anything else in old_text needed normalizing.
    pattern_view = normalized_view(old_text, protected_spans(old_text))
    pattern = pattern_view.text
    if not pattern.strip():
        return MatchOutcome("not_found", reason="old_text is only whitespace")
    zones: list[tuple[int, int]] | None = None

    def view_zones() -> list[tuple[int, int]]:
        nonlocal zones
        if zones is None:
            zones = _normalized_zones(view, protected)
        return zones

    def whole_hit(start: int, end: int) -> bool:
        # A hit may not end on the space a view keeps for a hard break: that
        # would take the page's break-spaces for old_text's trailing space.
        return view.covers_whole_units(start, end) and not _ends_on_hard_break(
            view.text, end
        )

    def break_tail(start: int, end: int) -> bool:
        return view.covers_whole_units(start, end) and _ends_on_hard_break(
            view.text, end
        )

    width = len(pattern)
    normalized = _find_whole(view.text, pattern, whole_hit)
    # A hit refused only for ending on a hard break is still an occurrence of
    # old_text. Leaving it out of the count let a second, ordinary occurrence
    # pass as the only one, and the edit landed where the caller never looked.
    tails = [
        start
        for start in _find_whole(view.text, pattern, break_tail)
        if all(abs(start - other) >= width for other in normalized)
    ]
    hard_break_refusal: MatchOutcome | None = None
    if normalized or tails:
        spans = tuple(view.original_span(start, start + width) for start in normalized)
        tail_spans = tuple(view.original_span(start, start + width) for start in tails)
        found = tuple(sorted(spans + tail_spans))
        if not _disjoint(found):
            # Cannot happen for whole-unit hits; fail closed if it ever does.
            return MatchOutcome("not_found", reason="matches overlap")
        if len(found) != expected_occurrences:
            return MatchOutcome(
                "count_mismatch",
                "normalized",
                found,
                candidates=tuple(
                    MatchCandidate(start, end, 0, 1.0, "normalized")
                    for start, end in found
                ),
            )
        if tail_spans:
            hard_break_refusal = MatchOutcome(
                "low_confidence",
                candidates=tuple(
                    MatchCandidate(start, end, 0, 1.0, "normalized")
                    for start, end in tail_spans[:MAX_REPORTED_CANDIDATES]
                ),
                reason=_HARD_BREAK_TAIL,
            )
            if normalized:
                return hard_break_refusal
            # Only the fuzzy tier is left. It may match old_text without its
            # trailing whitespace, which keeps the break; failing that, the
            # refusal says why the text that is there was not used.
        else:
            if not all(
                _zones_verbatim(
                    scope,
                    old_text,
                    view,
                    pattern_view,
                    start,
                    start + width,
                    lambda index, start=start: index - start,
                    view_zones(),
                )
                for start in normalized
            ):
                return MatchOutcome(
                    "low_confidence",
                    candidates=tuple(
                        MatchCandidate(start, end, 0, 1.0, "normalized")
                        for start, end in spans[:MAX_REPORTED_CANDIDATES]
                    ),
                    reason=_ZONE_DIFFERS,
                )
            return MatchOutcome("matched", "normalized", spans, 1.0)

    outcome = _fuzzy_tier(
        scope,
        old_text,
        protected,
        view,
        pattern_view,
        view_zones,
        whole_hit,
        expected_occurrences=expected_occurrences,
        allow_fuzzy=allow_fuzzy,
        diagnose=diagnose,
    )
    if outcome.status == "matched":
        return outcome
    if hard_break_refusal is not None:
        return hard_break_refusal
    return _zone_difference_refusal(view, old_text, pattern, whole_hit) or outcome


def _zone_difference_refusal(
    view: NormalizedView,
    old_text: str,
    pattern: str,
    whole_hit: Callable[[int, int], bool],
) -> MatchOutcome | None:
    """Name the real reason when old_text only differs inside its own code,
    URLs or other exact-only zones.

    Diagnostic only, after every tier refused: normalized as prose, old_text
    is found, so the refusal points there instead of reporting it absent.
    """

    prose = normalized_pattern(old_text)
    if prose == pattern:
        return None
    hits = _find_whole(view.text, prose, whole_hit)
    if not hits:
        return None
    return MatchOutcome(
        "low_confidence",
        candidates=tuple(
            MatchCandidate(
                *view.original_span(start, start + len(prose)), 0, 1.0, "normalized"
            )
            for start in hits[:MAX_REPORTED_CANDIDATES]
        ),
        reason=_ZONE_DIFFERS,
    )


def _fuzzy_tier(
    scope: str,
    old_text: str,
    protected: Sequence[tuple[int, int]],
    view: NormalizedView,
    pattern_view: NormalizedView,
    view_zones: Callable[[], list[tuple[int, int]]],
    whole_hit: Callable[[int, int], bool],
    *,
    expected_occurrences: int,
    allow_fuzzy: bool,
    diagnose: bool,
) -> MatchOutcome:
    """The third tier: one close match inside a named block or section."""

    pattern = pattern_view.text
    if not allow_fuzzy:
        return _diagnosed_not_found(
            scope,
            old_text,
            protected,
            diagnose,
            view=view,
            pattern=pattern,
            skipped="fuzzy matching needs `at` naming one block or section",
        )
    if expected_occurrences != 1:
        return _diagnosed_not_found(
            scope,
            old_text,
            protected,
            diagnose,
            view=view,
            pattern=pattern,
            skipped="fuzzy matching only replaces a single occurrence",
        )
    limit = max_fuzzy_distance(len(pattern))
    if limit == 0:
        return _diagnosed_not_found(
            scope,
            old_text,
            protected,
            diagnose,
            view=view,
            pattern=pattern,
            skipped=(
                f"old_text shorter than {FUZZY_MIN_LENGTH} characters "
                "must match exactly"
            ),
        )
    if len(view.text) > FUZZY_MAX_SCOPE_CHARS or len(pattern) > FUZZY_MAX_PATTERN_CHARS:
        return MatchOutcome(
            "not_found",
            fuzzy_skipped="the scope or old_text is too large for fuzzy matching",
        )

    protected_norm = view_zones()
    radius = limit + FUZZY_MARGIN
    # The normalized tier's whole-hit rule holds here too: a candidate that
    # ends on a hard break's kept space would splice new_text over the break.
    candidates, unrefined = _fuzzy_candidates(pattern, view.text, radius, whole_hit)
    mapped = tuple(
        MatchCandidate(
            *view.original_span(c.start, c.end), c.distance, c.similarity, "fuzzy"
        )
        for c in candidates[:MAX_REPORTED_CANDIDATES]
    )
    within = [c for c in candidates if c.distance <= limit]
    if not within:
        if candidates:
            return MatchOutcome(
                "low_confidence",
                candidates=mapped,
                reason="the closest text differs by more than this length allows",
            )
        return MatchOutcome("not_found")
    best = within[0]
    rivals = [
        c
        for c in candidates
        if _other_site(c, best) and c.distance <= best.distance + FUZZY_MARGIN - 1
    ]
    if rivals or (
        unrefined is not None and unrefined <= best.distance + FUZZY_MARGIN - 1
    ):
        return MatchOutcome(
            "ambiguous",
            candidates=mapped,
            reason="more than one place is about as close",
        )
    if best.similarity < min_fuzzy_similarity(len(pattern)):
        return MatchOutcome(
            "low_confidence",
            candidates=mapped,
            reason="the closest text differs by too large a share of old_text",
        )
    safe, why = _edits_are_safe(
        pattern, view.text, best.start, best.end, protected_norm
    )
    if not safe:
        return MatchOutcome("low_confidence", candidates=mapped, reason=why)
    _distance, _touched_text, _touched_pattern, kept = _aligned(
        pattern, view.text[best.start : best.end], radius
    )
    if not _zones_verbatim(
        scope,
        old_text,
        view,
        pattern_view,
        best.start,
        best.end,
        lambda index: kept.get(index - best.start),
        protected_norm,
    ):
        return MatchOutcome("low_confidence", candidates=mapped, reason=_ZONE_DIFFERS)
    span = view.original_span(best.start, best.end)
    return MatchOutcome("matched", "fuzzy", (span,), best.similarity, candidates=mapped)


_ZONE_DIFFERS = (
    "code, a URL, markup or frontmatter in the page is not in old_text exactly"
)
_HARD_BREAK_TAIL = (
    "old_text ends with whitespace where the page has a Markdown hard break "
    "(two or more spaces before the next line); leave that whitespace out of "
    "old_text and new_text"
)


def _ends_on_hard_break(view_text: str, end: int) -> bool:
    return (
        0 < end < len(view_text)
        and view_text[end - 1] == " "
        and view_text[end] == "\n"
    )


def _unify_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _zones_verbatim(
    scope: str,
    old_text: str,
    view: NormalizedView,
    pattern_view: NormalizedView,
    match_start: int,
    match_end: int,
    pattern_index: Callable[[int], int | None],
    zones: Sequence[tuple[int, int]],
) -> bool:
    """Whether every exact-only zone a loosened match covers is in old_text as
    written (line endings aside), at the place old_text has it.

    ``match_start:match_end`` and ``zones`` are view coordinates;
    ``pattern_index`` maps a view index to the normalized old_text index the
    match kept it as (None when the match edited it). The view keeps zones
    raw on the page side, but text normalizing made equal still matches
    there -- curly quotes typed into a code line, two spaces in a URL -- so
    the original old_text under each zone must equal the page's. Compared by
    position: a substring test passed whenever the same code also appeared,
    correctly, somewhere else in old_text.
    """

    for zone_start, zone_end in zones:
        start = max(zone_start, match_start)
        end = min(zone_end, match_end)
        if start >= end:
            continue
        page_start, page_end = view.original_span(start, end)
        piece = _unify_newlines(scope[page_start:page_end])
        if not piece.strip():
            continue
        indexes = [pattern_index(index) for index in range(start, end)]
        if any(index is None for index in indexes):
            return False
        kept = cast(list[int], indexes)
        if any(
            following != previous + 1
            for previous, following in zip(kept, kept[1:], strict=False)
        ):
            return False
        old_start, old_end = pattern_view.original_span(kept[0], kept[-1] + 1)
        if _unify_newlines(old_text[old_start:old_end]) != piece:
            return False
    return True


def _normalized_zones(
    view: NormalizedView, protected: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Protected original spans as normalized-index spans (starts and ends of
    the view are non-decreasing, so each zone is two binary searches)."""

    if view.identity:
        return [
            (max(0, start), min(len(view.text), end))
            for start, end in protected
            if start < end
        ]
    zones: list[tuple[int, int]] = []
    for zone_start, zone_end in protected:
        first = bisect_right(view.ends, zone_start)
        last = bisect_left(view.starts, zone_end)
        if first < last:
            zones.append((first, last))
    return zones


def _diagnosed_not_found(
    scope: str,
    old_text: str,
    protected: Sequence[tuple[int, int]],
    diagnose: bool,
    *,
    view: NormalizedView | None = None,
    pattern: str | None = None,
    skipped: str | None = None,
) -> MatchOutcome:
    """A refusal that still says where the nearest text is.

    The search is diagnostic only -- its result is never applied -- so it may
    run over an unscoped document, within a size bound.
    """

    if not diagnose:
        return MatchOutcome("not_found", fuzzy_skipped=skipped)
    view = view or normalized_view(scope, protected)
    pattern = pattern if pattern is not None else normalized_pattern(old_text)
    limit = max_fuzzy_distance(len(pattern))
    if (
        limit == 0
        or len(view.text) > DIAGNOSTIC_MAX_SCOPE_CHARS
        or len(pattern) > FUZZY_MAX_PATTERN_CHARS
    ):
        return MatchOutcome("not_found", fuzzy_skipped=skipped)
    candidates, _unrefined = _fuzzy_candidates(pattern, view.text, limit + FUZZY_MARGIN)
    mapped = tuple(
        MatchCandidate(
            *view.original_span(c.start, c.end), c.distance, c.similarity, "fuzzy"
        )
        for c in candidates[:MAX_REPORTED_CANDIDATES]
    )
    return MatchOutcome("not_found", candidates=mapped, fuzzy_skipped=skipped)


def inline_exact_zones(
    text: str, *, skip: Sequence[tuple[int, int]] = ()
) -> list[tuple[int, int]]:
    """Inline constructs of ``text`` that must match exactly.

    Code spans, URLs and autolinks, e-mail addresses, link and image
    destinations, reference definitions and labels, wikilinks, footnote
    labels, inline HTML (tags, and the content of code-like elements), math,
    and table delimiter rows. ``skip`` are code blocks: no code span starts
    or ends inside one.
    """

    zones = _code_span_zones(text, skip)
    for pattern in _INLINE_EXACT_PATTERNS:
        zones.extend(_token_spans(text, pattern))
    return zones


def _code_span_zones(
    text: str, skip: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Code spans the CommonMark way, in one left-to-right scan.

    A backslash escapes the next character outside a code span; a run of N
    backticks opens a span closed by the next run of exactly N backticks in
    the same paragraph (no blank line between); an unmatched run is literal.
    """

    blocked = sorted(skip)
    zones: list[tuple[int, int]] = []
    length = len(text)
    index = 0
    block_index = 0

    def next_block_start(position: int) -> int:
        nonlocal block_index
        while block_index < len(blocked) and blocked[block_index][1] <= position:
            block_index += 1
        if block_index < len(blocked):
            return blocked[block_index][0]
        return length

    while index < length:
        block_start = next_block_start(index)
        if block_start <= index:
            index = blocked[block_index][1]
            continue
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char != "`":
            index += 1
            continue
        run_end = index
        while run_end < length and text[run_end] == "`":
            run_end += 1
        run = run_end - index
        cursor, close = run_end, -1
        while cursor < min(length, block_start):
            if text[cursor] == "`":
                other = cursor
                while other < length and text[other] == "`":
                    other += 1
                if other - cursor == run:
                    close = other
                    break
                cursor = other
                continue
            if text[cursor] in "\r\n" and _blank_line_follows(text, cursor):
                break
            cursor += 1
        if close == -1:
            index = run_end
            continue
        zones.append((index, close))
        index = close
    return zones


def _blank_line_follows(text: str, newline: int) -> bool:
    position = newline + 1
    if text[newline] == "\r" and position < len(text) and text[position] == "\n":
        position += 1
    while position < len(text) and text[position] in " \t":
        position += 1
    return position >= len(text) or text[position] in "\r\n"


def protected_spans(
    text: str,
    *,
    block_kinds: Sequence[tuple[str, int, int]] = (),
    inline: bool = True,
) -> list[tuple[int, int]]:
    """Spans of ``text`` where only exact matching is allowed.

    ``block_kinds`` are ``(kind, char_start, char_end)`` for the scope's
    blocks; code blocks, raw HTML blocks and YAML frontmatter are protected
    whole, and so are inline zones a caller already computed over the whole
    document (kind ``inline``). With ``inline`` the inline zones of ``text``
    itself are added (see inline_exact_zones) -- right for a standalone text,
    wrong for a scope cut out of a larger body, whose edge can fall inside a
    code span.
    """

    spans = [
        (start, end)
        for kind, start, end in block_kinds
        if kind in PROTECTED_BLOCK_KINDS or kind == "inline"
    ]
    if inline:
        code_blocks = [
            (start, end) for kind, start, end in block_kinds if kind in ("code", "html")
        ]
        spans.extend(inline_exact_zones(text, skip=code_blocks))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged

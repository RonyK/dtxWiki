"""Shared stdlib grapheme approximation for display truncation."""

import unicodedata

# UAX #29 rules GB6-GB8: which conjoining jamo may follow which Hangul kind.
_HANGUL_JOINS = {
    "L": {"L", "V", "LV", "LVT"},
    "V": {"V", "T"},
    "LV": {"V", "T"},
    "T": {"T"},
    "LVT": {"T"},
}


def _hangul_kind(char: str) -> str | None:
    codepoint = ord(char)
    if 0x1100 <= codepoint <= 0x115F or 0xA960 <= codepoint <= 0xA97C:
        return "L"
    if 0x1160 <= codepoint <= 0x11A7 or 0xD7B0 <= codepoint <= 0xD7C6:
        return "V"
    if 0x11A8 <= codepoint <= 0x11FF or 0xD7CB <= codepoint <= 0xD7FB:
        return "T"
    if 0xAC00 <= codepoint <= 0xD7A3:
        return "LV" if (codepoint - 0xAC00) % 28 == 0 else "LVT"
    return None


def grapheme_clusters(value: str) -> list[str]:
    """Small stdlib-only grapheme approximation for safe display truncation.

    It keeps combining and spacing marks (including Thai/Lao SARA AM),
    variation selectors, emoji modifiers and tag sequences, regional indicator
    pairs, ZWJ sequences and conjoining Hangul jamo together. This avoids a
    heavyweight Unicode-regex dependency.
    """

    clusters: list[str] = []
    current = ""
    for char in value:
        codepoint = ord(char)
        extends = (
            unicodedata.category(char) in {"Mn", "Mc", "Me"}
            or codepoint in {0x0E33, 0x0EB3}
            or 0xFE00 <= codepoint <= 0xFE0F
            or 0xE0020 <= codepoint <= 0xE007F
            or 0xE0100 <= codepoint <= 0xE01EF
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or (
                bool(current)
                and _hangul_kind(char)
                in _HANGUL_JOINS.get(_hangul_kind(current[-1]) or "", ())
            )
        )
        regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        current_is_single_regional = (
            len(current) == 1 and 0x1F1E6 <= ord(current) <= 0x1F1FF
        )
        if not current:
            current = char
        elif (
            extends
            or char == "\u200d"
            or current.endswith("\u200d")
            or (regional and current_is_single_regional)
        ):
            current += char
        else:
            clusters.append(current)
            current = char
    if current:
        clusters.append(current)
    return clusters

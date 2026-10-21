"""Shared stdlib grapheme approximation for display truncation."""

import unicodedata


def grapheme_clusters(value: str) -> list[str]:
    """Small stdlib-only grapheme approximation for safe display truncation.

    It keeps combining marks, variation selectors, emoji modifiers, regional
    indicator pairs and ZWJ sequences together. This covers the client-name
    fixtures without adding a heavyweight Unicode-regex dependency.
    """

    clusters: list[str] = []
    current = ""
    for char in value:
        codepoint = ord(char)
        extends = (
            unicodedata.category(char) in {"Mn", "Mc", "Me"}
            or 0xFE00 <= codepoint <= 0xFE0F
            or 0xE0100 <= codepoint <= 0xE01EF
            or 0x1F3FB <= codepoint <= 0x1F3FF
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

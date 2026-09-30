"""Shared CommonMark fenced-code boundary recognition."""

from __future__ import annotations


def fence_opener(line: str) -> tuple[str, int] | None:
    marker_start = _fence_marker_start(line)
    if marker_start >= len(line) or line[marker_start] not in "`~":
        return None
    marker = line[marker_start]
    marker_end = marker_start
    while marker_end < len(line) and line[marker_end] == marker:
        marker_end += 1
    marker_length = marker_end - marker_start
    if marker_length < 3:
        return None
    if marker == "`" and "`" in line[marker_end:]:
        return None
    return marker, marker_length


def is_fence_closer(line: str, *, marker: str, minimum: int) -> bool:
    marker_start = _fence_marker_start(line)
    if marker_start >= len(line) or line[marker_start] != marker:
        return False
    marker_end = marker_start
    while marker_end < len(line) and line[marker_end] == marker:
        marker_end += 1
    return marker_end - marker_start >= minimum and all(
        character in " \t" for character in line[marker_end:]
    )


def _fence_marker_start(line: str) -> int:
    index = 0
    while index < min(3, len(line)) and line[index] == " ":
        index += 1
    return index

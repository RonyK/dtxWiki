"""Shared Markdown parsing and exact raw-source line coordinates.

``markdown-it`` normalizes every Markdown newline spelling internally.  Raw
coordinates must therefore not be reconstructed with ``str.split("\n")``:
that disagrees with the token line maps for CR-only input.  This module owns
both views and keeps their line indexes aligned.  It also removes a leading
BOM and masks YAML frontmatter only in the parser projection; offsets always
refer to the untouched source string.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property

from markdown_it import MarkdownIt
from markdown_it.token import Token

COMMONMARK = MarkdownIt("commonmark", {"html": True}).enable("table")

_FRONTMATTER_DELIMITERS = {"---", "..."}
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")
_BOM = "\ufeff"


@dataclass(frozen=True)
class RawLine:
    """One raw source line, excluding its terminator from ``text``."""

    text: str
    char_start: int
    content_char_end: int
    char_end: int


@dataclass(frozen=True)
class ParsedMarkdown:
    """One structural parse paired with its exact raw line table."""

    body: str
    lines: tuple[RawLine, ...]
    tokens: tuple[Token, ...]
    frontmatter_end_line: int | None

    def char_offset(self, line_index: int) -> int:
        if line_index < 0:
            raise ValueError("line index must not be negative")
        if line_index >= len(self.lines):
            return len(self.body)
        return self.lines[line_index].char_start

    def line_end_offset(self, line_index: int) -> int:
        if line_index < 0 or line_index >= len(self.lines):
            raise ValueError("line index is outside the source")
        return self.lines[line_index].char_end

    @cached_property
    def line_offsets(self) -> tuple[int, ...]:
        """Start offset of every raw line.

        Cached, not recomputed: markdown_outline._inclusive_line_end reads this
        once per heading, so a plain property made outline_for_body quadratic in
        (headings x lines) -- 16.6 s on a 1 MB heading-heavy body against 0.58 s
        cached, and no output cap can reach that cost because it is paid before
        a single entry is built.

        cached_property is safe on this frozen dataclass: it writes through
        instance.__dict__ rather than __setattr__, there are no __slots__, and
        the generated __eq__/__hash__ read only the declared fields, so a cached
        and an uncached instance stay equal.
        """

        return tuple(line.char_start for line in self.lines)


def split_raw_lines(body: str) -> tuple[RawLine, ...]:
    """Split LF, CRLF, and bare CR without losing raw byte coordinates."""

    lines: list[RawLine] = []
    start = 0
    for match in _NEWLINE_RE.finditer(body):
        lines.append(
            RawLine(
                text=body[start : match.start()],
                char_start=start,
                content_char_end=match.start(),
                char_end=match.end(),
            )
        )
        start = match.end()
    lines.append(
        RawLine(
            text=body[start:],
            char_start=start,
            content_char_end=len(body),
            char_end=len(body),
        )
    )
    return tuple(lines)


def frontmatter_end_line(lines: Sequence[str]) -> int | None:
    """Return the 0-indexed line of a leading YAML frontmatter block's
    closing delimiter, or None if `lines` does not open with one.

    Shared by markdown_outline.py and block_map.py so both exclude the same
    lines from structural (heading/block) detection -- a `---`-delimited
    frontmatter block's own content can otherwise misparse as a setext
    heading underline (a line of only `-` immediately following a text-only
    line reads as one under CommonMark's own rules).
    """
    if len(lines) < 2 or lines[0].removeprefix(_BOM).strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in _FRONTMATTER_DELIMITERS:
            return index
    return None


def parse_markdown(body: str) -> ParsedMarkdown:
    """Parse one raw body through the shared structural projection.

    Replacing frontmatter lines with empty lines, instead of slicing them
    away, is important: token ``map`` indexes remain raw-source line indexes,
    including when the closing delimiter is immediately followed by content.
    """

    raw_lines = split_raw_lines(body)
    projected = [line.text for line in raw_lines]
    if projected:
        projected[0] = projected[0].removeprefix(_BOM)
    frontmatter_end = frontmatter_end_line(projected)
    if frontmatter_end is not None:
        projected[: frontmatter_end + 1] = [""] * (frontmatter_end + 1)
    tokens = tuple(COMMONMARK.parse("\n".join(projected)))
    return ParsedMarkdown(
        body=body,
        lines=raw_lines,
        tokens=tokens,
        frontmatter_end_line=frontmatter_end,
    )

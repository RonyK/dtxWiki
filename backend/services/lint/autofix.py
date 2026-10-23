"""Deterministic, idempotent Markdown normalizer for Auto-fix mode (F2 / DEC-7).

Safe formatting only — it never changes meaning, and it never edits fenced code
blocks, indented code blocks, or the leading YAML frontmatter. Idempotent by
construction: ``normalize_markdown(normalize_markdown(x)) == normalize_markdown(x)``.

The normalizer is BLOCK-AWARE: a single pass classifies each line's context
(frontmatter / fenced code / indented code / table candidate / body) so a
transform is only ever applied to ordinary body text. When a block cannot be
proven safe to reshape (e.g. a ragged or ambiguous table), it is emitted
verbatim — formatting is a convenience, never a way to corrupt content.

Transforms applied to BODY text only:
- normalize line endings (CRLF/CR -> LF) and guarantee one trailing newline;
- strip trailing whitespace while retaining two-space Markdown hard breaks;
- headings -> ``#{level} text`` (single space after #; an ATX closing ``#`` run
  is stripped only when whitespace-preceded, so ``# C#`` keeps its content hash);
- unordered list markers ``*`` / ``+`` -> ``-`` (indent preserved), EXCEPT
  thematic breaks (``* * *``), which are left as-is;
- collapse 2+ consecutive blank lines to one (body only — blanks inside code
  survive verbatim);
- re-align GitHub tables only when the block is an unambiguous GFM table.
"""

from __future__ import annotations

import logging
import re
from array import array
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from backend.utils.markdown_fence import fence_opener, is_fence_closer
from backend.utils.page_frontmatter import frontmatter_body_offset

logger = logging.getLogger(__name__)


class MarkdownBlockKind(StrEnum):
    """Block contexts emitted by the Auto-fix normalizer.

    These are deliberately the normalizer's own contexts rather than a second
    Markdown parser.  Callers that need to reason about what Auto-fix persisted
    can therefore share the exact same classification decisions.
    """

    FRONTMATTER = "frontmatter"
    FENCE = "fence"
    INDENTED_CODE = "indented_code"
    TABLE = "table"
    PIPE_RUN = "pipe_run"
    HEADING = "heading"
    LIST = "list"
    BODY = "body"
    BLANK = "blank"


_BLOCK_KINDS = tuple(MarkdownBlockKind)
_BLOCK_KIND_CODES = {kind: index for index, kind in enumerate(_BLOCK_KINDS)}

TableCellSemanticKey = tuple[str, str]
TableRowSemanticKey = tuple[TableCellSemanticKey, ...]


@dataclass(frozen=True)
class MarkdownLineAnalysis:
    """One emitted normalized line and its originating source coordinates."""

    line_index: int
    kind: MarkdownBlockKind
    block_index: int
    source_line_index: int
    source_start: int
    source_content_end: int
    source_end: int
    normalized_start: int
    normalized_content_end: int
    normalized_end: int
    table_row_key: TableRowSemanticKey | None = None


@dataclass(frozen=True)
class MarkdownBlock:
    """A contiguous block classified by the normalization pass."""

    index: int
    kind: MarkdownBlockKind
    source_start: int
    source_end: int
    normalized_start: int
    normalized_end: int
    first_line: int
    line_count: int
    well_formed: bool | None = None


@dataclass(frozen=True)
class MarkdownBlockIndex(Sequence[MarkdownBlock]):
    """Compact random-access view over the document's classified blocks."""

    kinds: array
    source_starts: array
    source_ends: array
    normalized_starts: array
    normalized_ends: array
    first_lines: array
    line_counts: array
    well_formed: array

    def __len__(self) -> int:
        return len(self.kinds)

    def __getitem__(
        self, index: int | slice
    ) -> MarkdownBlock | tuple[MarkdownBlock, ...]:
        if isinstance(index, slice):
            return tuple(self[item] for item in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        well_formed_code = self.well_formed[index]
        return MarkdownBlock(
            index=index,
            kind=_BLOCK_KINDS[self.kinds[index]],
            source_start=self.source_starts[index],
            source_end=self.source_ends[index],
            normalized_start=self.normalized_starts[index],
            normalized_end=self.normalized_ends[index],
            first_line=self.first_lines[index],
            line_count=self.line_counts[index],
            well_formed=None if well_formed_code < 0 else bool(well_formed_code),
        )


@dataclass(frozen=True)
class MarkdownAnalysis:
    """Normalized bytes plus the source-to-block index from the same pass.

    The source-line lookup arrays use compact C integers: a one-megabyte
    newline-heavy page must not allocate one Python integer per line merely to
    support point lookup.  A mapping entry of ``-1`` denotes a source line that
    normalization discarded (for example, an extra blank in a collapsed run).
    """

    source: str
    normalized: str
    canonical: bool
    blocks: MarkdownBlockIndex
    source_line_starts: array
    source_line_to_output_line: array
    source_line_to_block: array
    normalized_line_starts: array
    output_line_to_source_line: array

    @property
    def source_length(self) -> int:
        return len(self.source)

    def source_line_index_at(self, point: int) -> int | None:
        """Return the source line containing ``point`` (EOF is the last line)."""

        if point < 0 or point > self.source_length or not self.source_line_starts:
            return None
        return bisect_right(self.source_line_starts, point) - 1

    def source_line_span(self, line_index: int) -> tuple[int, int, int]:
        """Return ``(start, content_end, end)`` for one exact source line.

        ``end`` includes the source's original LF, CRLF, or CR terminator while
        ``content_end`` excludes it.  The final virtual line after a trailing
        terminator is represented by the zero-width span at EOF.
        """

        if line_index < 0:
            line_index += len(self.source_line_starts)
        if line_index < 0 or line_index >= len(self.source_line_starts):
            raise IndexError(line_index)
        source_start = self.source_line_starts[line_index]
        source_end = (
            self.source_line_starts[line_index + 1]
            if line_index + 1 < len(self.source_line_starts)
            else len(self.source)
        )
        source_content_end = source_end
        if (
            source_content_end > source_start
            and self.source[source_content_end - 1] == "\n"
        ):
            source_content_end -= 1
            if (
                source_content_end > source_start
                and self.source[source_content_end - 1] == "\r"
            ):
                source_content_end -= 1
        elif (
            source_content_end > source_start
            and self.source[source_content_end - 1] == "\r"
        ):
            source_content_end -= 1
        return source_start, source_content_end, source_end

    def block_at_source_point(self, point: int) -> MarkdownBlock | None:
        """Return the emitted block owning the source line at ``point``.

        This lookup is useful on both raw and canonical input.  Callers mapping
        normalized byte offsets back to source bytes must additionally require
        ``canonical``; transformations can otherwise change offsets even though
        the source block identity remains valid.
        """

        line_index = self.source_line_index_at(point)
        if line_index is None or line_index >= len(self.source_line_to_block):
            return None
        block_index = self.source_line_to_block[line_index]
        if block_index < 0:
            return None
        return self.blocks[block_index]

    def line_at_source_point(self, point: int) -> MarkdownLineAnalysis | None:
        """Return the emitted line owning ``point`` without scanning its block."""

        source_line_index = self.source_line_index_at(point)
        if source_line_index is None or source_line_index >= len(
            self.source_line_to_output_line
        ):
            return None
        output_line_index = self.source_line_to_output_line[source_line_index]
        if output_line_index < 0:
            return None
        return self.line_at_output_index(output_line_index)

    def line_at_output_index(self, line_index: int) -> MarkdownLineAnalysis:
        """Materialize one line descriptor from compact indexes on demand."""

        if line_index < 0:
            line_index += len(self.output_line_to_source_line)
        if line_index < 0 or line_index >= len(self.output_line_to_source_line):
            raise IndexError(line_index)
        source_line_index = self.output_line_to_source_line[line_index]
        block_index = self.source_line_to_block[source_line_index]
        block = self.blocks[block_index]
        source_start, source_content_end, source_end = self.source_line_span(
            source_line_index
        )
        normalized_start = self.normalized_line_starts[line_index]
        normalized_end = (
            self.normalized_line_starts[line_index + 1]
            if line_index + 1 < len(self.normalized_line_starts)
            else len(self.normalized)
        )
        normalized_content_end = (
            normalized_end - 1
            if normalized_end > normalized_start
            and self.normalized[normalized_end - 1] == "\n"
            else normalized_end
        )
        normalized_line = self.normalized[normalized_start:normalized_content_end]
        return MarkdownLineAnalysis(
            line_index=line_index,
            kind=block.kind,
            block_index=block_index,
            source_line_index=source_line_index,
            source_start=source_start,
            source_content_end=source_content_end,
            source_end=source_end,
            normalized_start=normalized_start,
            normalized_content_end=normalized_content_end,
            normalized_end=normalized_end,
            table_row_key=(
                table_row_semantic_key(
                    normalized_line,
                    separator=line_index == block.first_line + 1,
                )
                if block.kind == MarkdownBlockKind.TABLE
                else None
            ),
        )

    def block_lines(self, block: MarkdownBlock) -> tuple[MarkdownLineAnalysis, ...]:
        """Materialize only the requested block's lines."""

        return tuple(
            self.line_at_output_index(line_index)
            for line_index in range(
                block.first_line, block.first_line + block.line_count
            )
        )


# A heading REQUIRES whitespace after the #'s (CommonMark); "#word" is a
# paragraph. An ATX closing sequence is only stripped when whitespace-preceded,
# so a content hash like "# C#" is preserved (the trailing #+ needs a leading
# \s+ to count as a closer).
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_UL_RE = re.compile(r"^(\s*)[*+-](\s+)(.*)$")
# Thematic break: >=3 of the SAME -, * or _ marker, optionally space/tab
# separated. Checked before the list rule so "* * *" is never made a list item.
_THEMATIC_BREAK_RE = re.compile(r"^ {0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")


def _is_indented_code(line: str) -> bool:
    """True for a non-blank line indented >=4 spaces (or a leading tab)."""
    if not line.strip():
        return False
    if line.startswith("\t"):
        return True
    return len(line) - len(line.lstrip(" ")) >= 4


def _split_cells(row: str) -> list[str]:
    """Split a table row into trimmed cells.

    Honors backslash escapes (``\\|``) and inline code spans (`` `a|b` ``) so a
    pipe inside a cell is never mistaken for a column delimiter, and drops the
    optional leading/trailing outer pipes.
    """
    s = row.strip()
    cells: list[str] = []
    buf: list[str] = []
    backtick_run = 0  # length of the inline code span we're inside, or 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            buf.append(s[i : i + 2])  # keep the escape + escaped char verbatim
            i += 2
            continue
        if ch == "`":
            j = i
            while j < len(s) and s[j] == "`":
                j += 1
            run = j - i
            if backtick_run == 0:
                backtick_run = run
            elif run == backtick_run:
                backtick_run = 0
            buf.append("`" * run)
            i = j
            continue
        if ch == "|" and backtick_run == 0:
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells)


def _is_well_formed_table(rows: list[list[str]]) -> bool:
    """True only for an unambiguous GFM table: >=2 rows, the separator row
    immediately after the header, and every row sharing the header's column
    count. Anything else is left verbatim (fail-closed)."""
    if len(rows) < 2:
        return False
    if not _is_separator_row(rows[1]):
        return False
    header_w = len(rows[0])
    return header_w > 0 and all(len(r) == header_w for r in rows)


def _cell_align(sep_cell: str) -> str:
    left = sep_cell.startswith(":")
    right = sep_cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    if left:
        return "left"
    return "none"


def table_row_semantic_key(row: str, *, separator: bool = False) -> TableRowSemanticKey:
    """Return a width-independent semantic key for one table row.

    Cell padding and optional outer pipes are intentionally absent.  Escaped
    pipes and pipes inside inline-code spans remain cell content because this
    delegates to the same splitter used by table formatting.  When the caller
    identifies the table's separator row, dash width is non-semantic while
    alignment remains part of the key.  A data cell containing ``---`` stays
    ordinary text and cannot be confused with the separator row.
    """

    return tuple(
        ("separator", _cell_align(cell))
        if separator and _TABLE_SEP_CELL_RE.fullmatch(cell)
        else ("text", cell)
        for cell in _split_cells(row)
    )


def _sep_cell(width: int, align: str) -> str:
    if align == "center":
        return ":" + "-" * (width - 2) + ":"
    if align == "right":
        return "-" * (width - 1) + ":"
    if align == "left":
        return ":" + "-" * (width - 1)
    return "-" * width


def _format_table(block: list[str]) -> list[str]:
    """Re-align an unambiguous GFM table (precondition: ``_is_well_formed_table``).

    Every row has exactly ``len(header)`` cells and the separator is row 1, so no
    padding or separator search is needed (both were StopIteration footguns).
    """
    rows = [_split_cells(line) for line in block]
    width = len(rows[0])
    sep_idx = 1
    aligns = [_cell_align(c) for c in rows[sep_idx]]

    col_w = [0] * width
    for i, r in enumerate(rows):
        if i == sep_idx:
            continue
        for c in range(width):
            col_w[c] = max(col_w[c], len(r[c]))
    col_w = [max(3, w) for w in col_w]  # min width 3 so "---" fits

    out: list[str] = []
    for i, r in enumerate(rows):
        if i == sep_idx:
            cells = (_sep_cell(col_w[c], aligns[c]) for c in range(width))
        else:
            cells = (r[c].ljust(col_w[c]) for c in range(width))
        out.append("| " + " | ".join(cells) + " |")
    return out


def _strip_trailing_whitespace(line: str, *, preserve_hard_break: bool) -> str:
    hard_break = preserve_hard_break and line.endswith("  ")
    return line.rstrip() + ("  " if hard_break else "")


def _normalize_body_line(line: str, *, preserve_hard_break: bool = False) -> str:
    hard_break = preserve_hard_break and line.endswith("  ")
    line = line.rstrip()
    if _THEMATIC_BREAK_RE.match(line):
        return line  # thematic break — never rewrite into a list item
    heading = _HEADING_RE.match(line)
    if heading:
        return f"{heading.group(1)} {heading.group(2)}".rstrip()
    ul = _UL_RE.match(line)
    if ul:
        line = f"{ul.group(1)}-{ul.group(2)}{ul.group(3)}".rstrip()
    return line + ("  " if hard_break else "")


def _source_line_start_index(body: str) -> array:
    """Return compact exact source starts for LF, CRLF, and CR lines."""

    starts = array("I", [0])
    cursor = 0
    while cursor < len(body):
        char = body[cursor]
        if char not in "\r\n":
            cursor += 1
            continue
        cursor += 1
        if char == "\r" and cursor < len(body) and body[cursor] == "\n":
            cursor += 1
        starts.append(cursor)
    return starts


def _ordinary_line_kind(line: str) -> MarkdownBlockKind:
    if not _THEMATIC_BREAK_RE.match(line):
        heading = _HEADING_RE.match(line)
        if heading:
            return MarkdownBlockKind.HEADING
        unordered = _UL_RE.match(line)
        if unordered:
            return MarkdownBlockKind.LIST
    return MarkdownBlockKind.BODY


def analyze_markdown(
    body: str, *, preserve_hard_breaks: bool = True
) -> MarkdownAnalysis:
    """Normalize and classify Markdown in the normalizer's single block pass."""

    source_line_starts = _source_line_start_index(body)
    if not body:
        return MarkdownAnalysis(
            source=body,
            normalized="",
            canonical=True,
            blocks=MarkdownBlockIndex(
                kinds=array("B"),
                source_starts=array("I"),
                source_ends=array("I"),
                normalized_starts=array("I"),
                normalized_ends=array("I"),
                first_lines=array("I"),
                line_counts=array("I"),
                well_formed=array("b"),
            ),
            source_line_starts=source_line_starts,
            source_line_to_output_line=array("i", [-1]),
            source_line_to_block=array("i", [-1]),
            normalized_line_starts=array("I"),
            output_line_to_source_line=array("I"),
        )

    text = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if len(lines) != len(source_line_starts):  # pragma: no cover - invariant
        raise ValueError("source line map diverged from canonical line endings")
    offset = frontmatter_body_offset(lines)
    frontmatter = lines[:offset]
    body_lines = lines[offset:]
    had_separator = bool(body_lines) and body_lines[0].strip() == ""

    out: list[str] = []
    out_source_lines = array("I")
    out_kinds = array("B")
    out_groups = array("i")
    pending_blank = False
    pending_blank_source: int | None = None
    pipe_run: list[tuple[str, int]] = []
    next_group_token = 0

    def new_group_token() -> int:
        nonlocal next_group_token
        token = next_group_token
        next_group_token += 1
        return token

    def append_output(
        line: str,
        source_line_index: int,
        kind: MarkdownBlockKind,
        *,
        group_token: int = -1,
    ) -> None:
        out.append(line)
        out_source_lines.append(source_line_index)
        out_kinds.append(_BLOCK_KIND_CODES[kind])
        out_groups.append(group_token)

    def emit_pending_blank() -> None:
        nonlocal pending_blank, pending_blank_source
        if pending_blank and out:
            if pending_blank_source is None:  # pragma: no cover - invariant
                raise ValueError("pending blank has no source line")
            append_output(
                "",
                pending_blank_source,
                MarkdownBlockKind.BLANK,
            )
        pending_blank = False
        pending_blank_source = None

    def emit(
        line: str,
        source_line_index: int,
        kind: MarkdownBlockKind,
        *,
        group_token: int = -1,
    ) -> None:
        emit_pending_blank()
        append_output(
            line,
            source_line_index,
            kind,
            group_token=group_token,
        )

    def flush_pipe_run() -> None:
        if not pipe_run:
            return
        raw_lines = [line for line, _source_line_index in pipe_run]
        rows = [_split_cells(line) for line in raw_lines]
        well_formed = _is_well_formed_table(rows)
        kind = MarkdownBlockKind.TABLE if well_formed else MarkdownBlockKind.PIPE_RUN
        emitted_lines = _format_table(raw_lines) if well_formed else raw_lines
        group_token = new_group_token()
        for emitted_line, (_raw_line, source_line_index) in zip(
            emitted_lines, pipe_run, strict=True
        ):
            normalized_line = (
                emitted_line
                if well_formed
                else _strip_trailing_whitespace(
                    emitted_line,
                    preserve_hard_break=(
                        preserve_hard_breaks
                        and source_line_index + 1 < len(lines)
                        and bool(lines[source_line_index + 1].strip())
                    ),
                )
            )
            emit(
                normalized_line,
                source_line_index,
                kind,
                group_token=group_token,
            )
        pipe_run.clear()

    i = 0
    n = len(body_lines)
    while i < n:
        line = body_lines[i]
        source_line_index = offset + i

        opener = fence_opener(line)
        if opener is not None:
            marker, fence_len = opener
            flush_pipe_run()
            emit_pending_blank()
            group_token = new_group_token()
            opening = line.rstrip()
            append_output(
                opening,
                source_line_index,
                MarkdownBlockKind.FENCE,
                group_token=group_token,
            )
            i += 1
            while i < n:
                fenced_line = body_lines[i]
                append_output(
                    fenced_line,
                    offset + i,
                    MarkdownBlockKind.FENCE,
                    group_token=group_token,
                )
                i += 1
                if is_fence_closer(
                    fenced_line,
                    marker=marker,
                    minimum=fence_len,
                ):
                    break
            continue

        if not pipe_run and _is_indented_code(line) and (pending_blank or not out):
            emit_pending_blank()
            last = i
            j = i
            while j < n and (
                not body_lines[j].strip() or _is_indented_code(body_lines[j])
            ):
                if body_lines[j].strip():
                    last = j
                j += 1
            group_token = new_group_token()
            for code_index in range(i, last + 1):
                code_line = body_lines[code_index]
                append_output(
                    code_line,
                    offset + code_index,
                    MarkdownBlockKind.INDENTED_CODE,
                    group_token=group_token,
                )
            i = last + 1
            continue

        if not line.strip():
            flush_pipe_run()
            pending_blank = True
            if pending_blank_source is None:
                pending_blank_source = source_line_index
            i += 1
            continue

        if "|" in line:
            pipe_run.append((line, source_line_index))
            i += 1
            continue

        flush_pipe_run()
        normalized_line = _normalize_body_line(
            line,
            preserve_hard_break=(
                preserve_hard_breaks and i + 1 < n and bool(body_lines[i + 1].strip())
            ),
        )
        kind = _ordinary_line_kind(normalized_line)
        emit(normalized_line, source_line_index, kind)
        i += 1

    flush_pipe_run()

    result_lines = list(frontmatter)
    result_source_lines = array("I", range(len(frontmatter)))
    result_kinds = array(
        "B",
        [_BLOCK_KIND_CODES[MarkdownBlockKind.FRONTMATTER]],
    ) * len(frontmatter)
    # -1 denotes ordinary lines, whose block grouping is kind-based.  -2 is a
    # private explicit group for the complete leading frontmatter block; body
    # scanner group tokens start at zero.
    result_groups = array("i", [-2]) * len(frontmatter)
    if frontmatter and had_separator and out:
        result_lines.append("")
        result_source_lines.append(offset)
        result_kinds.append(_BLOCK_KIND_CODES[MarkdownBlockKind.BLANK])
        result_groups.append(-1)
    result_lines.extend(out)
    result_source_lines.extend(out_source_lines)
    result_kinds.extend(out_kinds)
    result_groups.extend(out_groups)
    while result_lines and result_lines[-1] == "":
        result_lines.pop()
        result_source_lines.pop()
        result_kinds.pop()
        result_groups.pop()

    normalized = "\n".join(result_lines) + "\n" if result_lines else "\n"
    source_line_to_output_line = array("i", [-1]) * len(source_line_starts)
    source_line_to_block = array("i", [-1]) * len(source_line_starts)
    normalized_line_starts = array("I")

    block_kinds = array("B")
    block_source_starts = array("I")
    block_source_ends = array("I")
    block_normalized_starts = array("I")
    block_normalized_ends = array("I")
    block_first_lines = array("I")
    block_line_counts = array("I")
    block_well_formed = array("b")

    ordinary_group_kinds = {
        _BLOCK_KIND_CODES[MarkdownBlockKind.BODY],
        _BLOCK_KIND_CODES[MarkdownBlockKind.LIST],
        _BLOCK_KIND_CODES[MarkdownBlockKind.BLANK],
    }
    normalized_cursor = 0
    previous_kind_code: int | None = None
    previous_group_token: int | None = None
    for line_index, (line, source_line_index, kind_code, group_token) in enumerate(
        zip(
            result_lines,
            result_source_lines,
            result_kinds,
            result_groups,
            strict=True,
        )
    ):
        source_start = source_line_starts[source_line_index]
        source_end = (
            source_line_starts[source_line_index + 1]
            if source_line_index + 1 < len(source_line_starts)
            else len(body)
        )
        normalized_line_starts.append(normalized_cursor)
        normalized_end = normalized_cursor + len(line) + 1

        same_explicit_group = group_token != -1 and group_token == previous_group_token
        same_ordinary_group = (
            group_token == -1
            and previous_group_token == -1
            and kind_code == previous_kind_code
            and kind_code in ordinary_group_kinds
        )
        if not same_explicit_group and not same_ordinary_group:
            block_kinds.append(kind_code)
            block_source_starts.append(source_start)
            block_source_ends.append(source_end)
            block_normalized_starts.append(normalized_cursor)
            block_normalized_ends.append(normalized_end)
            block_first_lines.append(line_index)
            block_line_counts.append(1)
            block_well_formed.append(
                1
                if kind_code == _BLOCK_KIND_CODES[MarkdownBlockKind.TABLE]
                else 0
                if kind_code == _BLOCK_KIND_CODES[MarkdownBlockKind.PIPE_RUN]
                else -1
            )
        else:
            block_source_ends[-1] = source_end
            block_normalized_ends[-1] = normalized_end
            block_line_counts[-1] += 1

        block_index = len(block_kinds) - 1
        source_line_to_output_line[source_line_index] = line_index
        source_line_to_block[source_line_index] = block_index
        normalized_cursor = normalized_end
        previous_kind_code = kind_code
        previous_group_token = group_token

    return MarkdownAnalysis(
        source=body,
        normalized=normalized,
        canonical=normalized == body,
        blocks=MarkdownBlockIndex(
            kinds=block_kinds,
            source_starts=block_source_starts,
            source_ends=block_source_ends,
            normalized_starts=block_normalized_starts,
            normalized_ends=block_normalized_ends,
            first_lines=block_first_lines,
            line_counts=block_line_counts,
            well_formed=block_well_formed,
        ),
        source_line_starts=source_line_starts,
        source_line_to_output_line=source_line_to_output_line,
        source_line_to_block=source_line_to_block,
        normalized_line_starts=normalized_line_starts,
        output_line_to_source_line=result_source_lines,
    )


def normalize_markdown(body: str) -> str:
    return analyze_markdown(body).normalized


def safe_analyze_markdown(body: str) -> MarkdownAnalysis | None:
    """Fail closed when the shared normalizer/indexer encounters a fault."""

    try:
        return analyze_markdown(body)
    except Exception:  # pragma: no cover - defensive; the normalizer is total
        logger.exception("analyze_markdown failed; no semantic index available")
        return None


def safe_normalize_markdown(body: str) -> str:
    """Fail-closed wrapper around :func:`normalize_markdown`.

    Formatting is a convenience, never a save prerequisite — a normalizer fault
    must degrade to "leave the body as authored", never a 500 or an aborted
    save. Used by the write paths and the ``/lint/format`` endpoint.
    """
    analysis = safe_analyze_markdown(body)
    return body if analysis is None else analysis.normalized


def safe_normalize_markdown_legacy_v1(body: str) -> str:
    """Replay old branch proofs that predate hard-break preservation.

    New writes use :func:`safe_normalize_markdown`. A manifest without an
    explicit transform still needs the exact formatter behavior it captured.
    """
    try:
        return analyze_markdown(body, preserve_hard_breaks=False).normalized
    except Exception:  # pragma: no cover - defensive legacy proof replay
        logger.exception("Legacy Markdown formatting failed; retaining the prior body")
        return body

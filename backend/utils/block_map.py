"""Content-derived half of a page version's block map.

A block map is the persisted, ID-addressed replacement for absolute-position
addressing: an ordered list of top-level (CommonMark level-0) blocks, each
carrying a kind, a content hash, and (for headings) a subtree fold hash and
(for tables) per-row hashes.

This module computes only the ID-FREE fields (kind/hash/sub/lines/gap/rows) --
a pure function of `body_md`, independently reproducible from stored
markdown at any time. That is what makes the section 2.3 invariant gate
possible: recompute from the stored markdown and diff against the persisted
map. Block IDs themselves are assigned once, at write time, by diffing the
previous version's block map against this one (section 2.2, carry-forward)
-- that belongs to the write path, not here.

No absolute position (char/line range) is stored on a `BlockContent` for the
same reason `ContextPageVersion.block_map` stores none (section 2.1): a
block's own `lines` count is a local fact of the block itself and survives
edits elsewhere in the document, but an absolute range is a document-global
value that shifts under any earlier edit and is therefore not worth caching.
`gap` is the number of unowned raw lines between the preceding block and this
block (and therefore includes leading/frontmatter lines for the first block).
Unlike an absolute range it is an adjacency-local fact: changing a separator
only invalidates the following edge.  It is required because summing block
`lines` alone cannot recover positions when separator blank lines are excluded
from those counts.

The `line_start`/`line_end` fields below are a computation *aid* (line
offsets into the exact `body_md` this call was given), not something a
caller should carry across versions -- serialize only kind/hash/sub/lines/
gap/rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256

from backend.services.lint.autofix import table_row_semantic_key
from backend.utils.content_hash import content_hash
from backend.utils.markdown_parser import (
    RawLine,
    frontmatter_end_line,
    parse_markdown,
    split_raw_lines,
)

# token.type -> block kind, for every top-level block type this parser
# configuration (commonmark + table, see markdown_parser.py) can produce.
# heading_open and table_open are handled separately (kind depends on
# token.tag / carries rows respectively).
_KIND_BY_TOKEN_TYPE = {
    "paragraph_open": "p",
    "bullet_list_open": "list",
    "ordered_list_open": "list",
    "blockquote_open": "blockquote",
    "fence": "code",
    "code_block": "code",
    "html_block": "html",
    "hr": "thematic_break",
}


@dataclass(frozen=True)
class BlockRow:
    hash: str


@dataclass(frozen=True)
class BlockContent:
    kind: str
    hash: str
    lines: int
    gap: int
    line_start: int
    line_end: int
    sub: str | None = None
    rows: tuple[BlockRow, ...] = ()


def serialize_blocks(blocks: tuple[BlockContent, ...]) -> list[dict]:
    """JSON-ready form for `ContextPageVersion.block_map` -- kind/hash/lines/
    gap/sub/rows only. `line_start`/`line_end`
    are this-call-only computation aids (see module docstring) and are
    deliberately not serialized.
    """
    serialized: list[dict] = []
    for block in blocks:
        entry: dict = {
            "kind": block.kind,
            "hash": block.hash,
            "lines": block.lines,
            "gap": block.gap,
        }
        if block.sub is not None:
            entry["sub"] = block.sub
        if block.rows:
            entry["rows"] = [{"hash": row.hash} for row in block.rows]
        serialized.append(entry)
    return serialized


def compute_blocks(body_md: str) -> tuple[BlockContent, ...]:
    """Return the ordered, ID-free block map for `body_md`."""
    parsed = parse_markdown(body_md)
    tokens = parsed.tokens
    offsets = parsed.line_offsets

    entries: list[tuple[int, int, str, tuple[BlockRow, ...]]] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if token.level != 0 or token.map is None:
            index += 1
            continue
        line_start, line_end = token.map
        rows: tuple[BlockRow, ...] = ()
        if token.type == "heading_open":
            kind = token.tag
        elif token.type == "table_open":
            kind = "table"
            rows = _table_rows(tokens, index, parsed.lines)
        else:
            kind = _KIND_BY_TOKEN_TYPE.get(token.type)
        if kind is None:
            index += 1
            continue
        entries.append((line_start, line_end, kind, rows))
        index += 1

    heading_char_ends = _heading_subtree_char_ends(entries, offsets, len(body_md))

    blocks: list[BlockContent] = []
    previous_line_end = 0
    for entry_index, (line_start, line_end, kind, rows) in enumerate(entries):
        char_start = parsed.char_offset(line_start)
        char_end = parsed.char_offset(line_end)
        block_text = body_md[char_start:char_end]
        sub = None
        if kind.startswith("h") and kind[1:].isdigit():
            subtree_char_end = heading_char_ends[entry_index]
            sub = content_hash(body_md[char_start:subtree_char_end])
        blocks.append(
            BlockContent(
                kind=kind,
                hash=content_hash(block_text),
                lines=line_end - line_start,
                gap=line_start - previous_line_end,
                line_start=line_start,
                line_end=line_end,
                sub=sub,
                rows=rows,
            )
        )
        previous_line_end = line_end
    return tuple(blocks)


def blocks_from_persisted_map(
    body_md: str,
    block_map: Sequence[Mapping[str, object]],
) -> tuple[BlockContent, ...] | None:
    """Reconstruct block coordinates from ``gap + lines`` without parsing.

    The persisted coordinates are only a cache.  Every reconstructed raw
    slice is checked in the same normalized hash space as ``compute_blocks``;
    malformed, legacy, or stale cache data returns ``None`` so callers can
    take the authoritative full-parser fallback.  This is the design §3.3
    prefix-sum path shared by address resolution and incremental map writes.
    """

    raw_lines = split_raw_lines(body_md)
    frontmatter_end = frontmatter_end_line([line.text for line in raw_lines])
    cursor = 0
    resolved: list[BlockContent] = []
    for block_index, stored in enumerate(block_map):
        kind = stored.get("kind")
        stored_hash = stored.get("hash")
        lines = stored.get("lines")
        gap = stored.get("gap")
        sub = stored.get("sub")
        is_heading = (
            isinstance(kind, str) and kind.startswith("h") and kind[1:].isdigit()
        )
        if (
            not isinstance(kind, str)
            or not isinstance(stored_hash, str)
            or not isinstance(lines, int)
            or isinstance(lines, bool)
            or lines <= 0
            or not isinstance(gap, int)
            or isinstance(gap, bool)
            or gap < 0
            or (is_heading and not isinstance(sub, str))
            or (not is_heading and sub is not None)
        ):
            return None
        line_start = cursor + gap
        line_end = line_start + lines
        if line_start < cursor or line_end > len(raw_lines):
            return None
        if any(
            raw_lines[index].text.strip()
            and not (
                block_index == 0
                and frontmatter_end is not None
                and index <= frontmatter_end
            )
            for index in range(cursor, line_start)
        ):
            return None
        char_start = raw_lines[line_start].char_start
        char_end = (
            raw_lines[line_end].char_start
            if line_end < len(raw_lines)
            else len(body_md)
        )
        if content_hash(body_md[char_start:char_end]) != stored_hash:
            return None

        stored_rows = stored.get("rows") or ()
        if not isinstance(stored_rows, (list, tuple)):
            return None
        rows: list[BlockRow] = []
        if stored_rows and kind != "table":
            return None
        for row_index, stored_row in enumerate(stored_rows):
            if not isinstance(stored_row, Mapping):
                return None
            row_hash = stored_row.get("hash")
            raw_index = line_start + 2 + row_index
            if (
                not isinstance(row_hash, str)
                or raw_index >= line_end
                or table_row_hash(raw_lines[raw_index].text) != row_hash
            ):
                return None
            rows.append(BlockRow(hash=row_hash))
        resolved.append(
            BlockContent(
                kind=kind,
                hash=stored_hash,
                lines=lines,
                gap=gap,
                line_start=line_start,
                line_end=line_end,
                sub=sub,
                rows=tuple(rows),
            )
        )
        cursor = line_end
    if any(line.text.strip() for line in raw_lines[cursor:]):
        return None
    return tuple(resolved)


def _heading_subtree_char_ends(
    entries: list[tuple[int, int, str, tuple[BlockRow, ...]]],
    offsets: tuple[int, ...],
    body_length: int,
) -> list[int]:
    """For each entry, the char offset where a heading's subtree ends (body
    length for non-headings, unused by the caller in that case)."""
    ends = [body_length] * len(entries)
    open_headings: list[tuple[int, int]] = []  # (entry_index, level)
    for entry_index, (line_start, _line_end, kind, _rows) in enumerate(entries):
        if not (kind.startswith("h") and kind[1:].isdigit()):
            continue
        level = int(kind[1:])
        while open_headings and open_headings[-1][1] >= level:
            open_index, _open_level = open_headings.pop()
            ends[open_index] = offsets[line_start]
        open_headings.append((entry_index, level))
    return ends


def _table_rows(
    tokens: tuple, table_open_index: int, lines: tuple[RawLine, ...]
) -> tuple[BlockRow, ...]:
    """Data-row hashes for the table opened at `tokens[table_open_index]`.

    Header/separator rows are structural, not addressable content, so only
    `tbody` rows become `BlockRow` entries -- matching the design doc's own
    example (section 2.1), which lists exactly the data rows.
    """
    rows: list[BlockRow] = []
    depth = 0
    in_tbody = False
    index = table_open_index
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if token.type == "table_open":
            depth += 1
        elif token.type == "table_close":
            depth -= 1
            if depth == 0:
                break
        elif token.type == "tbody_open":
            in_tbody = True
        elif token.type == "tbody_close":
            in_tbody = False
        elif token.type == "tr_open" and in_tbody and token.map is not None:
            raw_row = lines[token.map[0]].text
            rows.append(BlockRow(hash=table_row_hash(raw_row)))
        index += 1
    return tuple(rows)


def table_row_hash(raw_row: str) -> str:
    """Return the canonical hash used by persisted table-row entries."""

    key = table_row_semantic_key(raw_row, separator=False)
    return sha256(repr(key).encode()).hexdigest()


def line_offsets(lines: list[str]) -> list[int]:
    """Legacy LF-only helper; raw-body callers must use ``parse_markdown``."""

    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets

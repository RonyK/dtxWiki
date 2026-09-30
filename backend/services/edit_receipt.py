"""What one context edit changed, in a form an agent and a reviewer can use.

A successful patch used to answer with the whole page and nothing else: the
agent could not tell which blocks got new ids or hashes without calling
read_structure_map again, and the Chats timeline could show no more than "the
call succeeded". The receipt is computed once from the before/after bodies
the write already has in hand. It names positions, sections and sizes; its
only content is two short previews around the first change, which is text the
caller itself just wrote or replaced.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from difflib import SequenceMatcher

from backend.schemas.edit_receipt import (
    EditMode,
    EditReceipt,
    EditReceiptBlock,
    EditReceiptFirstChange,
    EditReceiptOperation,
)
from backend.services.text_matching import MATCH_POLICY
from backend.utils.block_map import compute_blocks
from backend.utils.markdown_outline import Outline, parse_outline

_ANY_NEWLINE = re.compile(r"\r\n|\r|\n")

PREVIEW_CHARS = 300
MAX_CHANGED_SECTIONS = 20
MAX_CHANGED_BLOCKS = 50
# before-lines x after-lines of the region that differs; ~40 ms of diffing.
MAX_DIFF_WORK = 250_000


def _line_starts(text: str) -> list[int]:
    # CRLF, bare LF and bare CR all end a line (a CR-only page counted as one
    # line put every operation at line 1).
    return [0, *(match.end() for match in _ANY_NEWLINE.finditer(text))]


def _line_breaks(text: str, start: int, end: int) -> int:
    """Line breaks in ``text[start:end]``: CRLF once, bare CR and LF each."""

    return (
        text.count("\n", start, end)
        + text.count("\r", start, end)
        - text.count("\r\n", start, end)
    )


def _line_of(starts: Sequence[int], offset: int) -> int:
    """1-indexed line containing ``offset``."""

    low, high = 0, len(starts) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if starts[middle] <= offset:
            low = middle
        else:
            high = middle - 1
    return low + 1


def _section_for(outline: Outline, offset: int) -> tuple[str, ...]:
    best: tuple[str, ...] = ()
    best_level = -1
    for section in outline.sections:
        end = max(section.subtree_char_end, section.char_start + 1)
        if section.char_start <= offset < end:
            if section.level > best_level:
                best, best_level = section.heading_path, section.level
    return best


def _first_ink(text: str, start: int, end: int) -> int:
    """Offset of the first non-whitespace character in text[start:end]."""

    segment = text[start:end]
    stripped = segment.lstrip()
    return start + (len(segment) - len(stripped)) if stripped else start


def _changed_spans(before: str, after: str) -> list[tuple[int, int, int, int]]:
    """Changed (before_start, before_end, after_start, after_end) char spans.

    Common prefix and suffix are trimmed first, so an edit to one paragraph of
    a large page diffs only that region instead of the whole document.
    """

    # C-speed scans: a 1 MB body must not cost a Python-level step per
    # character on every save.
    prefix = len(os.path.commonprefix([before, after]))
    # Back up to a line start so the diff works on whole lines.
    prefix = before.rfind("\n", 0, prefix) + 1
    suffix = len(os.path.commonprefix([before[prefix:][::-1], after[prefix:][::-1]]))
    before_mid = before[prefix : len(before) - suffix]
    after_mid = after[prefix : len(after) - suffix]
    if before_mid == after_mid:
        return []
    before_lines = before_mid.splitlines(keepends=True)
    after_lines = after_mid.splitlines(keepends=True)
    if len(before_lines) * len(after_lines) > MAX_DIFF_WORK:
        # A line diff is quadratic in the worst case (97 s measured on a
        # 900 KB full rewrite). Past this bound report the changed region as
        # one span: the sizes stay exact, only the per-hunk split is lost.
        return [(prefix, prefix + len(before_mid), prefix, prefix + len(after_mid))]
    before_offsets = [0]
    for line in before_lines:
        before_offsets.append(before_offsets[-1] + len(line))
    after_offsets = [0]
    for line in after_lines:
        after_offsets.append(after_offsets[-1] + len(line))
    spans: list[tuple[int, int, int, int]] = []
    matcher = SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    for tag, b1, b2, a1, a2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        spans.append(
            (
                prefix + before_offsets[b1],
                prefix + before_offsets[b2],
                prefix + after_offsets[a1],
                prefix + after_offsets[a2],
            )
        )
    return spans


def build_edit_receipt(
    *,
    edit_mode: EditMode,
    before_body: str | None,
    after_body: str,
    editing_revision_before: int | None,
    editing_revision_after: int | None,
    operations: list[EditReceiptOperation] | None = None,
    before_block_map: Sequence[dict] | None = None,
    after_block_map: Sequence[dict] | None = None,
) -> EditReceipt:
    before = before_body or ""
    spans = _changed_spans(before, after_body)
    after_outline = parse_outline(after_body)
    before_outline = parse_outline(before) if before else None
    after_starts = _line_starts(after_body)

    chars_added = sum(a2 - a1 for _b1, _b2, a1, a2 in spans)
    chars_removed = sum(b2 - b1 for b1, b2, _a1, _a2 in spans)
    # Counting only "\n" reported no lines at all for a CR-only page.
    lines_added = sum(_line_breaks(after_body, a1, a2) for _b1, _b2, a1, a2 in spans)
    lines_removed = sum(_line_breaks(before, b1, b2) for b1, b2, _a1, _a2 in spans)

    sections: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for b1, b2, a1, a2 in spans:
        if a2 > a1:
            path = _section_for(after_outline, _first_ink(after_body, a1, a2))
        elif before_outline is not None:
            path = _section_for(before_outline, _first_ink(before, b1, b2))
        else:
            path = ()
        if path not in seen:
            seen.add(path)
            sections.append(list(path))
        if len(sections) >= MAX_CHANGED_SECTIONS:
            break

    first_change = None
    if spans and edit_mode != "create":
        b1, b2, a1, a2 = spans[0]
        # Start at the first changed character that is not a blank line: an
        # inserted section begins with the blank line that separates it, and
        # pointing at that line would name the section above it.
        after_start = _first_ink(after_body, a1, a2)
        before_start = _first_ink(before, b1, b2)
        first_change = EditReceiptFirstChange(
            line=_line_of(after_starts, after_start),
            before_preview=before[before_start : before_start + PREVIEW_CHARS],
            after_preview=after_body[after_start : after_start + PREVIEW_CHARS],
        )

    changed_blocks: list[EditReceiptBlock] = []
    if after_block_map is not None:
        previous = {
            (entry.get("id"), entry.get("hash")) for entry in before_block_map or ()
        }
        for entry in after_block_map:
            if (entry.get("id"), entry.get("hash")) in previous:
                continue
            changed_blocks.append(
                EditReceiptBlock(
                    id=str(entry.get("id")),
                    kind=str(entry.get("kind")),
                    hash=str(entry.get("hash")),
                )
            )
            if len(changed_blocks) >= MAX_CHANGED_BLOCKS:
                break

    return EditReceipt(
        edit_mode=edit_mode,
        editing_revision_before=editing_revision_before,
        editing_revision_after=editing_revision_after,
        chars_added=chars_added,
        chars_removed=chars_removed,
        lines_added=lines_added,
        lines_removed=lines_removed,
        changed_sections=sections,
        first_change=first_change,
        operations=list(operations or []),
        changed_blocks=changed_blocks,
    )


def patch_operation_receipts(
    *,
    body: str,
    operations: Sequence[object],
    edits: Sequence[object],
    block_map: Sequence[dict] | None,
) -> list[EditReceiptOperation]:
    """Per-operation positions for a resolved patch batch (before-body lines)."""

    starts = _line_starts(body)
    outline = parse_outline(body)
    block_lines: list[tuple[str, int, int]] = []
    if block_map is not None:
        blocks = compute_blocks(body)
        if len(blocks) == len(block_map):
            # 1-indexed inclusive lines, aligned with the (valid) map's ids.
            block_lines = [
                (
                    str(entry.get("id")),
                    block.line_start + 1,
                    max(block.line_start + 1, block.line_end),
                )
                for entry, block in zip(block_map, blocks, strict=True)
            ]
    receipts: list[EditReceiptOperation] = []
    for index, (operation, edit) in enumerate(zip(operations, edits, strict=True)):
        spans = (
            tuple(getattr(edit, "changed_ranges", ()) or ())
            if getattr(edit, "match_replacement", None) is not None
            else ()
        )
        if not spans:
            spans = ((edit.char_start, edit.char_end),)
        first = min(start for start, _end in spans)
        last = max(end for _start, end in spans)
        first_line = _line_of(starts, first)
        last_line = _line_of(starts, max(first, last - 1))
        if getattr(edit, "match_replacement", None) is not None:
            removed = sum(end - start for start, end in spans)
            per_span = getattr(edit, "match_replacements", None)
            added = (
                sum(len(text) for text in per_span)
                if per_span is not None
                else len(edit.match_replacement) * len(spans)
            )
        else:
            removed = edit.char_end - edit.char_start
            added = len(edit.replacement)
        heading = getattr(edit, "resolved_heading_path", None)
        affected = [
            block_id
            for block_id, start_line, end_line in block_lines
            if start_line <= last_line and first_line <= max(start_line, end_line)
        ]
        receipts.append(
            EditReceiptOperation(
                index=index,
                op=str(getattr(operation, "op", "unknown")),
                line_range=(first_line, last_line),
                heading_path=(
                    list(heading) if heading else list(_section_for(outline, first))
                ),
                affected_block_ids=affected[:MAX_CHANGED_BLOCKS],
                chars_added=0 if getattr(edit, "no_op", False) else added,
                chars_removed=0 if getattr(edit, "no_op", False) else removed,
                match_kind=getattr(edit, "match_kind", None),
                match_similarity=getattr(edit, "match_similarity", None),
                match_policy=(
                    MATCH_POLICY
                    if getattr(edit, "match_kind", None) is not None
                    else None
                ),
                matched_sha256=getattr(edit, "matched_sha256", None),
            )
        )
    return receipts

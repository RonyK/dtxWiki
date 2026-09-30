"""Stable block identity and explicit edit-operation transitions.

Content hashes describe what a block contains; they are not provenance.  A
block-addressed operation carries provenance explicitly and is applied as a
validated sequence transition after the final Markdown body is normalized.
Whole-document writes use only conservative, bounded matching.  Ambiguous
content never inherits an arbitrary old ID.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from secrets import token_hex
from typing import Literal, cast

from backend.utils.block_map import (
    BlockContent,
    blocks_from_persisted_map,
    compute_blocks,
    serialize_blocks,
)
from backend.utils.content_hash import content_hash
from backend.utils.markdown_parser import RawLine, parse_markdown, split_raw_lines

BlockOperationKind = Literal[
    "replace_block",
    "insert_after",
    "insert_before",
    "insert_document",
    "insert_row",
    "delete_block",
]

_BLOCK_OPERATION_KINDS = {
    "replace_block",
    "insert_after",
    "insert_before",
    "insert_document",
    "insert_row",
    "delete_block",
}
_SIMILARITY_THRESHOLD = 0.6
_SIMILARITY_MARGIN = 0.05
_FUZZY_POSITION_WINDOW = 12
_MAX_SIMILARITY_CHARS = 2048


class BlockMapTransitionError(ValueError):
    """The final body does not implement the declared block transition."""


class BlockReflowError(BlockMapTransitionError):
    """An edit changed how a block it did not target parses.

    Distinct from an unsupported transition: the operation itself was valid,
    but its content merged into, split, or re-kinded a neighbouring block
    (lazy paragraph continuation, an unclosed fence swallowing the rest of the
    page). The caller can fix this by isolating the content, so it must not be
    reported as "remove the operation".
    """

    def __init__(
        self,
        message: str,
        *,
        block_index: int | None = None,
        block_id: str | None = None,
        before_kind: str | None = None,
        after_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.block_index = block_index
        self.block_id = block_id
        self.before_kind = before_kind
        self.after_kind = after_kind


@dataclass(frozen=True)
class BlockOperationProvenance:
    """Durable identity effect of one resolved block-addressed operation.

    ``inserted_count`` is the explicit structural delta: top-level blocks for
    ``insert_before``/``insert_after``/``insert_document`` and data rows for
    ``insert_row``. Replace always produces exactly one top-level block; delete
    produces none.
    """

    operation: BlockOperationKind
    target_id: str | None
    inserted_count: int = 0

    def __post_init__(self) -> None:
        if self.operation not in _BLOCK_OPERATION_KINDS:
            raise ValueError(f"unsupported block operation: {self.operation}")
        if self.operation == "insert_document" and self.target_id is not None:
            raise ValueError("insert_document does not accept target_id")
        if self.operation != "insert_document" and not self.target_id:
            raise ValueError("block operation target_id must not be empty")
        if self.inserted_count < 0:
            raise ValueError("inserted_count must not be negative")
        if self.operation in {
            "insert_after",
            "insert_before",
            "insert_document",
            "insert_row",
        }:
            if self.inserted_count < 1:
                raise ValueError(f"{self.operation} must insert at least one item")
        elif self.inserted_count != 0:
            raise ValueError(f"{self.operation} does not accept inserted_count")

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "operation": self.operation,
            "target_id": self.target_id,
            "inserted_count": self.inserted_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BlockOperationProvenance:
        operation = value.get("operation")
        target_id = value.get("target_id")
        inserted_count = value.get("inserted_count", 0)
        if not isinstance(operation, str) or operation not in _BLOCK_OPERATION_KINDS:
            raise ValueError("invalid block operation provenance operation")
        if target_id is not None and not isinstance(target_id, str):
            raise ValueError("invalid block operation provenance target_id")
        if not isinstance(inserted_count, int) or isinstance(inserted_count, bool):
            raise ValueError("invalid block operation provenance inserted_count")
        return cls(
            operation=cast(BlockOperationKind, operation),
            target_id=target_id,
            inserted_count=inserted_count,
        )


@dataclass(frozen=True)
class BlockMapEdit:
    """One already-resolved block edit in the previous body's coordinates.

    This is the small, parser-independent hand-off from ``context_editing`` to
    the block-map writer.  Carrying the exact replacement lets the writer
    prove that the candidate body differs only at the declared operations
    before it re-parses touched blocks and their immediate boundaries.
    """

    char_start: int
    char_end: int
    replacement: str
    operation: BlockOperationProvenance
    no_op: bool = False

    def __post_init__(self) -> None:
        if self.char_start < 0 or self.char_end < self.char_start:
            raise ValueError("block-map edit range is invalid")


def serialize_operation_provenance(
    operations: Sequence[BlockOperationProvenance],
) -> list[dict[str, str | int | None]]:
    return [operation.to_dict() for operation in operations]


def deserialize_operation_provenance(
    values: Sequence[Mapping[str, object]],
) -> tuple[BlockOperationProvenance, ...]:
    return tuple(BlockOperationProvenance.from_dict(value) for value in values)


def new_block_id() -> str:
    """Return a cryptographically random 128-bit block ID."""

    return f"b_{token_hex(16)}"


def new_row_id() -> str:
    """Return a cryptographically random 128-bit table-row ID."""

    return f"r_{token_hex(16)}"


def locate_block(
    block_map: Sequence[Mapping[str, object]], block_id: str
) -> int | None:
    """Return a unique block position, failing closed on duplicates."""

    matches = [
        index for index, entry in enumerate(block_map) if entry.get("id") == block_id
    ]
    return matches[0] if len(matches) == 1 else None


def locate_row(
    block_map: Sequence[Mapping[str, object]], row_id: str
) -> tuple[int, int] | None:
    """Return ``(parent table position, row position)`` for a unique row ID."""

    matches: list[tuple[int, int]] = []
    for block_index, entry in enumerate(block_map):
        rows = entry.get("rows")
        if not isinstance(rows, list):
            continue
        for row_index, row in enumerate(rows):
            if isinstance(row, Mapping) and row.get("id") == row_id:
                matches.append((block_index, row_index))
    return matches[0] if len(matches) == 1 else None


def block_map_for_version(stored: list[dict] | None, body_md: str) -> list[dict]:
    """Return a stored map or a deterministic lazy map for a legacy row."""

    if stored is not None:
        return stored
    return compute_block_map_with_ids(
        body_md, previous_body_md=None, previous_block_map=None
    )


def validated_block_map_for_version(
    stored: list[dict] | None,
    body_md: str,
) -> list[dict]:
    """Return a map whose complete derived content matches ``body_md``.

    Unlike ``block_map_for_version`` this is a fail-closed boundary for durable
    manifests and reconciliation: a present-but-stale map is never accepted.
    Legacy rows with no map are still computed lazily because there is no
    contradictory durable claim to reject.
    """

    if stored is None:
        return compute_block_map_with_ids(
            body_md,
            previous_body_md=None,
            previous_block_map=None,
        )
    blocks = compute_blocks(body_md)
    if not _valid_previous_map(stored, blocks, require_cache_fields=True):
        raise BlockMapTransitionError("stored block map does not match its body")
    return stored


def heal_block_map(
    body_md: str,
    stored: list[dict] | None,
    *,
    identity_seed: str | None,
) -> tuple[list[dict], bool]:
    """Return a map that describes ``body_md``, and whether it had to be rebuilt.

    Rows written before every in-place writer kept the map in step can carry a
    map for an older body. ``block_map_for_version`` hands such a map out
    verbatim, so a reader paired stale ids/kinds with current line spans and
    the next patch was refused. This rebuilds only what drifted: a block whose
    kind and hash still match a stored entry (in order) keeps that entry's id;
    everything else gets an id minted from ``identity_seed`` and the stored
    map, so a reader and a later patch of the same row agree on every id
    without anything being persisted in between.

    Callers decide whether the rebuilt map may be persisted. Only the author's
    own mutable ``editing`` row should be; shared rows are healed in memory.
    """

    if not isinstance(stored, list):
        return (
            compute_block_map_with_ids(
                body_md,
                previous_body_md=None,
                previous_block_map=None,
                identity_seed=identity_seed,
            ),
            False,
        )
    blocks = compute_blocks(body_md)
    if _valid_previous_map(stored, blocks, require_cache_fields=True):
        return stored, False

    healed = serialize_blocks(blocks)
    well_formed = [entry for entry in stored if isinstance(entry, Mapping)]
    mint = _DeterministicIdMint(
        new_body_md=body_md,
        previous_block_map=well_formed,
        identity_seed=identity_seed,
    )
    # Linear, not a sequence diff: a sequence matcher over thousands of keys
    # with many identical blocks (repeated headings, blank tables) went
    # quadratic -- 19 s on a 900 KB rewrite. A (kind, hash) key present once
    # on each side carries its id; a group of identical blocks carries ids in
    # order only when both sides hold the same number of them; anything else
    # is ambiguous and gets a fresh id, the same conservatism _match_blocks
    # applies to whole-document writes.
    old_by_key: dict[tuple[object, object], list[int]] = defaultdict(list)
    new_by_key: dict[tuple[object, object], list[int]] = defaultdict(list)
    for index, entry in enumerate(well_formed):
        old_by_key[(entry.get("kind"), entry.get("hash"))].append(index)
    for index, entry in enumerate(healed):
        new_by_key[(entry.get("kind"), entry.get("hash"))].append(index)
    carried: dict[int, Mapping[str, object]] = {}
    claimed: set[str] = set()
    for key, new_indexes in new_by_key.items():
        old_indexes = old_by_key.get(key, [])
        if len(old_indexes) != len(new_indexes):
            continue
        for new_index, old_index in zip(new_indexes, old_indexes, strict=True):
            old_entry = well_formed[old_index]
            block_id = old_entry.get("id")
            if isinstance(block_id, str) and block_id and block_id not in claimed:
                carried[new_index] = old_entry
                claimed.add(block_id)

    for index, entry in enumerate(healed):
        old_entry = carried.get(index)
        if old_entry is None:
            _assign_fresh(entry, mint, f"heal:{index}")
            continue
        entry["id"] = old_entry["id"]
        rows = entry.get("rows")
        if not rows:
            continue
        old_rows = old_entry.get("rows")
        old_row_ids = [
            row.get("id") if isinstance(row, Mapping) else None
            for row in (old_rows if isinstance(old_rows, list) else [])
        ]
        if (
            len(old_row_ids) == len(rows)
            and all(
                isinstance(row_id, str) and row_id and row_id not in claimed
                for row_id in old_row_ids
            )
            and len(set(old_row_ids)) == len(old_row_ids)
        ):
            for row, row_id in zip(rows, old_row_ids, strict=True):
                row["id"] = row_id
                claimed.add(cast(str, row_id))
        else:
            for row_index, row in enumerate(rows):
                row["id"] = mint.mint("r", f"heal:{index}:row:{row_index}")
    return healed, True


def block_map_delta(before: list[dict], after: list[dict]) -> list[dict]:
    """Return the deterministic identity/hash delta between two block maps.

    The receipt deliberately excludes positional cache fields (``gap`` and
    ``lines``): those can change after formatting without changing a block's
    identity or content. Table rows share the same identity namespace and are
    reported with their owning block id so row-addressed writes are not hidden.
    """

    old_order, old_nodes = _receipt_nodes(before)
    new_order, new_nodes = _receipt_nodes(after)
    delta: list[dict] = []
    for node_id in new_order:
        new = new_nodes[node_id]
        old = old_nodes.get(node_id)
        if old is None:
            delta.append({"change": "added", "old": None, "new": new})
        elif old != new:
            delta.append({"change": "replaced", "old": old, "new": new})
    for node_id in old_order:
        if node_id not in new_nodes:
            delta.append({"change": "removed", "old": old_nodes[node_id], "new": None})
    return delta


def _receipt_nodes(block_map: list[dict]) -> tuple[list[str], dict[str, dict]]:
    order: list[str] = []
    nodes: dict[str, dict] = {}
    for entry in block_map:
        if not isinstance(entry, Mapping):
            raise BlockMapTransitionError("block map contains invalid receipt entry")
        block_id = entry.get("id")
        block_hash = entry.get("hash")
        kind = entry.get("kind")
        if not all(
            isinstance(value, str) and value for value in (block_id, block_hash, kind)
        ):
            raise BlockMapTransitionError("block map contains invalid receipt identity")
        if block_id in nodes:
            raise BlockMapTransitionError(
                "block map contains duplicate receipt identity"
            )
        order.append(block_id)
        nodes[block_id] = {
            "id": block_id,
            "hash": block_hash,
            "node_type": "block",
            "kind": kind,
            "parent_block_id": None,
        }
        for row in entry.get("rows") or ():
            if not isinstance(row, Mapping):
                raise BlockMapTransitionError("block map contains invalid row receipt")
            row_id = row.get("id")
            row_hash = row.get("hash")
            if not all(
                isinstance(value, str) and value for value in (row_id, row_hash)
            ):
                raise BlockMapTransitionError("block map contains invalid row receipt")
            if row_id in nodes:
                raise BlockMapTransitionError(
                    "block map contains duplicate receipt identity"
                )
            order.append(row_id)
            nodes[row_id] = {
                "id": row_id,
                "hash": row_hash,
                "node_type": "row",
                "kind": None,
                "parent_block_id": block_id,
            }
    return order, nodes


def compute_block_map_with_ids(
    new_body_md: str,
    *,
    previous_body_md: str | None,
    previous_block_map: list[dict] | None,
    operation_provenance: Sequence[BlockOperationProvenance | Mapping[str, object]]
    | None = None,
    identity_seed: str | None = None,
    explicit_replacements: dict[str, str] | None = None,
) -> list[dict]:
    """Compute an ID-bearing map for ``new_body_md``.

    ``operation_provenance`` is authoritative and validated against the final
    parsed body.  ``identity_seed`` is a stable request identity supplied by
    the write service, making dry-run and commit mint the same new IDs while
    different request identities mint different IDs.  ``explicit_replacements``
    remains a compatibility bridge for existing callers, but its derived
    hashes are intentionally ignored: the named replace target, not a post-hoc
    hash search, is the provenance fact.
    """

    new_blocks = compute_blocks(new_body_md)
    serialized = serialize_blocks(new_blocks)
    operations = _normalize_operations(operation_provenance)
    if explicit_replacements:
        explicit_targets = {operation.target_id for operation in operations}
        operations += tuple(
            BlockOperationProvenance("replace_block", target_id)
            for target_id in explicit_replacements
            if target_id not in explicit_targets
        )

    mint = _DeterministicIdMint(
        new_body_md=new_body_md,
        previous_block_map=previous_block_map,
        identity_seed=identity_seed,
    )
    if previous_body_md is None or previous_block_map is None:
        if operations:
            raise BlockMapTransitionError(
                "operation provenance requires a valid previous block map"
            )
        _assign_all_fresh(serialized, mint)
        return serialized

    old_blocks = compute_blocks(previous_body_md)
    if not _valid_previous_map(previous_block_map, old_blocks):
        if operations:
            raise BlockMapTransitionError(
                "operation provenance cannot be applied to a stale block map"
            )
        _assign_all_fresh(serialized, mint)
        return serialized

    if operations:
        return _apply_operation_transition(
            serialized,
            previous_block_map,
            operations,
            mint,
        )

    if new_body_md == previous_body_md:
        _carry_all_positionally(serialized, previous_block_map)
        return serialized

    matches = _match_blocks(
        new_body_md,
        new_blocks,
        previous_body_md,
        old_blocks,
    )
    for new_index, entry in enumerate(serialized):
        old_index = matches.get(new_index)
        if old_index is None:
            _assign_fresh(entry, mint, f"block:{new_index}")
            continue
        old_entry = previous_block_map[old_index]
        entry["id"] = old_entry["id"]
        if "rows" in entry:
            if new_blocks[new_index].hash == old_blocks[old_index].hash:
                _carry_rows_positionally(entry, old_entry)
            else:
                _carry_rows_conservatively(
                    entry,
                    old_entry,
                    mint,
                    f"block:{new_index}",
                )
    return serialized


def try_compute_incremental_block_map_with_ids(
    new_body_md: str,
    *,
    previous_body_md: str,
    previous_block_map: list[dict],
    edits: Sequence[BlockMapEdit],
    identity_seed: str | None = None,
    validated_previous_blocks: tuple[BlockContent, ...] | None = None,
) -> list[dict] | None:
    """Build the next map by parsing only touched blocks plus one boundary.

    ``None`` means that the persisted position cache cannot prove the fast
    path (legacy/malformed cache or a whole-document transition), so the
    caller must use ``compute_block_map_with_ids``.  Once the cache and exact
    edit replay are proven, structural disagreement raises instead of silently
    falling back: that is an invalid declared transition, not merely a slow
    legacy row.
    """

    if not edits or any(
        edit.operation.operation == "insert_document" for edit in edits
    ):
        return None
    previous_blocks = validated_previous_blocks
    if previous_blocks is None:
        previous_blocks = blocks_from_persisted_map(
            previous_body_md,
            previous_block_map,
        )
        if previous_blocks is None or not _valid_previous_map(
            previous_block_map,
            previous_blocks,
        ):
            return None

    active_edits = sorted(
        (edit for edit in edits if not edit.no_op),
        key=lambda edit: (edit.char_start, edit.char_end),
    )
    _validate_edit_replay(previous_body_md, new_body_md, active_edits)

    affected_indexes: set[int] = set()
    mutable_indexes: set[int] = set()
    for edit in edits:
        operation = edit.operation
        target_id = operation.target_id
        if target_id is None:
            return None
        if operation.operation == "insert_row":
            row_location = locate_row(previous_block_map, target_id)
            if row_location is None:
                raise BlockMapTransitionError(
                    "incremental row target did not resolve uniquely"
                )
            block_index, _row_index = row_location
            affected_indexes.add(block_index)
            mutable_indexes.add(block_index)
            continue
        block_index = locate_block(previous_block_map, target_id)
        if block_index is None:
            raise BlockMapTransitionError(
                "incremental block target did not resolve uniquely"
            )
        affected_indexes.add(block_index)
        if operation.operation == "replace_block":
            mutable_indexes.add(block_index)

    windows = _affected_windows(affected_indexes, len(previous_blocks))
    if not windows:
        return None

    previous_lines = split_raw_lines(previous_body_md)
    new_lines = split_raw_lines(new_body_md)
    new_line_by_offset = {
        line.char_start: index for index, line in enumerate(new_lines)
    }
    rebuilt: list[BlockContent] = []
    old_cursor = 0
    new_line_cursor = 0

    def append_carried(old_index: int) -> None:
        nonlocal new_line_cursor
        old = previous_blocks[old_index]
        line_start = new_line_cursor + old.gap
        line_end = line_start + old.lines
        if line_end > len(new_lines):
            raise BlockMapTransitionError(
                "incremental carried block exceeds the final body"
            )
        rebuilt.append(
            BlockContent(
                kind=old.kind,
                hash=old.hash,
                lines=old.lines,
                gap=old.gap,
                line_start=line_start,
                line_end=line_end,
                sub=old.sub,
                rows=old.rows,
            )
        )
        new_line_cursor = line_end

    for window_start, window_end in windows:
        while old_cursor < window_start:
            append_carried(old_cursor)
            old_cursor += 1

        base_char_start = _line_char_offset(
            previous_lines,
            previous_blocks[window_start].line_start,
            previous_body_md,
        )
        base_char_end = _line_char_offset(
            previous_lines,
            previous_blocks[window_end - 1].line_end,
            previous_body_md,
        )
        final_char_start = (
            0 if window_start == 0 else _map_base_offset(base_char_start, active_edits)
        )
        final_char_end = (
            len(new_body_md)
            if window_end == len(previous_blocks)
            else _map_base_offset(base_char_end, active_edits)
        )
        start_line = new_line_by_offset.get(final_char_start)
        if start_line is None or final_char_end < final_char_start:
            raise BlockMapTransitionError(
                "incremental parse window is not aligned to final lines"
            )
        fragment = new_body_md[final_char_start:final_char_end]
        prefix_lines = 0 if final_char_start == 0 else 1
        parsed_fragment = compute_blocks(
            fragment if prefix_lines == 0 else "\n" + fragment
        )
        for local in parsed_fragment:
            if local.line_start < prefix_lines:
                raise BlockMapTransitionError(
                    "incremental parser consumed its isolation boundary"
                )
            line_start = start_line + local.line_start - prefix_lines
            line_end = start_line + local.line_end - prefix_lines
            if line_start < new_line_cursor or line_end > len(new_lines):
                raise BlockMapTransitionError(
                    "incremental parser escaped its declared window"
                )
            rebuilt.append(
                BlockContent(
                    kind=local.kind,
                    hash=local.hash,
                    lines=local.lines,
                    gap=line_start - new_line_cursor,
                    line_start=line_start,
                    line_end=line_end,
                    # Heading subtree folds are document-contextual and are
                    # refreshed after all local windows have been assembled.
                    sub=local.sub,
                    rows=local.rows,
                )
            )
            new_line_cursor = line_end
        old_cursor = window_end

    while old_cursor < len(previous_blocks):
        append_carried(old_cursor)
        old_cursor += 1
    if any(line.text.strip() for line in new_lines[new_line_cursor:]):
        raise BlockMapTransitionError(
            "incremental map left unparsed non-whitespace content"
        )

    serialized = serialize_blocks(tuple(rebuilt))
    operations = tuple(edit.operation for edit in edits)
    mint = _DeterministicIdMint(
        new_body_md=new_body_md,
        previous_block_map=previous_block_map,
        identity_seed=identity_seed,
    )
    transitioned = _apply_operation_transition(
        serialized,
        previous_block_map,
        operations,
        mint,
    )
    _validate_incremental_untouched_blocks(
        previous_body_md=previous_body_md,
        previous_lines=previous_lines,
        previous_blocks=previous_blocks,
        new_body_md=new_body_md,
        new_lines=new_lines,
        rebuilt=tuple(rebuilt),
        transitioned=transitioned,
        previous_block_map=previous_block_map,
        mutable_indexes=mutable_indexes,
    )
    _refresh_heading_subtree_hashes(
        new_body_md,
        new_lines,
        tuple(rebuilt),
        transitioned,
    )
    return transitioned


def _validate_edit_replay(
    previous_body_md: str,
    new_body_md: str,
    edits: Sequence[BlockMapEdit],
) -> None:
    parts: list[str] = []
    cursor = 0
    for edit in edits:
        if edit.char_start < cursor or edit.char_end > len(previous_body_md):
            raise BlockMapTransitionError(
                "incremental edits overlap or exceed the previous body"
            )
        parts.append(previous_body_md[cursor : edit.char_start])
        parts.append(edit.replacement)
        cursor = edit.char_end
    parts.append(previous_body_md[cursor:])
    if "".join(parts) != new_body_md:
        raise BlockMapTransitionError(
            "final body contains changes outside the declared block edits"
        )


def _affected_windows(
    affected_indexes: set[int],
    block_count: int,
) -> tuple[tuple[int, int], ...]:
    windows: list[list[int]] = []
    for index in sorted(affected_indexes):
        start = max(0, index - 1)
        end = min(block_count, index + 2)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    return tuple((start, end) for start, end in windows)


def _map_base_offset(offset: int, edits: Sequence[BlockMapEdit]) -> int:
    delta = 0
    for edit in edits:
        if edit.char_end <= offset:
            delta += len(edit.replacement) - (edit.char_end - edit.char_start)
            continue
        if edit.char_start < offset < edit.char_end:
            raise BlockMapTransitionError(
                "incremental parse boundary falls inside an edit"
            )
        break
    return offset + delta


def _line_char_offset(
    lines: Sequence[RawLine],
    line_index: int,
    body_md: str,
) -> int:
    return lines[line_index].char_start if line_index < len(lines) else len(body_md)


def _raw_block_text(
    body_md: str,
    lines: Sequence[RawLine],
    block: BlockContent,
) -> str:
    return body_md[
        _line_char_offset(lines, block.line_start, body_md) : _line_char_offset(
            lines,
            block.line_end,
            body_md,
        )
    ]


def same_block_bytes(before_text: str, after_text: str, *, ends_document: bool) -> bool:
    """Byte identity for a block a batch did not target.

    The one allowed difference: the last block of a body with no trailing
    newline gains one when something is inserted after it. That is the line
    terminator the insertion needs, not a change to the block.
    """

    if before_text == after_text:
        return True
    return (
        ends_document
        and not before_text.endswith(("\r", "\n"))
        and after_text.rstrip("\r\n") == before_text
        and after_text[len(before_text) :] in ("\n", "\r\n", "\r")
    )


def _validate_incremental_untouched_blocks(
    *,
    previous_body_md: str,
    previous_lines: Sequence[RawLine],
    previous_blocks: tuple[BlockContent, ...],
    new_body_md: str,
    new_lines: Sequence[RawLine],
    rebuilt: tuple[BlockContent, ...],
    transitioned: list[dict],
    previous_block_map: list[dict],
    mutable_indexes: set[int],
) -> None:
    old_index_by_id = {
        cast(str, entry["id"]): index for index, entry in enumerate(previous_block_map)
    }
    for new_index, entry in enumerate(transitioned):
        old_index = old_index_by_id.get(cast(str, entry["id"]))
        if old_index is None or old_index in mutable_indexes:
            continue
        before = previous_blocks[old_index]
        after = rebuilt[new_index]
        if (
            before.kind != after.kind
            or before.hash != after.hash
            or before.lines != after.lines
            or before.rows != after.rows
            or not same_block_bytes(
                _raw_block_text(previous_body_md, previous_lines, before),
                _raw_block_text(new_body_md, new_lines, after),
                ends_document=before.line_end >= len(previous_lines),
            )
        ):
            raise BlockMapTransitionError(
                "a block outside the declared transition changed"
            )


def _refresh_heading_subtree_hashes(
    body_md: str,
    lines: Sequence[RawLine],
    blocks: tuple[BlockContent, ...],
    entries: list[dict],
) -> None:
    open_headings: list[tuple[int, int]] = []
    subtree_end_lines = [len(lines)] * len(blocks)
    for index, block in enumerate(blocks):
        if not (block.kind.startswith("h") and block.kind[1:].isdigit()):
            continue
        level = int(block.kind[1:])
        while open_headings and open_headings[-1][1] >= level:
            open_index, _open_level = open_headings.pop()
            subtree_end_lines[open_index] = block.line_start
        open_headings.append((index, level))
    for index, block in enumerate(blocks):
        if not (block.kind.startswith("h") and block.kind[1:].isdigit()):
            continue
        start = _line_char_offset(lines, block.line_start, body_md)
        end_line = subtree_end_lines[index]
        end = _line_char_offset(lines, end_line, body_md)
        entries[index]["sub"] = content_hash(body_md[start:end])


def _normalize_operations(
    operations: Sequence[BlockOperationProvenance | Mapping[str, object]] | None,
) -> tuple[BlockOperationProvenance, ...]:
    normalized: list[BlockOperationProvenance] = []
    for operation in operations or ():
        if isinstance(operation, BlockOperationProvenance):
            normalized.append(operation)
        elif isinstance(operation, Mapping):
            normalized.append(BlockOperationProvenance.from_dict(operation))
        else:
            raise ValueError("invalid block operation provenance")
    return tuple(normalized)


class _DeterministicIdMint:
    def __init__(
        self,
        *,
        new_body_md: str,
        previous_block_map: Sequence[Mapping[str, object]] | None,
        identity_seed: str | None,
    ) -> None:
        previous_json = json.dumps(
            previous_block_map or (),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._seed = sha256(
            (identity_seed or "").encode("utf-8")
            + b"\0"
            + new_body_md.encode("utf-8")
            + b"\0"
            + previous_json.encode("utf-8")
        ).hexdigest()
        self._used = _all_ids(previous_block_map or ())

    def mint(self, prefix: Literal["b", "r"], scope: str) -> str:
        attempt = 0
        while True:
            digest = sha256(
                f"{self._seed}\0{prefix}\0{scope}\0{attempt}".encode()
            ).hexdigest()
            candidate = f"{prefix}_{digest}"
            if candidate not in self._used:
                self._used.add(candidate)
                return candidate
            attempt += 1


def _all_ids(block_map: Sequence[Mapping[str, object]]) -> set[str]:
    ids: set[str] = set()
    for entry in block_map:
        block_id = entry.get("id")
        if isinstance(block_id, str):
            ids.add(block_id)
        rows = entry.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, Mapping) and isinstance(row.get("id"), str):
                ids.add(cast(str, row["id"]))
    return ids


def _valid_previous_map(
    previous_block_map: list[dict],
    old_blocks: tuple[BlockContent, ...],
    *,
    require_cache_fields: bool = False,
) -> bool:
    if len(previous_block_map) != len(old_blocks):
        return False
    seen: set[str] = set()
    expected = serialize_blocks(old_blocks)
    for stored, content in zip(previous_block_map, expected, strict=True):
        block_id = stored.get("id")
        if not isinstance(block_id, str) or not block_id or block_id in seen:
            return False
        seen.add(block_id)
        ignored_fields = {"id"}
        if not require_cache_fields:
            ignored_fields.update({"lines", "gap"})
        stored_content = {
            key: value for key, value in stored.items() if key not in ignored_fields
        }
        content = {
            key: value for key, value in content.items() if key not in ignored_fields
        }
        if "rows" in stored:
            stored_content["rows"] = [
                {"hash": row.get("hash")} for row in stored.get("rows") or ()
            ]
        if stored_content != content:
            return False
        for row in stored.get("rows") or ():
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id or row_id in seen:
                return False
            seen.add(row_id)
    return True


def _assign_all_fresh(entries: list[dict], mint: _DeterministicIdMint) -> None:
    for index, entry in enumerate(entries):
        _assign_fresh(entry, mint, f"block:{index}")


def _assign_fresh(entry: dict, mint: _DeterministicIdMint, scope: str) -> None:
    entry["id"] = mint.mint("b", scope)
    for row_index, row in enumerate(entry.get("rows") or ()):
        row["id"] = mint.mint("r", f"{scope}:row:{row_index}")


def _carry_all_positionally(entries: list[dict], old_entries: list[dict]) -> None:
    for entry, old_entry in zip(entries, old_entries, strict=True):
        entry["id"] = old_entry["id"]
        if "rows" in entry:
            _carry_rows_positionally(entry, old_entry)


def _carry_rows_positionally(entry: dict, old_entry: dict) -> None:
    rows = entry.get("rows") or []
    old_rows = old_entry.get("rows") or []
    if len(rows) != len(old_rows):
        raise BlockMapTransitionError("table row count changed without provenance")
    for row, old_row in zip(rows, old_rows, strict=True):
        row["id"] = old_row["id"]


def _apply_operation_transition(
    entries: list[dict],
    old_entries: list[dict],
    operations: tuple[BlockOperationProvenance, ...],
    mint: _DeterministicIdMint,
) -> list[dict]:
    document_operations = [
        operation
        for operation in operations
        if operation.operation == "insert_document"
    ]
    if document_operations:
        if len(document_operations) != 1 or len(operations) != 1 or old_entries:
            raise BlockMapTransitionError(
                "insert_document is only valid as the sole edit of an empty map"
            )
        operation = document_operations[0]
        if len(entries) != operation.inserted_count:
            raise BlockMapTransitionError(
                "insert_document final block count did not match provenance"
            )
        for inserted_index, entry in enumerate(entries):
            _assign_fresh(entry, mint, f"document:{inserted_index}")
        return entries

    block_operations: dict[int, BlockOperationProvenance] = {}
    row_operations: dict[int, list[tuple[int, BlockOperationProvenance]]] = defaultdict(
        list
    )
    claimed_targets: set[str] = set()

    for operation in operations:
        target_id = operation.target_id
        if target_id is None:
            raise BlockMapTransitionError("block transition target was missing")
        if target_id in claimed_targets:
            raise BlockMapTransitionError("a block transition target was used twice")
        claimed_targets.add(target_id)
        if operation.operation == "insert_row":
            location = locate_row(old_entries, target_id)
            if location is None:
                raise BlockMapTransitionError(
                    "insert_row target did not resolve uniquely"
                )
            block_index, row_index = location
            if block_index in block_operations:
                raise BlockMapTransitionError(
                    "a table cannot have row and block transitions together"
                )
            row_operations[block_index].append((row_index, operation))
            continue
        block_index = locate_block(old_entries, target_id)
        if block_index is None:
            raise BlockMapTransitionError(
                "block operation target did not resolve uniquely"
            )
        if block_index in block_operations or block_index in row_operations:
            raise BlockMapTransitionError("a block has conflicting transitions")
        block_operations[block_index] = operation

    new_index = 0
    for old_index, old_entry in enumerate(old_entries):
        operation = block_operations.get(old_index)
        if operation is not None and operation.operation == "delete_block":
            continue
        if operation is not None and operation.operation == "insert_before":
            for inserted_index in range(operation.inserted_count):
                if new_index >= len(entries):
                    raise BlockMapTransitionError(
                        "insert_before produced fewer blocks than declared"
                    )
                _assign_fresh(
                    entries[new_index],
                    mint,
                    f"before:{old_index}:{inserted_index}",
                )
                new_index += 1
        if new_index >= len(entries):
            raise BlockReflowError(
                "final body ended before the declared transition",
                block_index=old_index,
                block_id=cast(str | None, old_entry.get("id")),
                before_kind=cast(str | None, old_entry.get("kind")),
            )

        entry = entries[new_index]
        if operation is None or operation.operation != "replace_block":
            if entry.get("kind") != old_entry.get("kind"):
                raise BlockReflowError(
                    "an untouched block changed structural kind after the edit",
                    block_index=old_index,
                    block_id=cast(str | None, old_entry.get("id")),
                    before_kind=cast(str | None, old_entry.get("kind")),
                    after_kind=cast(str | None, entry.get("kind")),
                )
        entry["id"] = old_entry["id"]

        if old_index in row_operations:
            if entry.get("kind") != "table":
                raise BlockMapTransitionError(
                    "insert_row no longer resolves to a table"
                )
            _apply_row_transition(
                entry,
                old_entry,
                row_operations[old_index],
                mint,
                f"block:{new_index}",
            )
        elif "rows" in entry or "rows" in old_entry:
            if operation is not None and operation.operation == "replace_block":
                _carry_rows_conservatively(
                    entry,
                    old_entry,
                    mint,
                    f"block:{new_index}",
                )
            else:
                _carry_rows_positionally(entry, old_entry)
        new_index += 1

        if operation is not None and operation.operation == "insert_after":
            for inserted_index in range(operation.inserted_count):
                if new_index >= len(entries):
                    raise BlockMapTransitionError(
                        "insert_after produced fewer blocks than declared"
                    )
                _assign_fresh(
                    entries[new_index],
                    mint,
                    f"after:{old_index}:{inserted_index}",
                )
                new_index += 1

    if new_index != len(entries):
        raise BlockReflowError(
            "final body contains undeclared top-level block changes",
            block_index=len(old_entries),
        )
    return entries


def _apply_row_transition(
    entry: dict,
    old_entry: dict,
    operations: list[tuple[int, BlockOperationProvenance]],
    mint: _DeterministicIdMint,
    scope: str,
) -> None:
    rows = entry.get("rows") or []
    old_rows = old_entry.get("rows") or []
    by_row: dict[int, BlockOperationProvenance] = {}
    for row_index, operation in operations:
        if row_index in by_row:
            raise BlockMapTransitionError("a row has conflicting transitions")
        by_row[row_index] = operation

    new_row_index = 0
    for old_row_index, old_row in enumerate(old_rows):
        if new_row_index >= len(rows):
            raise BlockMapTransitionError("final table ended before its declared rows")
        rows[new_row_index]["id"] = old_row["id"]
        new_row_index += 1
        operation = by_row.get(old_row_index)
        if operation is None:
            continue
        for inserted_index in range(operation.inserted_count):
            if new_row_index >= len(rows):
                raise BlockMapTransitionError(
                    "insert_row produced fewer rows than declared"
                )
            rows[new_row_index]["id"] = mint.mint(
                "r", f"{scope}:after-row:{old_row_index}:{inserted_index}"
            )
            new_row_index += 1
    if new_row_index != len(rows):
        raise BlockMapTransitionError("final table contains undeclared row changes")


def _carry_rows_conservatively(
    entry: dict,
    old_entry: dict,
    mint: _DeterministicIdMint,
    scope: str,
) -> None:
    rows = entry.get("rows") or []
    old_rows = old_entry.get("rows") or []
    old_by_hash: dict[str, list[int]] = defaultdict(list)
    new_by_hash: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(old_rows):
        old_by_hash[row["hash"]].append(index)
    for index, row in enumerate(rows):
        new_by_hash[row["hash"]].append(index)

    matched: dict[int, int] = {}
    for row_hash, new_indexes in new_by_hash.items():
        old_indexes = old_by_hash.get(row_hash, [])
        if len(new_indexes) == len(old_indexes) == 1:
            matched[new_indexes[0]] = old_indexes[0]
    for index, row in enumerate(rows):
        old_index = matched.get(index)
        if old_index is None:
            row["id"] = mint.mint("r", f"{scope}:row:{index}")
        else:
            row["id"] = old_rows[old_index]["id"]


def _match_blocks(
    new_body_md: str,
    new_blocks: tuple[BlockContent, ...],
    old_body_md: str,
    old_blocks: tuple[BlockContent, ...],
) -> dict[int, int]:
    """Conservative O(N * fixed-window) whole-document matching."""

    matches: dict[int, int] = {}
    claimed_old: set[int] = set()
    old_by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    new_by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, block in enumerate(old_blocks):
        old_by_key[(block.kind, block.hash)].append(index)
    for index, block in enumerate(new_blocks):
        new_by_key[(block.kind, block.hash)].append(index)

    # Exact duplicate groups are ambiguous.  Only one-to-one content keys carry.
    for key, new_indexes in new_by_key.items():
        old_indexes = old_by_key.get(key, [])
        if len(new_indexes) == len(old_indexes) == 1:
            matches[new_indexes[0]] = old_indexes[0]
            claimed_old.add(old_indexes[0])

    ambiguous_new = {
        index
        for indexes in new_by_key.values()
        if len(indexes) > 1
        for index in indexes
    }
    ambiguous_old = {
        index
        for indexes in old_by_key.values()
        if len(indexes) > 1
        for index in indexes
    }

    new_texts = _block_texts(new_body_md, new_blocks)
    old_texts = _block_texts(old_body_md, old_blocks)
    scores: dict[tuple[int, int], float] = {}
    for new_index, new_block in enumerate(new_blocks):
        if new_index in matches or new_index in ambiguous_new:
            continue
        lower = max(0, new_index - _FUZZY_POSITION_WINDOW)
        upper = min(len(old_blocks), new_index + _FUZZY_POSITION_WINDOW + 1)
        for old_index in range(lower, upper):
            if (
                old_index in claimed_old
                or old_index in ambiguous_old
                or old_blocks[old_index].kind != new_block.kind
            ):
                continue
            scores[(new_index, old_index)] = SequenceMatcher(
                None,
                new_texts[new_index],
                old_texts[old_index],
                autojunk=False,
            ).ratio()

    best_for_new = _unique_best(scores, key_position=0)
    best_for_old = _unique_best(scores, key_position=1)
    for new_index, (old_index, ratio) in best_for_new.items():
        reverse = best_for_old.get(old_index)
        if (
            ratio >= _SIMILARITY_THRESHOLD
            and reverse is not None
            and reverse[0] == new_index
            and old_index not in claimed_old
        ):
            matches[new_index] = old_index
            claimed_old.add(old_index)
    _pin_identical_blocks_to_neighbours(matches, claimed_old, new_blocks, old_blocks)
    return matches


# Bounds the neighbour propagation below; a pass that pins nothing ends it.
_NEIGHBOUR_PASSES = 3


def _pin_identical_blocks_to_neighbours(
    matches: dict[int, int],
    claimed_old: set[int],
    new_blocks: tuple[BlockContent, ...],
    old_blocks: tuple[BlockContent, ...],
) -> None:
    """Carry ids of identical blocks that their matched neighbours pin down.

    A group of identical blocks (a label repeated under every item, such as
    "Acceptance criteria:") is ambiguous by content alone, so it used to get
    fresh ids -- and since every in-place write recomputes the map, those ids
    changed on every edit anywhere, and an id read before the edit stopped
    resolving. Position relative to a matched neighbour is evidence: new block
    j follows new block j-1, old block i follows old block i-1, j-1 matched
    i-1, and j and i have identical content, so j is i. (Symmetrically for a
    matched successor.) Pairing a group by ORDER would not be safe: after two
    sections swap, it hands one section's label id to the other's, and a
    block-addressed edit would land in the wrong section.

    One neighbour is not always enough evidence. After the groups around a
    repeated label are reordered, its predecessor and its successor point at
    two different old labels, and trusting either handed the label of one
    group to another: a stale id then edited the wrong section. So a block is
    pinned only when every matched neighbour proposes the same old block, a
    single proposing neighbour must itself sit where it sat before (next to
    its own matched neighbour, or at the same document edge), and evidence
    never crosses a heading: a heading opens the next section, so it cannot
    vouch for the block before it, nor its predecessor for it. Anything less
    leaves the block a fresh id, which a stale caller sees as block_not_found
    instead of an edit somewhere else.
    """

    def key(block: BlockContent) -> tuple[str, str]:
        return (block.kind, block.hash)

    def is_heading(block: BlockContent) -> bool:
        return block.kind.startswith("h") and block.kind[1:].isdigit()

    last_new = len(new_blocks) - 1
    last_old = len(old_blocks) - 1

    def settled(new_index: int) -> bool:
        """Whether a matched block kept its place next to a neighbour."""

        old_index = matches[new_index]
        return (
            (new_index == 0 and old_index == 0)
            or (new_index == last_new and old_index == last_old)
            or matches.get(new_index - 1) == old_index - 1
            or matches.get(new_index + 1) == old_index + 1
        )

    def proposal(new_index: int) -> int | None:
        """The old block every matched same-section neighbour points at."""

        proposals: list[tuple[int, int]] = []
        # The predecessor is in the same section unless this block is a
        # heading; the successor unless the successor is one.
        predecessor = new_index - 1
        if predecessor in matches and not is_heading(new_blocks[new_index]):
            proposals.append((predecessor, matches[predecessor] + 1))
        successor = new_index + 1
        if successor in matches and not is_heading(new_blocks[successor]):
            proposals.append((successor, matches[successor] - 1))
        if not proposals or len({old for _neighbour, old in proposals}) != 1:
            return None
        if len(proposals) == 1 and not settled(proposals[0][0]):
            return None
        return proposals[0][1]

    for _ in range(_NEIGHBOUR_PASSES):
        pinned = False
        for order in (
            range(len(new_blocks)),
            range(len(new_blocks) - 1, -1, -1),
        ):
            for new_index in order:
                if new_index in matches:
                    continue
                old_index = proposal(new_index)
                if (
                    old_index is not None
                    and 0 <= old_index < len(old_blocks)
                    and old_index not in claimed_old
                    and key(old_blocks[old_index]) == key(new_blocks[new_index])
                ):
                    matches[new_index] = old_index
                    claimed_old.add(old_index)
                    pinned = True
        if not pinned:
            return


def _unique_best(
    scores: Mapping[tuple[int, int], float], *, key_position: Literal[0, 1]
) -> dict[int, tuple[int, float]]:
    grouped: dict[int, list[tuple[float, int]]] = defaultdict(list)
    other_position = 1 - key_position
    for pair, ratio in scores.items():
        grouped[pair[key_position]].append((ratio, pair[other_position]))

    result: dict[int, tuple[int, float]] = {}
    for key, candidates in grouped.items():
        candidates.sort(reverse=True)
        best_ratio, best_other = candidates[0]
        if len(candidates) > 1 and best_ratio - candidates[1][0] < _SIMILARITY_MARGIN:
            continue
        result[key] = (best_other, best_ratio)
    return result


def _block_texts(body_md: str, blocks: tuple[BlockContent, ...]) -> tuple[str, ...]:
    parsed = parse_markdown(body_md)
    return tuple(
        body_md[
            parsed.char_offset(block.line_start) : parsed.char_offset(block.line_end)
        ][:_MAX_SIMILARITY_CHARS]
        for block in blocks
    )

"""patch_draft push-outcome decision.

`request.base` names the version a client's edit was computed against; the
page's current editable head may have moved since. This module decides what
a push against a moved head means, using section 5's 4-row (this module: 3,
since `base_not_found` is a lookup failure the caller handles directly)
decision table -- merge unit is the block (or table row), never the line:

  base == head                                -> committed (today's only path)
  base != head, every target id's stored content is
    unchanged between base and head           -> rebased (apply at head, parent=head)
  base != head, some target's content changed -> diverged (branch: apply at
                                                   base, parent=base)

`target_ids` is None when the request contains any operation this module
cannot address at block/row granularity (every operation type except
replace_block/insert_after/insert_row/delete_block -- see
`target_ids_for_operations`). Section 5's rebase/conflict proof requires
knowing exactly which ids an edit touches, so the write service rejects a
stale unaddressable request rather than inventing a positional conflict.

Per the user's decision (260829): a diverged push reuses this codebase's
existing "alternative draft" branch mechanism (`version_status="draft"`,
`graph_status="branched"`, projected as `alternative_draft` -- see
backend/services/context_lifecycle.py and page_service.py's
`_save_context_page_as_alternative`) rather than a new `version_status`
value. No migration, no second parallel branch concept for callers of
History/the review rail to learn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from backend.schemas.context_edit import (
    DeleteBlockOperation,
    EditOperation,
    InsertAfterOperation,
    InsertRowOperation,
    ReplaceBlockOperation,
)
from backend.utils.block_map import compute_blocks
from backend.utils.content_hash import content_hash, same_authored_markdown
from backend.utils.markdown_parser import parse_markdown

PushOutcome = Literal["committed", "rebased", "diverged"]

_RESOLUTION_INSTRUCTION = (
    "Set base to conflicts[*].head.version_id, set reconciles to the returned "
    "alternative_draft_id, and resubmit operations that explicitly resolve "
    "every target listed in conflicts. The server replays the branch's durable "
    "non-conflicting operations; do not resend or overwrite them."
)


@dataclass(frozen=True)
class PushDecision:
    outcome: PushOutcome
    # Every id (block or row) whose hash differs between base and head --
    # the "clean_changes" (rebased) or the candidate conflict set (diverged;
    # the actual conflicts are this set intersected with target_ids).
    changed_ids: frozenset[str]


@dataclass(frozen=True)
class ReconciliationTargetSets:
    """Server-proved state of the branch's active operation targets."""

    conflict_ids: frozenset[str]
    already_satisfied_ids: frozenset[str]


def target_ids_for_operations(operations: list[EditOperation]) -> frozenset[str] | None:
    """The block/row ids `operations` names, or None if any operation
    cannot be addressed at that granularity (see module docstring)."""
    ids: set[str] = set()
    for op in operations:
        if isinstance(
            op, (ReplaceBlockOperation, InsertAfterOperation, DeleteBlockOperation)
        ):
            ids.add(op.block)
        elif isinstance(op, InsertRowOperation):
            ids.add(op.row)
        else:
            return None
    return frozenset(ids)


def operation_target_id(operation: EditOperation) -> str | None:
    """Return the opaque block/row id named by one block-addressed op."""
    if isinstance(
        operation, (ReplaceBlockOperation, InsertAfterOperation, DeleteBlockOperation)
    ):
        return operation.block
    if isinstance(operation, InsertRowOperation):
        return operation.row
    return None


def hash_map(block_map: list[dict]) -> dict[str, str]:
    """id -> hash for every block AND every table row in one block map --
    the unified space `target_ids_for_operations` addresses into."""
    hashes: dict[str, str] = {}
    for entry in block_map:
        hashes[entry["id"]] = entry["hash"]
        for row in entry.get("rows") or ():
            hashes[row["id"]] = row["hash"]
    return hashes


def changed_ids_between_maps(
    base_block_map: list[dict],
    candidate_block_map: list[dict],
    *,
    base_body_md: str | None = None,
    candidate_body_md: str | None = None,
) -> frozenset[str]:
    """Supplement map deltas with authored text when both bodies are available.

    The persisted hash deliberately ignores trailing spaces. Authored text
    distinguishes Markdown hard breaks while retaining NFC/newline equivalence.
    """
    base_hashes = hash_map(base_block_map)
    candidate_hashes = hash_map(candidate_block_map)
    changed = {
        target_id
        for target_id in base_hashes.keys() | candidate_hashes.keys()
        if base_hashes.get(target_id) != candidate_hashes.get(target_id)
    }
    if (
        base_body_md is not None
        and candidate_body_md is not None
        and not same_authored_markdown(base_body_md, candidate_body_md)
    ):
        base_text = _text_by_id(base_body_md, base_block_map)
        candidate_text = _text_by_id(candidate_body_md, candidate_block_map)
        for target_id in base_hashes.keys() & candidate_hashes.keys():
            before, after = base_text.get(target_id), candidate_text.get(target_id)
            if (
                before is None
                or after is None
                or before[0] != after[0]
                or not same_authored_markdown(before[3], after[3])
            ):
                changed.add(target_id)
    return frozenset(changed)


def actual_target_ids_for_operations(
    operations: list[EditOperation],
    *,
    base_block_map: list[dict],
    candidate_block_map: list[dict],
    base_body_md: str | None = None,
    candidate_body_md: str | None = None,
) -> frozenset[str]:
    """Named merge targets whose authored operation survives normalization.

    Replacement/deletion targets must differ in the final stored content.
    Inserts preserve their anchor hash, so a surviving inserted block/row is
    the proof that the anchor operation changed the candidate. This prevents a
    raw or normalization-induced no-op from conflicting merely because it
    happened to name a block that somebody else changed at head.
    """
    delta_ids = changed_ids_between_maps(
        base_block_map,
        candidate_block_map,
        base_body_md=base_body_md,
        candidate_body_md=candidate_body_md,
    )
    base_ids = set(hash_map(base_block_map))
    actual: set[str] = set()
    for operation in operations:
        target_id = operation_target_id(operation)
        if target_id is None:
            continue
        if target_id in delta_ids:
            actual.add(target_id)
        elif isinstance(operation, InsertAfterOperation):
            target_index = next(
                (
                    index
                    for index, entry in enumerate(candidate_block_map)
                    if entry["id"] == target_id
                ),
                None,
            )
            if (
                target_index is not None
                and target_index + 1 < len(candidate_block_map)
                and candidate_block_map[target_index + 1]["id"] not in base_ids
            ):
                actual.add(target_id)
        elif isinstance(operation, InsertRowOperation):
            for entry in candidate_block_map:
                rows = entry.get("rows") or ()
                row_index = next(
                    (index for index, row in enumerate(rows) if row["id"] == target_id),
                    None,
                )
                if (
                    row_index is not None
                    and row_index + 1 < len(rows)
                    and rows[row_index + 1]["id"] not in base_ids
                ):
                    actual.add(target_id)
                if row_index is not None:
                    break
    return frozenset(actual)


def target_descriptor(block_map: list[dict], target_id: str) -> dict | None:
    """Describe one block/row id without inferring identity from its text."""
    for block_index, entry in enumerate(block_map):
        if entry["id"] == target_id:
            return {
                "target_id": target_id,
                "kind": entry["kind"],
                "parent_block_id": None,
                "ordinal": block_index,
            }
        for row_index, row in enumerate(entry.get("rows") or ()):
            if row["id"] == target_id:
                return {
                    "target_id": target_id,
                    "kind": "table_row",
                    "parent_block_id": entry["id"],
                    "ordinal": row_index,
                }
    return None


def resolve_target_id_between_maps(
    source_block_map: list[dict],
    target_block_map: list[dict],
    source_target_id: str,
) -> str | None:
    """Resolve a durable target id into a later map, conservatively.

    A surviving opaque id is authoritative. Table-row ids may legitimately
    churn when their row changes; in that one nested case the stable parent
    table id plus the row ordinal is the address contract. Top-level block
    positions are never treated as identity: repeated paragraphs make that
    inference unsafe, so a retired block id fails closed.
    """
    target_hashes = hash_map(target_block_map)
    if source_target_id in target_hashes:
        return source_target_id

    source = target_descriptor(source_block_map, source_target_id)
    if source is None or source["kind"] != "table_row":
        return None
    parent_id = source["parent_block_id"]
    row_index = source["ordinal"]
    parent = next(
        (entry for entry in target_block_map if entry["id"] == parent_id),
        None,
    )
    if parent is None:
        return None
    source_parent = next(
        (entry for entry in source_block_map if entry["id"] == parent_id),
        None,
    )
    source_rows = source_parent.get("rows") or () if source_parent else ()
    target_rows = parent.get("rows") or ()
    if len(source_rows) != len(target_rows) or row_index >= len(target_rows):
        return None
    # Ordinal is only a safe bridge when exactly this row changed in place.
    # A deletion followed by a left-shift must not turn the next logical row
    # into the conflict target, nor may a reorder be treated as identity.
    if any(
        source_row["id"] != target_rows[index]["id"]
        for index, source_row in enumerate(source_rows)
        if index != row_index
    ):
        return None
    return target_rows[row_index]["id"]


def rebase_operations_onto_map(
    *,
    operations: list[EditOperation],
    source_block_map: list[dict],
    target_block_map: list[dict],
) -> tuple[list[EditOperation], frozenset[str]]:
    """Carry original block/row operations to an unchanged later target.

    The operation kind and authored content are preserved. Only the opaque
    address (for a row-id churn) and CAS hash are rewritten. A target whose
    content changed or disappeared is returned in ``stale_ids`` and is never
    silently skipped or converted to a whole-block replacement.
    """
    source_hashes = hash_map(source_block_map)
    target_hashes = hash_map(target_block_map)
    rebased: list[EditOperation] = []
    stale_ids: set[str] = set()
    for operation in operations:
        source_id = operation_target_id(operation)
        if source_id is None:
            stale_ids.add("<unaddressable>")
            continue
        target_id = resolve_target_id_between_maps(
            source_block_map, target_block_map, source_id
        )
        source_hash = source_hashes.get(source_id)
        target_hash = target_hashes.get(target_id) if target_id is not None else None
        if source_hash is None or target_hash != source_hash:
            stale_ids.add(source_id)
            continue
        updates: dict[str, str] = {"expected": target_hash}
        if isinstance(operation, InsertRowOperation):
            updates["row"] = target_id
        else:
            updates["block"] = target_id
        rebased.append(operation.model_copy(update=updates))
    return rebased, frozenset(stale_ids)


def decide_push_outcome(
    *,
    base_version_id: UUID,
    head_version_id: UUID,
    base_block_map: list[dict],
    head_block_map: list[dict],
    target_ids: frozenset[str] | None,
    base_body_md: str | None = None,
    head_body_md: str | None = None,
) -> PushDecision:
    if base_version_id == head_version_id:
        return PushDecision("committed", frozenset())

    changed_ids = changed_ids_between_maps(
        base_block_map,
        head_block_map,
        base_body_md=base_body_md,
        candidate_body_md=head_body_md,
    )

    if target_ids is None or (target_ids & changed_ids):
        return PushDecision("diverged", changed_ids)
    return PushDecision("rebased", changed_ids)


def _text_by_id(
    body_md: str, block_map: list[dict]
) -> dict[str, tuple[str, int, int, str]]:
    """id -> (kind, 1-indexed line_start, 1-indexed line_end, raw text) for
    every block AND row `body_md`/`block_map` together describe. Line span
    and text are computed fresh (never persisted -- section 2.1), the same
    way read_structure_map does; row entries reuse their parent table's
    span (a row has no line span of its own worth reporting separately)."""
    lookup: dict[str, tuple[str, int, int, str]] = {}
    fresh_blocks = compute_blocks(body_md)
    if len(fresh_blocks) != len(block_map):
        return lookup
    parsed = parse_markdown(body_md)
    for stored, fresh in zip(block_map, fresh_blocks, strict=True):
        line_start, line_end = fresh.line_start + 1, fresh.line_end + 1
        char_start = parsed.char_offset(fresh.line_start)
        last_line_index = max(fresh.line_start, fresh.line_end - 1)
        char_end = parsed.lines[last_line_index].content_char_end
        text = body_md[char_start:char_end]
        lookup[stored["id"]] = (stored["kind"], line_start, line_end, text)
        for row_index, row in enumerate(stored.get("rows") or ()):
            # Persisted table rows are exactly the tbody rows, in source
            # order. Report the row itself rather than duplicating the whole
            # parent table in every row-level conflict.
            row_line = fresh.line_start + 2 + row_index
            row_text = (
                parsed.lines[row_line].text if row_line < fresh.line_end else text
            )
            lookup[row["id"]] = (
                "table_row",
                row_line + 1 if row_line < fresh.line_end else line_start,
                row_line + 1 if row_line < fresh.line_end else line_end,
                row_text,
            )
    return lookup


def text_by_id(
    body_md: str, block_map: list[dict]
) -> dict[str, tuple[str, int, int, str]]:
    """Public server-owned text projection for reconciliation/read surfaces."""

    return _text_by_id(body_md, block_map)


def _yours_content_by_target(operations: list[EditOperation]) -> dict[str, str]:
    """target id -> the new content this request's own operations propose
    for it (only content-bearing block-addressed ops have one; delete_block
    has nothing to show as "yours")."""
    content: dict[str, str] = {}
    for op in operations:
        if isinstance(op, ReplaceBlockOperation | InsertAfterOperation):
            content[op.block] = op.content
        elif isinstance(op, InsertRowOperation):
            content[op.row] = op.content
    return content


def authored_sides_by_target(
    *,
    operations: list[EditOperation],
    base_block_map: list[dict],
    authored_body_md: str,
    authored_block_map: list[dict],
) -> dict[str, dict]:
    """Extract the normalized contribution each operation actually authored."""
    base_ids = set(hash_map(base_block_map))
    authored_hashes = hash_map(authored_block_map)
    authored_lookup = _text_by_id(authored_body_md, authored_block_map)
    sides: dict[str, dict] = {}
    for operation in operations:
        target_id = operation_target_id(operation)
        if target_id is None:
            continue
        authored_id: str | None = None
        if isinstance(operation, ReplaceBlockOperation):
            authored_id = target_id
        elif isinstance(operation, DeleteBlockOperation):
            sides[target_id] = {"hash": None, "text": None, "deleted": True}
            continue
        elif isinstance(operation, InsertAfterOperation):
            block_index = next(
                (
                    index
                    for index, block in enumerate(authored_block_map)
                    if block["id"] == target_id
                ),
                None,
            )
            if block_index is not None and block_index + 1 < len(authored_block_map):
                candidate_id = authored_block_map[block_index + 1]["id"]
                if candidate_id not in base_ids:
                    authored_id = candidate_id
        elif isinstance(operation, InsertRowOperation):
            for block in authored_block_map:
                rows = block.get("rows") or ()
                row_index = next(
                    (index for index, row in enumerate(rows) if row["id"] == target_id),
                    None,
                )
                if row_index is not None and row_index + 1 < len(rows):
                    candidate_id = rows[row_index + 1]["id"]
                    if candidate_id not in base_ids:
                        authored_id = candidate_id
                    break
        authored_entry = (
            authored_lookup.get(authored_id) if authored_id is not None else None
        )
        if authored_entry is None:
            continue
        sides[target_id] = {
            "hash": authored_hashes.get(authored_id),
            "text": authored_entry[3],
            "deleted": False,
        }
    return sides


def reconciliation_target_sets(
    *,
    operations: list[EditOperation],
    base_block_map: list[dict],
    authored_body_md: str,
    authored_block_map: list[dict],
    head_block_map: list[dict],
    base_body_md: str | None = None,
    head_body_md: str | None = None,
) -> ReconciliationTargetSets:
    """Classify current conflicts and safely converged branch targets.

    A replace is already satisfied only when the durable authored result and
    the current HEAD target have the same persisted block hash and authored
    text when supplied. A delete is already satisfied only when the target's
    old slot is provably empty.
    Insert operations remain conflicts when their anchor changed because
    content equality alone cannot prove insertion position or identity.
    """

    active_target_ids = target_ids_for_operations(operations)
    if active_target_ids is None:
        return ReconciliationTargetSets(frozenset(), frozenset())
    candidate_conflicts = set(
        changed_ids_between_maps(
            base_block_map,
            head_block_map,
            base_body_md=base_body_md,
            candidate_body_md=head_body_md,
        )
        & active_target_ids
    )
    for operation in operations:
        if isinstance(operation, InsertAfterOperation | InsertRowOperation) and (
            _insertion_gap_changed(
                operation=operation,
                base_block_map=base_block_map,
                head_block_map=head_block_map,
            )
        ):
            target_id = operation_target_id(operation)
            if target_id is not None:
                candidate_conflicts.add(target_id)
    authored_sides = authored_sides_by_target(
        operations=operations,
        base_block_map=base_block_map,
        authored_body_md=authored_body_md,
        authored_block_map=authored_block_map,
    )
    head_hashes = hash_map(head_block_map)
    head_text = (
        _text_by_id(head_body_md, head_block_map) if head_body_md is not None else None
    )
    already_satisfied: set[str] = set()
    for operation in operations:
        target_id = operation_target_id(operation)
        if target_id is None or target_id not in candidate_conflicts:
            continue
        if isinstance(operation, ReplaceBlockOperation):
            current_id = resolve_target_id_between_maps(
                base_block_map,
                head_block_map,
                target_id,
            )
            authored = authored_sides.get(target_id)
            authored_hash = authored.get("hash") if authored is not None else None
            authored_text = authored.get("text") if authored is not None else None
            if (
                current_id is not None
                and isinstance(authored_hash, str)
                and head_hashes.get(current_id) == authored_hash
                and (
                    head_text is None
                    or (
                        current_id in head_text
                        and isinstance(authored_text, str)
                        and same_authored_markdown(
                            head_text[current_id][3], authored_text
                        )
                    )
                )
            ):
                already_satisfied.add(target_id)
        elif isinstance(operation, DeleteBlockOperation) and (
            resolve_target_id_between_maps(
                base_block_map,
                authored_block_map,
                target_id,
            )
            is None
            and retired_deletion_state(
                base_block_map,
                authored_block_map,
                target_id,
            )
            is True
            and resolve_target_id_between_maps(
                base_block_map,
                head_block_map,
                target_id,
            )
            is None
            and retired_deletion_state(
                base_block_map,
                head_block_map,
                target_id,
            )
            is True
        ):
            already_satisfied.add(target_id)
    return ReconciliationTargetSets(
        conflict_ids=frozenset(candidate_conflicts - already_satisfied),
        already_satisfied_ids=frozenset(already_satisfied),
    )


def _insertion_gap_changed(
    *,
    operation: InsertAfterOperation | InsertRowOperation,
    base_block_map: list[dict],
    head_block_map: list[dict],
) -> bool:
    """Whether HEAD modified the exact gap named by an insertion operation."""

    source_id = operation_target_id(operation)
    if source_id is None:
        return True
    target_id = resolve_target_id_between_maps(
        base_block_map,
        head_block_map,
        source_id,
    )
    if target_id is None:
        return True
    if isinstance(operation, InsertAfterOperation):
        source = target_descriptor(base_block_map, source_id)
        target = target_descriptor(head_block_map, target_id)
        if (
            source is None
            or target is None
            or source["kind"] == "table_row"
            or target["kind"] == "table_row"
        ):
            return True
        return _ordered_gap_changed(
            source_ids=[entry["id"] for entry in base_block_map],
            target_ids=[entry["id"] for entry in head_block_map],
            source_anchor_id=source_id,
            target_anchor_id=target_id,
        )

    source = target_descriptor(base_block_map, source_id)
    target = target_descriptor(head_block_map, target_id)
    if (
        source is None
        or target is None
        or source["kind"] != "table_row"
        or target["kind"] != "table_row"
        or source["parent_block_id"] != target["parent_block_id"]
    ):
        return True
    source_parent = next(
        (entry for entry in base_block_map if entry["id"] == source["parent_block_id"]),
        None,
    )
    target_parent = next(
        (entry for entry in head_block_map if entry["id"] == target["parent_block_id"]),
        None,
    )
    if source_parent is None or target_parent is None:
        return True
    return _ordered_gap_changed(
        source_ids=[row["id"] for row in source_parent.get("rows") or ()],
        target_ids=[row["id"] for row in target_parent.get("rows") or ()],
        source_anchor_id=source_id,
        target_anchor_id=target_id,
    )


def _ordered_gap_changed(
    *,
    source_ids: list[str],
    target_ids: list[str],
    source_anchor_id: str,
    target_anchor_id: str,
) -> bool:
    try:
        source_index = source_ids.index(source_anchor_id)
        target_index = target_ids.index(target_anchor_id)
    except ValueError:
        return True
    target_positions = {target_id: index for index, target_id in enumerate(target_ids)}
    next_survivor = next(
        (
            source_id
            for source_id in source_ids[source_index + 1 :]
            if source_id in target_positions
        ),
        None,
    )
    if next_survivor is None:
        return target_index != len(target_ids) - 1
    return target_positions[next_survivor] != target_index + 1


def build_conflict_details(
    *,
    conflicting_ids: frozenset[str],
    base_body_md: str,
    base_block_map: list[dict],
    head_body_md: str,
    head_block_map: list[dict],
    head_version_id: UUID,
    head_actor_kind: str,
    head_actor_display_name: str | None,
    head_created_at: datetime,
    operations: list[EditOperation],
    authored_body_md: str | None = None,
    authored_block_map: list[dict] | None = None,
) -> list[dict]:
    """Section 6.1's `conflicts` entries: one per id this push's own targets
    collided with, structured fields alongside a git-style marker block (the
    format an LLM already knows how to resolve without extra explanation)."""
    base_lookup = _text_by_id(base_body_md, base_block_map)
    head_lookup = _text_by_id(head_body_md, head_block_map)
    authored_sides = (
        authored_sides_by_target(
            operations=operations,
            base_block_map=base_block_map,
            authored_body_md=authored_body_md,
            authored_block_map=authored_block_map,
        )
        if authored_body_md is not None and authored_block_map is not None
        else {
            target_id: {
                "hash": content_hash(text if text.endswith("\n") else text + "\n"),
                "text": text,
                "deleted": False,
            }
            for target_id, text in _yours_content_by_target(operations).items()
        }
    )
    base_hashes = hash_map(base_block_map)
    head_hashes = hash_map(head_block_map)

    entries: list[dict] = []
    for conflict_id in sorted(conflicting_ids):
        resolution_target_id = resolve_target_id_between_maps(
            base_block_map, head_block_map, conflict_id
        )
        head_id_for_hash = resolution_target_id
        head_entry = (
            head_lookup.get(head_id_for_hash) if head_id_for_hash is not None else None
        )
        base_entry = base_lookup.get(conflict_id)
        if base_entry is None:
            continue
        base_kind, base_line_start, base_line_end, base_text = base_entry
        if head_entry is None:
            kind, line_start, line_end, head_text = (
                base_kind,
                base_line_start,
                base_line_end,
                None,
            )
        else:
            kind, line_start, line_end, head_text = head_entry
        base_hash = base_hashes.get(conflict_id, "")
        head_hash = (
            head_hashes.get(head_id_for_hash) if head_id_for_hash is not None else None
        )
        yours_side = authored_sides.get(conflict_id)
        yours_text = yours_side.get("text") if yours_side is not None else None

        entry: dict = {
            "block_id": conflict_id,
            "kind": kind,
            "resolution_target_id": resolution_target_id,
            "line_span": [line_start, line_end],
            "base": {"hash": base_hash, "text": base_text},
            "head": {
                "hash": head_hash,
                "text": head_text,
                "actor_kind": head_actor_kind,
                "actor": head_actor_display_name,
                "at": head_created_at.isoformat(),
                "version_id": str(head_version_id),
                "deleted": (
                    retired_deletion_state(base_block_map, head_block_map, conflict_id)
                    if resolution_target_id is None
                    else False
                ),
                "identity_retired": resolution_target_id is None,
            },
        }
        if yours_side is not None:
            entry["yours"] = yours_side
            if head_text is not None and yours_text is not None:
                entry["markers"] = (
                    f"<<<<<<< yours (this conversation)\n{yours_text}\n"
                    f"=======\n{head_text}\n"
                    f">>>>>>> theirs (v{head_version_id}, {head_actor_kind} "
                    f"{head_actor_display_name or 'unknown'})"
                )
        entries.append(entry)
    return entries


def retired_deletion_state(
    base_block_map: list[dict], head_block_map: list[dict], target_id: str
) -> bool | None:
    """Prove deletion only when the retired top-level slot is empty at head.

    A single unknown block occupying that slot may be a rewritten target or an
    unrelated insertion, so it remains tri-state unknown. Opaque identity is
    never inferred from ordinal position.
    """
    base_ids = [entry["id"] for entry in base_block_map]
    try:
        base_index = base_ids.index(target_id)
    except ValueError:
        return None
    head_ids = [entry["id"] for entry in head_block_map]
    if not head_ids:
        return True
    head_positions = {entry_id: index for index, entry_id in enumerate(head_ids)}
    previous_id = next(
        (
            entry_id
            for entry_id in reversed(base_ids[:base_index])
            if entry_id in head_positions
        ),
        None,
    )
    next_id = next(
        (
            entry_id
            for entry_id in base_ids[base_index + 1 :]
            if entry_id in head_positions
        ),
        None,
    )
    if previous_id is not None and next_id is not None:
        previous_position = head_positions[previous_id]
        next_position = head_positions[next_id]
        return True if next_position == previous_position + 1 else None
    if base_index == 0 and next_id is not None:
        return True if head_positions[next_id] == 0 else None
    if base_index == len(base_ids) - 1 and previous_id is not None:
        return True if head_positions[previous_id] == len(head_ids) - 1 else None
    return None


def build_clean_changes(
    *,
    changed_ids: frozenset[str],
    conflicting_ids: frozenset[str],
    head_body_md: str,
    head_block_map: list[dict],
) -> list[dict]:
    """Section 6.1's `clean_changes`: every id that changed since base but
    did NOT conflict with this push's own targets -- already folded into
    head, informational only."""
    head_lookup = _text_by_id(head_body_md, head_block_map)
    entries: list[dict] = []
    for changed_id in sorted(changed_ids - conflicting_ids):
        head_entry = head_lookup.get(changed_id)
        if head_entry is None:
            entries.append(
                {
                    "block_id": changed_id,
                    "kind": "unknown",
                    "summary": None,
                    "deleted": None,
                    "identity_retired": True,
                }
            )
            continue
        kind, _line_start, _line_end, text = head_entry
        summary = text.strip().splitlines()[0] if text.strip() else ""
        entries.append(
            {
                "block_id": changed_id,
                "kind": kind,
                "summary": summary[:120],
                "deleted": False,
                "identity_retired": False,
            }
        )
    return entries


def resolution_instruction() -> str:
    return _RESOLUTION_INSTRUCTION

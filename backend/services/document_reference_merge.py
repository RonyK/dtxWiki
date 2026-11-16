"""Merge version-owned bibliography metadata alongside a stale body patch."""

from dataclasses import dataclass
from itertools import product
from uuid import NAMESPACE_URL, uuid5

from backend.context_versions.domain import VersionNode
from backend.services.document_references import (
    MAX_DOCUMENT_REFERENCES,
    authored_document_references,
    deleted_document_references,
)
from backend.services.read_errors import ReadValidationError


@dataclass(frozen=True)
class AuthoredReferenceMerge:
    rows: tuple
    changed: bool


def _snapshot(session, version):
    rows = {
        row.reference.key: row for row in authored_document_references(session, version)
    }
    for key, value in deleted_document_references(session, version).items():
        _tombstone, entry = value
        if entry.get(
            "metadata_present", entry.get("registration_origin") == "metadata"
        ):
            rows[key] = value
    return rows


def _notes(rows):
    return {
        key: row[1].get("note") if isinstance(row, tuple) else row.note
        for key, row in rows.items()
    }


def _conflict():
    # Referenced identities may have become unavailable to the caller. Keep
    # this conflict source-owned and disclose neither target IDs nor notes.
    return ReadValidationError(
        "Reference metadata changed on both versions. Update this draft's "
        "References before retrying the merge.",
        error_code="reconcile_reference_conflict",
    )


_MISSING = object()


def _base_note_options(session, source, head):
    """Merge every best base while retaining unresolved ancestral note values.

    This is the metadata virtual-base rule used by ContextMergeService: an
    ambiguous ancestor remains a set of possible values until the actual tips
    agree, rather than choosing one old note and silently dropping the other.
    """
    from backend.models.context import ContextPage
    from backend.repositories.workspace_scoped import get_in_workspace
    from backend.services.context_draft_workflow import history_for

    page = get_in_workspace(
        session,
        ContextPage,
        workspace_id=source.workspace_id,
        resource_id=source.page_id,
    )
    if page is None:
        raise _conflict()
    history, versions = history_for(session, page)
    options = {}
    bases_cache = {}

    def state(version_id):
        if version_id not in options:
            options[version_id] = {
                key: frozenset((note,))
                for key, note in _notes(
                    _snapshot(session, versions[version_id])
                ).items()
            }
        return options[version_id]

    def frame(ours_id, theirs_id):
        bases = history.merge_bases(ours_id, theirs_id)
        return [(ours_id, theirs_id), bases, bases[0] if bases else None, 1]

    stack = [frame(source.id, head.id)]
    while stack:
        key, bases, current_id, index = stack[-1]
        if index >= len(bases):
            bases_cache[key] = current_id
            stack.pop()
            continue
        other_id = bases[index]
        dependency = (current_id, other_id)
        if dependency not in bases_cache:
            stack.append(frame(*dependency))
            continue
        base_id = bases_cache[dependency]
        base = state(base_id) if base_id else {}
        ours, theirs = state(current_id), state(other_id)
        combined = {}
        for reference in dict.fromkeys((*base, *ours, *theirs)):
            old = base.get(reference, frozenset((_MISSING,)))
            mine = ours.get(reference, frozenset((_MISSING,)))
            other = theirs.get(reference, frozenset((_MISSING,)))
            possible = set()
            for left, right in product(mine, other):
                if left == right or old == {right}:
                    possible.add(left)
                elif old == {left}:
                    possible.add(right)
                else:
                    possible.update((left, right))
            combined[reference] = frozenset(possible)
        virtual_id = uuid5(
            NAMESPACE_URL, f"context-reference-base:{current_id}:{other_id}"
        )
        if virtual_id not in options:
            history = history.with_node(
                VersionNode(
                    source.workspace_id,
                    source.page_id,
                    virtual_id,
                    (current_id, other_id),
                )
            )
            options[virtual_id] = combined
        stack[-1][2] = virtual_id
        stack[-1][3] += 1
    base_id = bases_cache[(source.id, head.id)]
    return state(base_id) if base_id else {}


def merge_authored_document_references(session, *, source, head):
    """Preserve disjoint edits and reject competing changes to one document.

    Bibliography metadata uses the same page DAG as authored content. All best
    common bases contribute to the comparison, including merge parents and
    unresolved virtual-base notes. Inline references remain owned by the merged
    body; legacy evidence stays on the normal copy path.
    """
    if (source.workspace_id, source.page_id) != (head.workspace_id, head.page_id):
        raise _conflict()
    ours = _snapshot(session, source)
    current = _snapshot(session, head)
    our_notes, current_notes = _notes(ours), _notes(current)
    if our_notes == current_notes:
        return AuthoredReferenceMerge(tuple(current.values()), False)
    base_options = _base_note_options(session, source, head)
    missing = _MISSING
    merged = {}
    # Keep current numbering stable; append source-only registrations in
    # their authored order. The command API has no bibliography reorder edit.
    for key in dict.fromkeys((*current, *ours)):
        our_note = our_notes.get(key, missing)
        current_note = current_notes.get(key, missing)
        old = base_options.get(key, frozenset((missing,)))
        if our_note == current_note:
            selected = ours.get(key)
        elif old == {our_note}:
            selected = current.get(key)
        elif old == {current_note}:
            selected = ours.get(key)
        else:
            raise _conflict()
        if selected is not None:
            merged[key] = selected
    if len(merged) > MAX_DOCUMENT_REFERENCES:
        raise ReadValidationError(
            "The merged document exceeds the reference limit; update its "
            "References before retrying the merge.",
            error_code="reconcile_reference_conflict",
        )
    return AuthoredReferenceMerge(
        tuple(merged.values()), _notes(merged) != current_notes
    )

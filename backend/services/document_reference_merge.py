"""Merge version-owned bibliography metadata alongside a stale body patch."""

from dataclasses import dataclass

from backend.models.context import ContextPageVersion
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
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
        "Reference metadata changed on both versions. Reconcile the References "
        "on the current editing version before retrying the body merge.",
        error_code="reconcile_reference_conflict",
    )


def _common_ancestor(session, source, head):
    parents = dict(
        session.execute(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=source.workspace_id,
                conditions=(ContextPageVersion.page_id == source.page_id,),
            ).with_only_columns(
                ContextPageVersion.id, ContextPageVersion.parent_version_id
            )
        ).all()
    )

    def lineage(version_id):
        seen = set()
        while version_id is not None:
            if version_id in seen or version_id not in parents:
                raise _conflict()
            seen.add(version_id)
            yield version_id
            version_id = parents[version_id]

    source_history = set(lineage(source.id))
    ancestor_id = next(
        (version_id for version_id in lineage(head.id) if version_id in source_history),
        None,
    )
    if ancestor_id is None:
        raise _conflict()
    ancestor = get_in_workspace(
        session,
        ContextPageVersion,
        workspace_id=source.workspace_id,
        resource_id=ancestor_id,
    )
    if ancestor is None or ancestor.page_id != source.page_id:
        raise _conflict()
    return ancestor


def merge_authored_document_references(session, *, source, head):
    """Preserve disjoint edits and reject competing changes to one document.

    The source can descend from a private editing version with bibliography
    changes made before its body diverged. Its immediate parent is therefore
    not necessarily the common metadata base shared with the current head.
    Inline references remain owned by the merged body; legacy evidence stays
    on the normal copy path and is never rewritten here.
    """
    if (source.workspace_id, source.page_id) != (head.workspace_id, head.page_id):
        raise _conflict()
    ours = _snapshot(session, source)
    current = _snapshot(session, head)
    our_notes, current_notes = _notes(ours), _notes(current)
    if our_notes == current_notes:
        return AuthoredReferenceMerge(tuple(current.values()), False)
    base = _snapshot(session, _common_ancestor(session, source, head))
    base_notes = _notes(base)
    missing = object()
    merged = {}
    # Keep current numbering stable; append source-only registrations in
    # their authored order. The command API has no bibliography reorder edit.
    for key in dict.fromkeys((*current, *ours)):
        our_note = our_notes.get(key, missing)
        current_note = current_notes.get(key, missing)
        base_note = base_notes.get(key, missing)
        if our_note == base_note:
            selected = current.get(key)
        elif current_note == base_note or our_note == current_note:
            selected = ours.get(key)
        else:
            raise _conflict()
        if selected is not None:
            merged[key] = selected
    if len(merged) > MAX_DOCUMENT_REFERENCES:
        raise ReadValidationError(
            "The merged document exceeds the reference limit; reconcile its "
            "References before retrying the body merge.",
            error_code="reconcile_reference_conflict",
        )
    return AuthoredReferenceMerge(
        tuple(merged.values()), _notes(merged) != current_notes
    )

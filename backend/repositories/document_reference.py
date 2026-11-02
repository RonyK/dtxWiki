"""Copy authored bibliography without copying publication projections."""

from uuid import uuid4

from sqlalchemy import Text, cast, inspect

from backend.models.document_reference import ContextDocumentReference, target_columns
from backend.models.library import LibraryItemTombstone
from backend.repositories.workspace_scoped import select_in_workspace


def document_reference_schema_available(session) -> bool:
    """Versioned read capability for older migration/runtime guard schemas.

    A partial installation is an error, not an empty bibliography. The probe
    is scoped to the current Session transaction and cannot survive commit.
    """
    connection = session.connection()
    transaction = session.get_transaction()
    cached = session.info.get("document_reference_schema.v1")
    if cached is not None and cached[0] is transaction:
        return cached[1]
    inspector = inspect(connection)
    names = (
        "context_document_reference",
        "context_published_citation",
        "context_citation_publication",
    )
    present = [inspector.has_table(name) for name in names]
    if any(present) and not all(present):
        raise RuntimeError("The document reference schema is only partially installed")
    available = all(present)
    session.info["document_reference_schema.v1"] = transaction, available
    return available


def copy_document_references(session, *, source, target) -> None:
    if not document_reference_schema_available(session):
        return
    if source.workspace_id != target.workspace_id:
        raise ValueError("References can only be copied within their workspace")
    if source.id == target.id:
        return
    rows = session.scalars(
        select_in_workspace(
            ContextDocumentReference,
            workspace_id=source.workspace_id,
            conditions=(ContextDocumentReference.owner_version_id == source.id,),
        )
    ).all()
    for row in rows:
        session.add(
            ContextDocumentReference(
                workspace_id=target.workspace_id,
                owner_version_id=target.id,
                **target_columns(row.reference),
                note=row.note,
                sort_order=row.sort_order,
                created_by_actor_kind=row.created_by_actor_kind,
                created_by_actor_id=row.created_by_actor_id,
            )
        )

    # Deleted targets no longer have FK rows. Preserve only the selected
    # source version's reference snapshot as normal copy-on-edit metadata.
    tombstones = session.scalars(
        select_in_workspace(
            LibraryItemTombstone,
            workspace_id=source.workspace_id,
            conditions=(
                cast(LibraryItemTombstone.citations, Text).contains(str(source.id)),
            ),
        )
    )
    for tombstone in tombstones:
        items = tombstone.citations or []
        if any(
            value.get("target_id") == str(target.id)
            and value.get("citation_type") == "document_reference"
            for value in items
        ):
            continue
        candidates = [
            value
            for value in items
            if value.get("target_id") == str(source.id)
            and value.get("citation_type") == "document_reference"
        ]
        if not candidates:
            continue
        chosen = next(
            (
                value
                for value in candidates
                if value.get("registration_origin") == "published"
            ),
            candidates[0],
        )
        new_id = str(uuid4())
        tombstone.citations = [
            *items,
            {
                **chosen,
                "target_id": str(target.id),
                "reference_id": new_id,
                "citation_id": new_id,
                "registration_origin": "metadata",
            },
        ]

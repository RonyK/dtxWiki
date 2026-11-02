"""Read legacy Library citation endpoints as document identities.

The selectable is read-only and does not rewrite stored projections. Collapse
references to several versions/artifacts before graph visibility, counts and
budgets. A separate mapper avoids polluting a stored EdgeProjection object's
identity-map state with the normalized endpoints.
"""

from functools import lru_cache
from uuid import UUID

from sqlalchemy import and_, func, literal, or_, select, union_all
from sqlalchemy.orm import aliased, registry

from backend.models.context import EdgeProjection
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.repositories.workspace_scoped import (
    join_correlated_workspace_projection,
    join_in_workspace,
    select_in_workspace,
)
from backend.services.library_canonical import LEGACY_READY_PROCESSING_STATUSES


def _document_edges(workspace_id, source_ids=None, source_kinds=None):
    raw = EdgeProjection.__table__
    artifact = aliased(LibraryArtifact)
    version = aliased(LibraryItemVersion)
    artifact_version = aliased(LibraryItemVersion)
    item = aliased(LibraryItem)
    library_citation = and_(
        raw.c.edge_kind == "citation_source",
        raw.c.target_kind.in_(("library_item_version", "library_artifact")),
    )
    from sqlalchemy import case

    target_id = case(
        (raw.c.target_kind == "library_item", raw.c.target_id),
        (raw.c.target_kind == "library_item_version", version.library_item_id),
        else_=artifact_version.library_item_id,
    )
    columns = [
        literal("library_item").label(column.name)
        if column.name == "target_kind"
        else target_id.label(column.name)
        if column.name == "target_id"
        else column
        for column in raw.c
    ]
    statement = select(*columns).select_from(raw)
    for model, condition in (
        (
            version,
            and_(
                raw.c.target_kind == "library_item_version",
                version.id == raw.c.target_id,
            ),
        ),
        (
            artifact,
            and_(
                raw.c.target_kind == "library_artifact", artifact.id == raw.c.target_id
            ),
        ),
        (artifact_version, artifact_version.id == artifact.library_item_version_id),
        (item, item.id == target_id),
    ):
        statement = join_correlated_workspace_projection(
            statement,
            raw.c.workspace_id,
            model,
            on=condition,
        )
    referenced_version = func.coalesce(version.id, artifact_version.id)
    referenced_status = func.coalesce(
        version.processing_status, artifact_version.processing_status
    )
    canonical_version = or_(
        item.current_ready_version_id == referenced_version,
        and_(
            item.current_ready_version_id.is_(None),
            item.current_version_id == referenced_version,
            referenced_status.in_(sorted(LEGACY_READY_PROCESSING_STATUSES)),
        ),
    )
    eligible = or_(
        raw.c.target_kind == "library_item",
        and_(
            canonical_version,
            or_(
                raw.c.target_kind == "library_item_version",
                artifact.attempt_id == artifact_version.selected_conversion_attempt_id,
            ),
        ),
    )
    if source_ids is not None:
        statement = statement.where(raw.c.source_id.in_(source_ids))
    if source_kinds is not None:
        statement = statement.where(raw.c.source_kind.in_(source_kinds))
    normalized = statement.where(
        raw.c.workspace_id == workspace_id,
        library_citation,
        target_id.is_not(None),
        eligible,
    ).subquery("library_document_citations")
    identity = [
        normalized.c[name]
        for name in (
            "workspace_id",
            "source_kind",
            "source_id",
            "target_kind",
            "target_id",
            "edge_kind",
        )
    ]
    ranked = select(
        *normalized.c,
        func.row_number()
        .over(partition_by=identity, order_by=normalized.c.id)
        .label("document_ordinal"),
        func.sum(normalized.c.weight)
        .over(partition_by=identity)
        .label("document_weight"),
        func.max(normalized.c.occurred_at)
        .over(partition_by=identity)
        .label("document_occurred_at"),
    ).subquery("unique_library_document_citations")
    deduplicated = select(
        *[
            ranked.c.document_weight.label(column.name)
            if column.name == "weight"
            else ranked.c.document_occurred_at.label(column.name)
            if column.name == "occurred_at"
            else ranked.c[column.name]
            for column in raw.c
        ]
    ).where(ranked.c.document_ordinal == 1)
    ordinary = select(*raw.c).where(
        raw.c.workspace_id == workspace_id, ~library_citation
    )
    if source_ids is not None:
        ordinary = ordinary.where(raw.c.source_id.in_(source_ids))
    branches = (
        [ordinary.where(raw.c.source_kind == kind) for kind in source_kinds]
        if source_kinds is not None
        else [ordinary]
    )
    return union_all(*branches, deduplicated).subquery("document_edge_projection")


class DocumentEdgeProjection:
    """Read model with the same columns and separate ORM identity as storage."""


_mapper_registry = registry()
_selectable = _document_edges(UUID(int=0))
_mapper_registry.map_imperatively(
    DocumentEdgeProjection, _selectable, primary_key=[_selectable.c.id]
)


@lru_cache(maxsize=64)
def document_projection_for_workspace(workspace_id, source_ids=None, source_kinds=None):
    """Cache SQL structure only, with an explicit immutable workspace binding."""
    return aliased(
        DocumentEdgeProjection,
        _document_edges(workspace_id, source_ids, source_kinds),
        adapt_on_names=True,
    )


def select_document_edges_in_workspace(*, workspace_id, columns, predicates=()):
    """Scope a read-only document projection without classifying it as a table."""
    projection = document_projection_for_workspace(workspace_id)
    return select(*columns).where(projection.workspace_id == workspace_id, *predicates)


def document_citation_identities(session, workspace_id, identities):
    """Normalize legacy observation identities without rewriting evidence.

    This only maps bounded, already authorized endpoints to their document.
    Callers retain exact-version authorization and current projection admission;
    mapping an ID by itself never grants visibility or canonical eligibility.
    """
    identities = list(identities)
    targets = {
        (identity[2], identity[3])
        for identity in identities
        if identity[4] == "citation_source"
        and identity[2] in {"library_item_version", "library_artifact"}
    }
    documents = {}
    for kind in ("library_item_version", "library_artifact"):
        ids = [target_id for target_kind, target_id in targets if target_kind == kind]
        for start in range(0, len(ids), 400):
            batch = ids[start : start + 400]
            if kind == "library_item_version":
                statement = select_in_workspace(
                    LibraryItemVersion,
                    workspace_id=workspace_id,
                    conditions=(LibraryItemVersion.id.in_(batch),),
                ).with_only_columns(
                    LibraryItemVersion.id, LibraryItemVersion.library_item_id
                )
            else:
                statement = join_in_workspace(
                    select_in_workspace(
                        LibraryArtifact,
                        workspace_id=workspace_id,
                        conditions=(LibraryArtifact.id.in_(batch),),
                    ),
                    LibraryArtifact,
                    LibraryItemVersion,
                    workspace_id=workspace_id,
                    on=LibraryItemVersion.id == LibraryArtifact.library_item_version_id,
                ).with_only_columns(
                    LibraryArtifact.id, LibraryItemVersion.library_item_id
                )
            documents.update(
                {
                    (kind, target_id): item_id
                    for target_id, item_id in session.execute(statement)
                }
            )
    return {
        identity: (
            identity[0],
            identity[1],
            "library_item",
            documents[identity[2:4]],
            identity[4],
        )
        if identity[4] == "citation_source" and identity[2:4] in documents
        else identity
        for identity in identities
    }

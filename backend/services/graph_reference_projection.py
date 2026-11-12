"""Read legacy Library citation endpoints and document containers as documents.

The selectable is read-only and does not rewrite stored projections. Collapse
references to several versions/artifacts before graph visibility, counts and
budgets. A tree row whose parent placement is a document reads as that
document's relation, so TreeRail containment and links share one node. Which
placements may read that way is the reader's TreeRail visibility, supplied by
the caller. A separate mapper avoids polluting a stored EdgeProjection object's
identity-map state with the normalized endpoints.
"""

from uuid import UUID

from sqlalchemy import and_, cast, func, literal, or_, select, true, union_all
from sqlalchemy.orm import aliased, registry

from backend.models.context import EdgeProjection
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.repositories.workspace_scoped import (
    join_correlated_workspace_projection,
    join_in_workspace,
    select_in_workspace,
    select_projection_in_workspace,
)
from backend.services.library_canonical import LEGACY_READY_PROCESSING_STATUSES

# Tree placements that are documents. Their children belong to the document.
DOCUMENT_TREE_TARGET_KINDS = ("context_page", "library_item", "agent_page")


def _document_edges(
    workspace_id, visible_placement, source_ids=None, source_kinds=None
):
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
    container = aliased(WorkspaceTreeNode)
    document_container = and_(
        raw.c.edge_kind == "tree",
        raw.c.source_kind == "workspace_tree_node",
        container.id == raw.c.source_id,
        container.node_kind != "folder",
        container.target_kind.in_(DOCUMENT_TREE_TARGET_KINDS),
        container.target_id.is_not(None),
    )
    has_document_container = container.id.is_not(None)
    ordinary = join_correlated_workspace_projection(
        select(*raw.c).select_from(raw),
        raw.c.workspace_id,
        container,
        on=document_container,
    ).where(
        raw.c.workspace_id == workspace_id,
        ~library_citation,
        ~has_document_container,
    )
    if source_ids is not None:
        ordinary = ordinary.where(raw.c.source_id.in_(source_ids))
    branches = (
        [ordinary.where(raw.c.source_kind == kind) for kind in source_kinds]
        if source_kinds is not None
        else [ordinary]
    )
    # The document reads as the source. Several placements of one document
    # may hold the same child; like citations, they collapse to one relation.
    container_kind = cast(container.target_kind, raw.c.source_kind.type)
    container_id = container.target_id
    contained_identity = [
        raw.c.workspace_id,
        container_kind,
        container_id,
        raw.c.target_kind,
        raw.c.target_id,
        raw.c.edge_kind,
    ]
    contained_columns = [
        container_kind.label(column.name)
        if column.name == "source_kind"
        else container_id.label(column.name)
        if column.name == "source_id"
        else column
        for column in raw.c
    ]
    contained_ordinal = (
        func.row_number()
        .over(partition_by=contained_identity, order_by=raw.c.id)
        .label("document_ordinal")
    )
    placement_scope = [raw.c.workspace_id == workspace_id, has_document_container]
    if source_ids is not None:
        # Only these documents' placements, found through the source index,
        # so the placement check below reads their rows alone.
        placement_scope += [
            raw.c.source_kind == "workspace_tree_node",
            raw.c.source_id.in_(
                select_projection_in_workspace(
                    WorkspaceTreeNode,
                    workspace_id=workspace_id,
                    columns=(WorkspaceTreeNode.id,),
                    predicates=(WorkspaceTreeNode.target_id.in_(source_ids),),
                ).correlate(None)
            ),
        ]
    contained = join_correlated_workspace_projection(
        select(*contained_columns, contained_ordinal).select_from(raw),
        raw.c.workspace_id,
        container,
        on=document_container,
    ).where(
        *placement_scope,
        # The stored source is the placement; once it reads as the document,
        # no later check can see it. A placement the reader cannot see in
        # TreeRail must not contain anything, before placements collapse.
        visible_placement(raw.c.source_id),
    )
    if source_kinds is not None:
        contained = contained.where(container_kind.in_(source_kinds))
    contained = contained.subquery("document_container_trees")
    unique_contained = select(*[contained.c[column.name] for column in raw.c]).where(
        contained.c.document_ordinal == 1
    )
    return union_all(*branches, deduplicated, unique_contained).subquery(
        "document_edge_projection"
    )


class DocumentEdgeProjection:
    """Read model with the same columns and separate ORM identity as storage."""


_mapper_registry = registry()
# Column shape only; every query reads a reader's own projection below.
_selectable = _document_edges(UUID(int=0), lambda _placement_id: true())
_mapper_registry.map_imperatively(
    DocumentEdgeProjection, _selectable, primary_key=[_selectable.c.id]
)


def document_edge_projection(
    workspace_id, visible_placement, source_ids=None, source_kinds=None
):
    """Build the read model with an explicit immutable workspace binding.

    ``visible_placement`` maps a placement id column to the reader's TreeRail
    visibility for it. Graph visibility owns that rule and the caching.
    """
    return aliased(
        DocumentEdgeProjection,
        _document_edges(workspace_id, visible_placement, source_ids, source_kinds),
        adapt_on_names=True,
    )


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

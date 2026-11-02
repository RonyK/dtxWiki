"""Citation-owned edge extraction from stored publication and legacy evidence.

This adapter never parses or backfills historical bodies. Projection storage,
locking, observations and workspace revision changes belong to its callers.
"""

from __future__ import annotations

from functools import partial
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, aliased

from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.repositories.workspace_scoped import (
    join_in_workspace,
    select_in_workspace,
    where_in_workspace,
)
from backend.services.entity_ref import EntityKind
from backend.services.indexing.edge_projection import build_edge_row

_edge = partial(build_edge_row, provenance="curated")


def _citation_scope(statement, citing_page_id: UUID | None, workspace_id: UUID):
    """Restrict citations to one page's current published version.

    ``CitationInput`` stores claim ids for claim-scoped citations and version
    ids for whole/section citations. Legacy page/version target kinds remain
    readable, but draft and superseded-version evidence never projects into
    the viewer-visible graph.
    """
    if citing_page_id is None:
        return statement
    published_version_ids = select_in_workspace(
        ContextPage,
        workspace_id=workspace_id,
        conditions=(
            ContextPage.id == citing_page_id,
            ContextPage.current_published_version_id.is_not(None),
        ),
    ).with_only_columns(ContextPage.current_published_version_id)
    claim_ids = select_in_workspace(
        ContextClaim,
        workspace_id=workspace_id,
        conditions=(ContextClaim.page_version_id.in_(published_version_ids),),
    ).with_only_columns(ContextClaim.id)
    return where_in_workspace(
        statement,
        ContextCitation,
        workspace_id=workspace_id,
        conditions=(
            or_(
                and_(
                    ContextCitation.target_kind == "context_page",
                    ContextCitation.target_id == citing_page_id,
                ),
                and_(
                    ContextCitation.target_kind.in_(
                        (
                            "context_page_version",
                            "context_page_whole",
                            "context_page_section",
                        )
                    ),
                    ContextCitation.target_id.in_(published_version_ids),
                ),
                and_(
                    ContextCitation.target_kind == "context_claim",
                    ContextCitation.target_id.in_(claim_ids),
                ),
            ),
        ),
    )


def _aggregate_citation_edges(
    session: Session,
    workspace_id: UUID,
    edge_kind: str,
    per_citation: list[tuple[ContextCitation, EntityKind, UUID]],
) -> list[dict[str, object]]:
    """Collapse citation instances onto identity edges.

    A page can cite the same target more than once (two claims in one draft,
    or citations carried across revisions — ContextCitation has no
    uniqueness), and uq_edge_projection_identity would reject duplicate rows
    mid-publish. Weight = instance count, occurred_at = latest evidence.
    """
    citing_by_citation = _citing_page_map(
        session, [citation for citation, _kind, _id in per_citation], workspace_id
    )
    workspace_pages = _workspace_page_ids(
        session, set(citing_by_citation.values()), workspace_id
    )
    from backend.models.document_reference import ContextCitationPublication
    from backend.repositories.document_reference import (
        document_reference_schema_available,
    )

    materialized_pages = set()
    if document_reference_schema_available(session):
        registered_versions = select_in_workspace(
            ContextCitationPublication,
            workspace_id=workspace_id,
        ).with_only_columns(ContextCitationPublication.version_id)
        materialized_pages = set(
            session.scalars(
                select_in_workspace(
                    ContextPage,
                    workspace_id=workspace_id,
                    conditions=(
                        ContextPage.current_published_version_id.in_(
                            registered_versions
                        ),
                    ),
                ).with_only_columns(ContextPage.id)
            )
        )
    aggregated: dict[tuple[UUID, EntityKind, UUID], list] = {}
    for citation, target_kind, target_id in per_citation:
        citing_page_id = citing_by_citation.get(citation.id)
        if (
            citing_page_id is None
            or citing_page_id not in workspace_pages
            or citing_page_id in materialized_pages
        ):
            continue
        key = (citing_page_id, target_kind, target_id)
        entry = aggregated.get(key)
        if entry is None:
            aggregated[key] = [1, citation.created_at]
        else:
            entry[0] += 1
            if citation.created_at and (
                entry[1] is None or citation.created_at > entry[1]
            ):
                entry[1] = citation.created_at
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.CONTEXT_PAGE,
            source_id=citing_page_id,
            target_kind=target_kind,
            target_id=target_id,
            edge_kind=edge_kind,
            source_table="context_citation",
            weight=float(count),
            occurred_at=latest,
        )
        for (citing_page_id, target_kind, target_id), (
            count,
            latest,
        ) in aggregated.items()
    ]


def _published_document_citation_rows(
    session, workspace_id, target_kind, citing_page_id=None
):
    from backend.models.agent import AgentPage
    from backend.models.document_reference import ContextPublishedCitation
    from backend.models.library import LibraryItem
    from backend.repositories.document_reference import (
        document_reference_schema_available,
    )

    if not document_reference_schema_available(session):
        return []
    if target_kind == "context_page":
        target_model = aliased(ContextPage)
        target_column = ContextPublishedCitation.context_page_id
        target_filters = (
            target_model.current_published_version_id.is_not(None),
            target_model.lifecycle_status != "archived",
        )
    elif target_kind == "library_item":
        target_model, target_column = (
            LibraryItem,
            ContextPublishedCitation.library_item_id,
        )
        target_filters = (LibraryItem.lifecycle_status.notin_(("archived", "hidden")),)
    else:
        target_model, target_column = AgentPage, ContextPublishedCitation.agent_page_id
        target_filters = (
            AgentPage.current_active_version_id.is_not(None),
            AgentPage.lifecycle_status != "archived",
        )
    conditions = (
        ContextPage.lifecycle_status != "archived",
        ContextPublishedCitation.target_kind == target_kind,
        *target_filters,
    )
    if citing_page_id is not None:
        conditions += (ContextPage.id == citing_page_id,)
    statement = select_in_workspace(
        ContextPublishedCitation,
        workspace_id=workspace_id,
        conditions=conditions,
    )
    statement = join_in_workspace(
        statement,
        ContextPublishedCitation,
        ContextPage,
        workspace_id=workspace_id,
        on=ContextPage.current_published_version_id
        == ContextPublishedCitation.source_version_id,
    )
    statement = join_in_workspace(
        statement,
        ContextPublishedCitation,
        target_model,
        workspace_id=workspace_id,
        on=target_model.id == target_column,
    ).with_only_columns(ContextPublishedCitation, ContextPage.id)
    edge_kind = {
        "context_page": "citation_page",
        "library_item": "citation_source",
        "agent_page": "citation_agent",
    }[target_kind]
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.CONTEXT_PAGE,
            source_id=page_id,
            target_kind=EntityKind(target_kind),
            target_id=row.target_id,
            edge_kind=edge_kind,
            source_table="context_published_citation",
            weight=float(
                max(
                    1, row.inline_count, row.legacy_count if not row.inline_count else 0
                )
            ),
            occurred_at=row.created_at,
        )
        for row, page_id in session.execute(statement)
    ]


def citation_agent_rows(session, workspace_id, citing_page_id=None):
    return _published_document_citation_rows(
        session, workspace_id, "agent_page", citing_page_id
    )


def citation_page_rows(
    session: Session,
    workspace_id: UUID,
    citing_page_id: UUID | None = None,
) -> list[dict[str, object]]:
    """Curated page->page citations (ContextCitation XOR: cited page arm)."""
    statement = join_in_workspace(
        select_in_workspace(
            ContextCitation,
            workspace_id=workspace_id,
            conditions=(ContextCitation.cited_context_page_version_id.is_not(None),),
        ),
        ContextCitation,
        ContextPageVersion,
        workspace_id=workspace_id,
        on=ContextCitation.cited_context_page_version_id == ContextPageVersion.id,
    ).with_only_columns(ContextCitation, ContextPageVersion.page_id)
    rows = session.execute(
        _citation_scope(statement, citing_page_id, workspace_id)
    ).all()
    return _published_document_citation_rows(
        session, workspace_id, "context_page", citing_page_id
    ) + _aggregate_citation_edges(
        session,
        workspace_id,
        "citation_page",
        [
            (citation, EntityKind.CONTEXT_PAGE, cited_page_id)
            for citation, cited_page_id in rows
        ],
    )


def citation_source_rows(
    session: Session,
    workspace_id: UUID,
    citing_page_id: UUID | None = None,
) -> list[dict[str, object]]:
    """Curated page->library citations (artifact / item-version arms)."""
    statement = select_in_workspace(
        ContextCitation,
        workspace_id=workspace_id,
        conditions=(
            (ContextCitation.library_artifact_id.is_not(None))
            | (ContextCitation.library_item_version_id.is_not(None)),
        ),
    )
    citations = session.scalars(
        _citation_scope(statement, citing_page_id, workspace_id)
    ).all()
    per_citation: list[tuple[ContextCitation, EntityKind, UUID]] = []
    for citation in citations:
        if citation.library_artifact_id is not None:
            target_kind = EntityKind.LIBRARY_ARTIFACT
            target_id = citation.library_artifact_id
        else:
            target_kind = EntityKind.LIBRARY_ITEM_VERSION
            target_id = citation.library_item_version_id
        per_citation.append((citation, target_kind, target_id))
    return _published_document_citation_rows(
        session, workspace_id, "library_item", citing_page_id
    ) + _aggregate_citation_edges(
        session, workspace_id, "citation_source", per_citation
    )


def _citing_page_map(
    session: Session, citations: list[ContextCitation], workspace_id: UUID
) -> dict[UUID, UUID]:
    """Map citations to a page only when they belong to its published head."""
    version_targets = {
        citation.target_id
        for citation in citations
        if citation.target_kind
        in (
            "context_page_version",
            "context_page_whole",
            "context_page_section",
        )
    }
    version_owners: dict[UUID, tuple[UUID, UUID]] = {}
    if version_targets:
        version_owners = {
            version_id: (page_id, version_id)
            for version_id, page_id in session.execute(
                select_in_workspace(
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    conditions=(ContextPageVersion.id.in_(version_targets),),
                ).with_only_columns(ContextPageVersion.id, ContextPageVersion.page_id)
            ).all()
        }

    claim_targets = {
        citation.target_id
        for citation in citations
        if citation.target_kind == "context_claim"
    }
    claim_owners: dict[UUID, tuple[UUID, UUID]] = {}
    if claim_targets:
        claim_owners = {
            claim_id: (page_id, version_id)
            for claim_id, version_id, page_id in session.execute(
                join_in_workspace(
                    select_in_workspace(
                        ContextClaim,
                        workspace_id=workspace_id,
                        conditions=(ContextClaim.id.in_(claim_targets),),
                    ),
                    ContextClaim,
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    on=ContextClaim.page_version_id == ContextPageVersion.id,
                ).with_only_columns(
                    ContextClaim.id, ContextPageVersion.id, ContextPageVersion.page_id
                )
            ).all()
        }

    page_targets = {
        citation.target_id
        for citation in citations
        if citation.target_kind == "context_page"
    }
    owner_by_citation: dict[UUID, tuple[UUID, UUID | None]] = {}
    for citation in citations:
        if citation.target_kind in (
            "context_page_version",
            "context_page_whole",
            "context_page_section",
        ):
            owner = version_owners.get(citation.target_id)
        elif citation.target_kind == "context_claim":
            owner = claim_owners.get(citation.target_id)
        elif citation.target_kind == "context_page":
            owner = (citation.target_id, None)
        else:
            owner = None
        if owner is not None:
            owner_by_citation[citation.id] = owner

    page_ids = page_targets | {
        page_id for page_id, _version_id in owner_by_citation.values()
    }
    published_by_page: dict[UUID, UUID | None] = {}
    if page_ids:
        published_by_page = dict(
            session.execute(
                select_in_workspace(
                    ContextPage,
                    workspace_id=workspace_id,
                    conditions=(ContextPage.id.in_(page_ids),),
                ).with_only_columns(
                    ContextPage.id, ContextPage.current_published_version_id
                )
            ).all()
        )

    mapping: dict[UUID, UUID] = {}
    for citation_id, (page_id, version_id) in owner_by_citation.items():
        published_version_id = published_by_page.get(page_id)
        if published_version_id is not None and (
            version_id is None or version_id == published_version_id
        ):
            mapping[citation_id] = page_id
    return mapping


def _workspace_page_ids(
    session: Session, page_ids: set[UUID], workspace_id: UUID
) -> set[UUID]:
    if not page_ids:
        return set()
    return set(
        session.scalars(
            select_in_workspace(
                ContextPage,
                workspace_id=workspace_id,
                conditions=(ContextPage.id.in_(page_ids),),
            ).with_only_columns(ContextPage.id)
        ).all()
    )


def document_citation_source_ids(session, workspace_id, target_kind, target_id):
    """Reconcile incoming citations when a target becomes publishable again.

    The sources already have registered document identities. This never parses
    historical bodies or republishes a source document.
    """
    from backend.models.document_reference import ContextPublishedCitation
    from backend.repositories.document_reference import (
        document_reference_schema_available,
    )

    if not document_reference_schema_available(session):
        return []
    column = {
        "context_page": ContextPublishedCitation.context_page_id,
        "library_item": ContextPublishedCitation.library_item_id,
        "agent_page": ContextPublishedCitation.agent_page_id,
    }[target_kind]
    statement = select_in_workspace(
        ContextPage,
        workspace_id=workspace_id,
        conditions=(column == target_id,),
    )
    statement = (
        join_in_workspace(
            statement,
            ContextPage,
            ContextPublishedCitation,
            workspace_id=workspace_id,
            on=ContextPublishedCitation.source_version_id
            == ContextPage.current_published_version_id,
        )
        .with_only_columns(ContextPage.id)
        .distinct()
    )
    return list(session.scalars(statement))

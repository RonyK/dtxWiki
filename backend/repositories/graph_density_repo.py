"""Bounded SQL inputs for workspace relation-density reporting.

The repository reads authored relations from the canonical Context heads.
``EdgeProjection`` is intentionally absent: it is a rebuildable projection and
cannot be the source of an operator-facing density measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, func, select, union_all
from sqlalchemy.orm import Session, aliased

from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    PageLink,
)
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.workspace import Workspace
from backend.repositories.context_visibility import (
    PUBLISHED_CONTEXT_VERSION_STATUSES,
)

_VERSION_CITATION_TARGET_KINDS = (
    "context_page_version",
    "context_page_whole",
    "context_page_section",
)
_EXCLUDED_LIBRARY_LIFECYCLE_STATUSES = ("archived", "hidden")
_VISIBLE_FALLBACK_LIBRARY_VERSION_STATUSES = (
    "completed",
    "ready",
    "ready_partial",
)


@dataclass(frozen=True, slots=True)
class GraphDensityWorkspaceRow:
    id: UUID
    slug: str
    edge_projection_version: int


@dataclass(frozen=True, slots=True)
class GraphDensityContextHeadRow:
    page_id: UUID
    version_id: UUID


@dataclass(frozen=True, slots=True)
class GraphDensityPageLinkRow:
    source_page_id: UUID
    target_page_id: UUID


@dataclass(frozen=True, slots=True)
class GraphDensityCitationRow:
    """One raw current-head citation plus its document-grain target mapping."""

    citation_id: UUID
    citation_type: str
    source_page_id: UUID
    source_version_id: UUID
    source_target_kind: str
    source_target_id: UUID
    cited_context_page_version_id: UUID | None
    cited_context_page_id: UUID | None
    library_item_version_id: UUID | None
    library_artifact_id: UUID | None
    cited_library_item_version_id: UUID | None
    cited_library_item_id: UUID | None


@dataclass(frozen=True, slots=True)
class GraphDensityLibraryHeadRow:
    item_id: UUID
    workspace_id: UUID
    lifecycle_status: str
    trust_level: str
    current_ready_version_id: UUID | None
    current_version_id: UUID | None
    version_id: UUID
    version_library_item_id: UUID
    processing_status: str


@dataclass(frozen=True, slots=True)
class GraphDensitySnapshotInputs:
    workspace: GraphDensityWorkspaceRow
    context_heads: tuple[GraphDensityContextHeadRow, ...]
    page_links: tuple[GraphDensityPageLinkRow, ...]
    citations: tuple[GraphDensityCitationRow, ...]
    library_heads: tuple[GraphDensityLibraryHeadRow, ...]


def load_graph_density_workspace_row(
    session: Session,
    workspace_id: UUID,
) -> GraphDensityWorkspaceRow | None:
    row = session.execute(
        select(
            Workspace.id,
            Workspace.slug,
            Workspace.edge_projection_version,
        ).where(Workspace.id == workspace_id)
    ).one_or_none()
    if row is None:
        return None
    return GraphDensityWorkspaceRow(
        id=row.id,
        slug=row.slug,
        edge_projection_version=row.edge_projection_version,
    )


def _load_context_heads(
    session: Session,
    workspace_id: UUID,
) -> tuple[GraphDensityContextHeadRow, ...]:
    rows = session.execute(
        select(ContextPage.id, ContextPageVersion.id)
        .select_from(ContextPage)
        .join(
            ContextPageVersion,
            and_(
                ContextPageVersion.workspace_id == ContextPage.workspace_id,
                ContextPageVersion.workspace_id == workspace_id,
                ContextPageVersion.page_id == ContextPage.id,
                ContextPageVersion.id == ContextPage.current_published_version_id,
            ),
        )
        .where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.lifecycle_status != "archived",
            ContextPageVersion.version_status.in_(
                PUBLISHED_CONTEXT_VERSION_STATUSES
            ),
        )
        .order_by(ContextPage.id)
    ).all()
    return tuple(
        GraphDensityContextHeadRow(page_id=page_id, version_id=version_id)
        for page_id, version_id in rows
    )


def _load_page_links(
    session: Session,
    workspace_id: UUID,
) -> tuple[GraphDensityPageLinkRow, ...]:
    source_version = aliased(ContextPageVersion, name="density_link_source_version")
    target_page = aliased(ContextPage, name="density_link_target_page")
    rows = session.execute(
        select(
            PageLink.source_page_id,
            PageLink.target_page_id,
        )
        .select_from(PageLink)
        .join(
            ContextPage,
            and_(
                ContextPage.workspace_id == PageLink.workspace_id,
                ContextPage.workspace_id == workspace_id,
                ContextPage.id == PageLink.source_page_id,
                ContextPage.current_published_version_id
                == PageLink.source_version_id,
            ),
        )
        .join(
            source_version,
            and_(
                source_version.workspace_id == PageLink.workspace_id,
                source_version.workspace_id == workspace_id,
                source_version.page_id == PageLink.source_page_id,
                source_version.id == PageLink.source_version_id,
                source_version.version_status.in_(
                    PUBLISHED_CONTEXT_VERSION_STATUSES
                ),
            ),
        )
        .join(
            target_page,
            and_(
                target_page.workspace_id == PageLink.workspace_id,
                target_page.workspace_id == workspace_id,
                target_page.id == PageLink.target_page_id,
            ),
        )
        .where(
            PageLink.workspace_id == workspace_id,
            ContextPage.lifecycle_status != "archived",
            PageLink.target_page_id.is_not(None),
        )
        .with_hint(
            PageLink,
            "INDEXED BY idx_page_links_graph_head",
            dialect_name="sqlite",
        )
        .order_by(PageLink.id)
    ).all()
    return tuple(
        GraphDensityPageLinkRow(
            source_page_id=source_page_id,
            target_page_id=target_page_id,
        )
        for source_page_id, target_page_id in rows
    )


def _current_head_citation_sources(workspace_id: UUID):
    version_source_page = aliased(
        ContextPage,
        name="density_citation_version_source_page",
    )
    version_source = aliased(
        ContextPageVersion,
        name="density_citation_version_source",
    )
    claim_source_page = aliased(
        ContextPage,
        name="density_citation_claim_source_page",
    )
    claim_source_version = aliased(
        ContextPageVersion,
        name="density_citation_claim_source_version",
    )
    legacy_source_page = aliased(
        ContextPage,
        name="density_citation_legacy_source_page",
    )
    legacy_source_version = aliased(
        ContextPageVersion,
        name="density_citation_legacy_source_version",
    )

    raw_columns = (
        ContextCitation.id.label("citation_id"),
        ContextCitation.citation_type.label("citation_type"),
        ContextCitation.target_kind.label("source_target_kind"),
        ContextCitation.target_id.label("source_target_id"),
        ContextCitation.cited_context_page_version_id.label(
            "cited_context_page_version_id"
        ),
        ContextCitation.library_item_version_id.label("library_item_version_id"),
        ContextCitation.library_artifact_id.label("library_artifact_id"),
    )
    version_citations = (
        select(
            *raw_columns,
            version_source_page.id.label("source_page_id"),
            version_source.id.label("source_version_id"),
        )
        .select_from(ContextCitation)
        .join(
            version_source,
            and_(
                version_source.workspace_id == ContextCitation.workspace_id,
                version_source.workspace_id == workspace_id,
                version_source.id == ContextCitation.target_id,
                version_source.version_status.in_(
                    PUBLISHED_CONTEXT_VERSION_STATUSES
                ),
            ),
        )
        .join(
            version_source_page,
            and_(
                version_source_page.workspace_id == version_source.workspace_id,
                version_source_page.workspace_id == workspace_id,
                version_source_page.id == version_source.page_id,
                version_source_page.current_published_version_id == version_source.id,
            ),
        )
        .where(
            ContextCitation.workspace_id == workspace_id,
            ContextCitation.target_kind.in_(_VERSION_CITATION_TARGET_KINDS),
            version_source_page.lifecycle_status != "archived",
        )
        .with_hint(
            ContextCitation,
            "INDEXED BY idx_context_citation_graph_head",
            dialect_name="sqlite",
        )
    )
    claim_citations = (
        select(
            *raw_columns,
            claim_source_page.id.label("source_page_id"),
            claim_source_version.id.label("source_version_id"),
        )
        .select_from(ContextCitation)
        .join(
            ContextClaim,
            and_(
                ContextClaim.workspace_id == ContextCitation.workspace_id,
                ContextClaim.workspace_id == workspace_id,
                ContextClaim.id == ContextCitation.target_id,
            ),
        )
        .join(
            claim_source_version,
            and_(
                claim_source_version.workspace_id == ContextClaim.workspace_id,
                claim_source_version.workspace_id == workspace_id,
                claim_source_version.id == ContextClaim.page_version_id,
                claim_source_version.version_status.in_(
                    PUBLISHED_CONTEXT_VERSION_STATUSES
                ),
            ),
        )
        .join(
            claim_source_page,
            and_(
                claim_source_page.workspace_id == claim_source_version.workspace_id,
                claim_source_page.workspace_id == workspace_id,
                claim_source_page.id == claim_source_version.page_id,
                claim_source_page.current_published_version_id
                == claim_source_version.id,
            ),
        )
        .where(
            ContextCitation.workspace_id == workspace_id,
            ContextCitation.target_kind == "context_claim",
            claim_source_page.lifecycle_status != "archived",
        )
        .with_hint(
            ContextCitation,
            "INDEXED BY idx_context_citation_graph_head",
            dialect_name="sqlite",
        )
    )
    legacy_page_citations = (
        select(
            *raw_columns,
            legacy_source_page.id.label("source_page_id"),
            legacy_source_version.id.label("source_version_id"),
        )
        .select_from(ContextCitation)
        .join(
            legacy_source_page,
            and_(
                legacy_source_page.workspace_id == ContextCitation.workspace_id,
                legacy_source_page.workspace_id == workspace_id,
                legacy_source_page.id == ContextCitation.target_id,
            ),
        )
        .join(
            legacy_source_version,
            and_(
                legacy_source_version.workspace_id
                == legacy_source_page.workspace_id,
                legacy_source_version.workspace_id == workspace_id,
                legacy_source_version.page_id == legacy_source_page.id,
                legacy_source_version.id
                == legacy_source_page.current_published_version_id,
                legacy_source_version.version_status.in_(
                    PUBLISHED_CONTEXT_VERSION_STATUSES
                ),
            ),
        )
        .where(
            ContextCitation.workspace_id == workspace_id,
            ContextCitation.target_kind == "context_page",
            legacy_source_page.lifecycle_status != "archived",
        )
        .with_hint(
            ContextCitation,
            "INDEXED BY idx_context_citation_graph_head",
            dialect_name="sqlite",
        )
    )
    return union_all(
        version_citations,
        claim_citations,
        legacy_page_citations,
    ).subquery(
        "density_current_head_citations"
    )


def _load_citations(
    session: Session,
    workspace_id: UUID,
) -> tuple[GraphDensityCitationRow, ...]:
    sources = _current_head_citation_sources(workspace_id)
    cited_context_version = aliased(
        ContextPageVersion,
        name="density_cited_context_version",
    )
    cited_context_page = aliased(
        ContextPage,
        name="density_cited_context_page",
    )
    cited_library_version = aliased(
        LibraryItemVersion,
        name="density_cited_library_version",
    )
    cited_library_artifact = aliased(
        LibraryArtifact,
        name="density_cited_library_artifact",
    )
    artifact_library_version = aliased(
        LibraryItemVersion,
        name="density_artifact_library_version",
    )
    rows = session.execute(
        select(
            sources.c.citation_id,
            sources.c.citation_type,
            sources.c.source_page_id,
            sources.c.source_version_id,
            sources.c.source_target_kind,
            sources.c.source_target_id,
            sources.c.cited_context_page_version_id,
            cited_context_page.id.label("cited_context_page_id"),
            sources.c.library_item_version_id,
            sources.c.library_artifact_id,
            func.coalesce(
                artifact_library_version.id,
                cited_library_version.id,
            ).label("cited_library_item_version_id"),
            func.coalesce(
                artifact_library_version.library_item_id,
                cited_library_version.library_item_id,
            ).label("cited_library_item_id"),
        )
        .select_from(sources)
        .outerjoin(
            cited_context_version,
            and_(
                cited_context_version.workspace_id == workspace_id,
                cited_context_version.id
                == sources.c.cited_context_page_version_id,
                cited_context_version.version_status.in_(
                    PUBLISHED_CONTEXT_VERSION_STATUSES
                ),
            ),
        )
        .outerjoin(
            cited_context_page,
            and_(
                cited_context_page.workspace_id
                == cited_context_version.workspace_id,
                cited_context_page.workspace_id == workspace_id,
                cited_context_page.id == cited_context_version.page_id,
            ),
        )
        .outerjoin(
            cited_library_version,
            and_(
                cited_library_version.workspace_id == workspace_id,
                cited_library_version.id == sources.c.library_item_version_id,
            ),
        )
        .outerjoin(
            cited_library_artifact,
            and_(
                cited_library_artifact.workspace_id == workspace_id,
                cited_library_artifact.id == sources.c.library_artifact_id,
            ),
        )
        .outerjoin(
            artifact_library_version,
            and_(
                artifact_library_version.workspace_id
                == cited_library_artifact.workspace_id,
                artifact_library_version.workspace_id == workspace_id,
                artifact_library_version.id
                == cited_library_artifact.library_item_version_id,
            ),
        )
        .order_by(sources.c.citation_id)
    ).all()
    return tuple(
        GraphDensityCitationRow(
            citation_id=row.citation_id,
            citation_type=row.citation_type,
            source_page_id=row.source_page_id,
            source_version_id=row.source_version_id,
            source_target_kind=row.source_target_kind,
            source_target_id=row.source_target_id,
            cited_context_page_version_id=row.cited_context_page_version_id,
            cited_context_page_id=row.cited_context_page_id,
            library_item_version_id=row.library_item_version_id,
            library_artifact_id=row.library_artifact_id,
            cited_library_item_version_id=row.cited_library_item_version_id,
            cited_library_item_id=row.cited_library_item_id,
        )
        for row in rows
    )


def _load_library_heads(
    session: Session,
    workspace_id: UUID,
) -> tuple[GraphDensityLibraryHeadRow, ...]:
    rows = session.execute(
        select(
            LibraryItem.id,
            LibraryItem.workspace_id,
            LibraryItem.lifecycle_status,
            LibraryItem.trust_level,
            LibraryItem.current_ready_version_id,
            LibraryItem.current_version_id,
            LibraryItemVersion.id,
            LibraryItemVersion.library_item_id,
            LibraryItemVersion.processing_status,
        )
        .select_from(LibraryItem)
        .join(
            LibraryItemVersion,
            and_(
                LibraryItemVersion.workspace_id == LibraryItem.workspace_id,
                LibraryItemVersion.workspace_id == workspace_id,
                LibraryItemVersion.library_item_id == LibraryItem.id,
                LibraryItemVersion.id
                == func.coalesce(
                    LibraryItem.current_ready_version_id,
                    LibraryItem.current_version_id,
                ),
            ),
        )
        .where(
            LibraryItem.workspace_id == workspace_id,
            LibraryItem.lifecycle_status.notin_(
                _EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
            ),
            (
                LibraryItem.current_ready_version_id.is_not(None)
                | (
                    LibraryItem.current_ready_version_id.is_(None)
                    & LibraryItemVersion.processing_status.in_(
                        _VISIBLE_FALLBACK_LIBRARY_VERSION_STATUSES
                    )
                )
            ),
        )
        .order_by(LibraryItem.id)
    ).all()
    return tuple(
        GraphDensityLibraryHeadRow(
            item_id=item_id,
            workspace_id=row_workspace_id,
            lifecycle_status=lifecycle_status,
            trust_level=trust_level,
            current_ready_version_id=current_ready_version_id,
            current_version_id=current_version_id,
            version_id=version_id,
            version_library_item_id=version_library_item_id,
            processing_status=processing_status,
        )
        for (
            item_id,
            row_workspace_id,
            lifecycle_status,
            trust_level,
            current_ready_version_id,
            current_version_id,
            version_id,
            version_library_item_id,
            processing_status,
        ) in rows
    )


def load_graph_density_snapshot_inputs(
    session: Session,
    workspace_id: UUID,
) -> GraphDensitySnapshotInputs | None:
    """Load one density snapshot with five statements regardless of data size.

    ``library_heads`` contains lifecycle-visible canonical items only. Restricted
    rows remain present so the service can classify them against the reporting
    operator without re-querying or leaking them into an unauthorized report.
    """

    workspace = load_graph_density_workspace_row(session, workspace_id)
    if workspace is None:
        return None
    return GraphDensitySnapshotInputs(
        workspace=workspace,
        context_heads=_load_context_heads(session, workspace_id),
        page_links=_load_page_links(session, workspace_id),
        citations=_load_citations(session, workspace_id),
        library_heads=_load_library_heads(session, workspace_id),
    )

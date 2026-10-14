"""Repair authored-link graph projections from Context source versions."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.graph import GraphHistoryCoverage
from backend.repositories import context_page_repo
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.graph_cache import bump_edge_version, edge_version
from backend.services.indexing.edge_registry import (
    EdgeProjectionDelta,
    reconcile_wikilink_edge_projection,
)
from backend.services.page_service import (
    PageLinkProjectionDelta,
    prune_stale_page_link_projections,
    sync_page_link_projection,
)


class GraphReprojectionInvariantError(RuntimeError):
    """A Context version pointer cannot be safely projected."""

    error_code = "invalid_context_version_pointer"


@dataclass(frozen=True)
class WorkspaceGraphReprojectionResult:
    workspace_id: UUID
    pages: int
    versions: int
    page_links: int
    wikilink_edges: int
    page_link_changes: PageLinkProjectionDelta
    edge_changes: EdgeProjectionDelta
    edge_projection_version_before: int
    edge_projection_version_after: int
    ignored_non_live_pointers: int


def reproject_workspace_graph(
    session: Session,
    workspace_id: UUID,
) -> WorkspaceGraphReprojectionResult:
    """Reconcile one workspace without committing or fabricating history.

    The caller owns the transaction. Page and version locks follow the normal
    publish order; the workspace revision is changed only after all authored
    link rows have reached their fixed point.
    """

    acquire_workspace_write_intent(session)
    pages = list(
        session.scalars(
            select(ContextPage)
            .where(ContextPage.workspace_id == workspace_id)
            .order_by(ContextPage.id.asc())
            .with_for_update()
        )
    )
    live_ids_by_page: dict[UUID, set[UUID]] = {}
    pointer_ids_by_page: dict[UUID, set[UUID]] = {}
    version_ids: set[UUID] = set()
    for page in pages:
        pointer_ids = _page_pointer_ids(page)
        live_ids = context_page_repo.live_page_link_version_ids(session, page)
        pointer_ids_by_page[page.id] = pointer_ids
        live_ids_by_page[page.id] = live_ids
        version_ids.update(pointer_ids)
        version_ids.update(live_ids)

    locked_versions = (
        list(
            session.scalars(
                select(ContextPageVersion)
                .where(
                    ContextPageVersion.workspace_id == workspace_id,
                    ContextPageVersion.id.in_(version_ids),
                )
                .order_by(
                    ContextPageVersion.page_id.asc(),
                    ContextPageVersion.version_no.asc(),
                    ContextPageVersion.id.asc(),
                )
                .with_for_update()
            )
        )
        if version_ids
        else []
    )
    versions_by_id = {version.id: version for version in locked_versions}
    _validate_page_pointers(
        workspace_id=workspace_id,
        pages=pages,
        pointer_ids_by_page=pointer_ids_by_page,
        versions_by_id=versions_by_id,
    )

    revision_before = edge_version(session, workspace_id)
    processed_versions = 0
    ignored_non_live_pointers = 0
    page_link_changes = PageLinkProjectionDelta()
    for page in pages:
        live_ids = live_ids_by_page[page.id]
        ignored_non_live_pointers += len(pointer_ids_by_page[page.id] - live_ids)
        live_versions = sorted(
            (versions_by_id[version_id] for version_id in live_ids),
            key=lambda version: (version.version_no, str(version.id)),
        )
        for version in live_versions:
            delta = sync_page_link_projection(
                session,
                page=page,
                version=version,
                observed_at=version.created_at,
            )
            page_link_changes = page_link_changes.plus(
                inserted=delta.inserted,
                updated=delta.updated,
                deleted=delta.deleted,
            )
            processed_versions += 1
        if not live_versions:
            page_link_changes = page_link_changes.plus(
                deleted=prune_stale_page_link_projections(session, page=page)
            )

    # New PageLink rows must be visible to the aggregate extractor below.
    session.flush()
    edge_changes = reconcile_wikilink_edge_projection(
        session,
        workspace_id,
        source_page_ids={page.id for page in pages},
    )
    _demote_unreplayable_history_coverage(
        session,
        workspace_id,
        affected_source_ids=edge_changes.affected_source_ids,
    )
    revision_after = (
        bump_edge_version(session, workspace_id)
        if edge_changes.changed
        else edge_version(session, workspace_id)
    )
    page_links = int(
        session.scalar(
            select(func.count(PageLink.id)).where(PageLink.workspace_id == workspace_id)
        )
        or 0
    )
    return WorkspaceGraphReprojectionResult(
        workspace_id=workspace_id,
        pages=len(pages),
        versions=processed_versions,
        page_links=page_links,
        wikilink_edges=edge_changes.desired_count,
        page_link_changes=page_link_changes,
        edge_changes=edge_changes,
        edge_projection_version_before=revision_before,
        edge_projection_version_after=revision_after,
        ignored_non_live_pointers=ignored_non_live_pointers,
    )


def _page_pointer_ids(page: ContextPage) -> set[UUID]:
    return {
        version_id
        for version_id in (
            page.current_published_version_id,
            page.latest_draft_version_id,
            page.current_version_id,
        )
        if version_id is not None
    }


def _validate_page_pointers(
    *,
    workspace_id: UUID,
    pages: list[ContextPage],
    pointer_ids_by_page: dict[UUID, set[UUID]],
    versions_by_id: dict[UUID, ContextPageVersion],
) -> None:
    for page in pages:
        for version_id in pointer_ids_by_page[page.id]:
            version = versions_by_id.get(version_id)
            if version is None:
                raise GraphReprojectionInvariantError(
                    "Context page "
                    f"{page.id} points to missing version {version_id} or a "
                    "version outside the selected workspace; run 'dtxwiki "
                    "context history repair' before graph reprojection"
                )
            if version.workspace_id != workspace_id or version.page_id != page.id:
                raise GraphReprojectionInvariantError(
                    "Context page "
                    f"{page.id} points to version {version_id} owned by another "
                    "page or workspace; run 'dtxwiki context history repair' "
                    "before graph reprojection"
                )


def _demote_unreplayable_history_coverage(
    session: Session,
    workspace_id: UUID,
    *,
    affected_source_ids: frozenset[UUID],
) -> None:
    """Stop claiming complete replay after a repair with no temporal event."""

    if not affected_source_ids:
        return
    coverages = session.scalars(
        select(GraphHistoryCoverage)
        .where(
            GraphHistoryCoverage.workspace_id == workspace_id,
            GraphHistoryCoverage.stream_kind == "context_page",
            GraphHistoryCoverage.stream_id.in_(affected_source_ids),
            GraphHistoryCoverage.status.in_(("complete", "reconstructed")),
        )
        .order_by(GraphHistoryCoverage.stream_id.asc())
        .with_for_update()
    )
    for coverage in coverages:
        coverage.status = "partial"
        coverage.updated_at = utc_now()

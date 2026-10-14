"""Rebuild all derived search and navigation projections for a workspace."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.container import DatabaseProvider
from backend.models.chunk import ContentChunk
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryItem
from backend.services.delegated_tasks import produce_folder_summary_tasks
from backend.services.indexing.edge_registry import rebuild_edge_projection
from backend.services.page_service import sync_page_projections
from backend.services.retrieval.protocol import Indexer
from backend.services.workspace_graph_capability import (
    usage_relation_signals_enabled,
)


def reindex_workspace(
    database_provider: DatabaseProvider,
    indexer: Indexer,
    workspace_id: UUID,
) -> dict[str, int]:
    """Rebuild transactional projections, then the configured index planes."""
    # The owner's usage-signal switch lives on the control plane.
    with database_provider.control_session_factory()() as control_session:
        usage_signals = usage_relation_signals_enabled(control_session, workspace_id)
    with database_provider.session_for(workspace_id).begin() as session:
        pages = session.scalars(
            select(ContextPage).where(ContextPage.workspace_id == workspace_id)
        ).all()
        page_ids = [page.id for page in pages]
        item_ids = session.scalars(
            select(LibraryItem.id).where(
                LibraryItem.workspace_id == workspace_id,
                LibraryItem.lifecycle_status == "ready",
            )
        ).all()
        synced_versions = 0
        for page in pages:
            live_versions = _live_context_versions(session, page)
            if not live_versions:
                sync_page_projections(
                    session,
                    page=page,
                    version=None,
                    record_graph_observations=False,
                )
            for version in live_versions:
                sync_page_projections(
                    session,
                    page=page,
                    version=version,
                    record_graph_observations=False,
                )
                synced_versions += 1
        edge_counts = rebuild_edge_projection(
            session, workspace_id, usage_signals_enabled=usage_signals
        )
        delegated = produce_folder_summary_tasks(session, workspace_id)
        counts = {
            "context_pages": len(page_ids),
            "context_versions": synced_versions,
            "library_items": len(item_ids),
            "edge_projections": sum(edge_counts.values()),
            "delegated_tasks": delegated,
        }
    index_counts = indexer.rebuild(workspace_id=workspace_id)
    counts["pruned_orphans"] = index_counts.get("pruned_orphans", 0)
    counts["search_documents"] = sum(
        value
        for key, value in index_counts.items()
        if key != "pruned_orphans"
    )
    return counts


def _live_context_versions(
    session: Session,
    page: ContextPage,
) -> list[ContextPageVersion]:
    version_ids = []
    for version_id in (
        page.current_published_version_id,
        page.latest_draft_version_id,
        page.current_version_id,
    ):
        if version_id is not None and version_id not in version_ids:
            version_ids.append(version_id)
    versions = []
    for version_id in version_ids:
        version = session.get(ContextPageVersion, version_id)
        if version is not None:
            versions.append(version)
    return versions


def prune_orphan_chunks(session: Session, workspace_id: UUID) -> int:
    """Delete content chunks whose parent document no longer exists.

    ``content_chunk.document_id`` is polymorphic (a Context page OR a Library
    item), so no FK cascade is possible; deleting a page/item leaves its chunks
    (and, absent this, their vectors) behind. Reindex reconciles them here — the
    ``chunk_embedding`` rows cascade via their FK on chunk delete. Returns the
    number of chunks removed.
    """
    live_page_ids = set(
        session.scalars(
            select(ContextPage.id).where(ContextPage.workspace_id == workspace_id)
        ).all()
    )
    live_item_ids = set(
        session.scalars(
            select(LibraryItem.id).where(LibraryItem.workspace_id == workspace_id)
        ).all()
    )
    removed = 0
    for chunk in session.scalars(
        select(ContentChunk).where(ContentChunk.workspace_id == workspace_id)
    ).all():
        live_ids = (
            live_page_ids
            if chunk.document_kind == "context_page"
            else live_item_ids
        )
        if chunk.document_id not in live_ids:
            session.delete(chunk)
            removed += 1
    session.flush()
    return removed

"""SQLite FTS5 indexer and atomic write-through helpers."""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from functools import cache
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.container import DatabaseProvider
from backend.models.agent import AgentPage
from backend.models.context import (
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    PageNameIndex,
)
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.search_index import ensure_fts5_tables
from backend.repositories.search_projection import WorkspaceFts5ProjectionRepository
from backend.repositories.workspace_scoped import get_in_workspace
from backend.services.indexing.doc_source import (
    compose_agent_page_doc_sources,
    compose_context_page_doc_sources,
    compose_library_doc_sources,
)


def sync_context_page_doc_sources(session: Session, page: ContextPage) -> int:
    if not _prepare_fts5(session):
        return 0
    repository = WorkspaceFts5ProjectionRepository(session, page.workspace_id)
    repository.delete_document_entity(
        source_kind="context_page",
        entity_id=page.id,
    )
    version_ids = session.scalars(
        select(ContextPageVersion.id).where(ContextPageVersion.page_id == page.id)
    ).all()
    repository.delete_document_versions(
        source_kind="context_claim",
        version_ids=version_ids,
    )
    if page.lifecycle_status == "archived":
        return 0
    documents = compose_context_page_doc_sources(session, page)
    repository.insert_documents(documents)
    return len(documents)


def sync_agent_page_doc_sources(session: Session, page: AgentPage) -> int:
    if not _prepare_fts5(session):
        return 0
    repository = WorkspaceFts5ProjectionRepository(session, page.workspace_id)
    repository.delete_document_entity(
        source_kind="agent_page",
        entity_id=page.id,
    )
    if page.lifecycle_status == "archived":
        return 0
    documents = compose_agent_page_doc_sources(session, page)
    repository.insert_documents(documents)
    return len(documents)


def sync_library_doc_sources(session: Session, item: LibraryItem) -> int:
    if not _prepare_fts5(session):
        return 0
    repository = WorkspaceFts5ProjectionRepository(session, item.workspace_id)
    repository.delete_document_entity(
        source_kind="library_item",
        entity_id=item.id,
    )
    version_ids = session.scalars(
        select(LibraryItemVersion.id).where(
            LibraryItemVersion.library_item_id == item.id
        )
    ).all()
    repository.delete_document_versions(
        source_kind="library_artifact",
        version_ids=version_ids,
    )
    if item.lifecycle_status in {"archived", "hidden"}:
        return 0
    documents = compose_library_doc_sources(session, item)
    repository.insert_documents(documents)
    return len(documents)


def sync_page_name_trigrams(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
    rows: Iterable[PageNameIndex] | None = None,
) -> int:
    if not _prepare_fts5(session):
        return 0
    resolved_rows = (
        list(rows)
        if rows is not None
        else session.scalars(
            select(PageNameIndex).where(
                PageNameIndex.workspace_id == workspace_id,
                PageNameIndex.page_id == page_id,
            )
        ).all()
    )
    return WorkspaceFts5ProjectionRepository(session, workspace_id).replace_page_names(
        page_id=page_id, rows=resolved_rows
    )


class Fts5Indexer:
    """Session-independent Tier-B Indexer implementation.

    Request writers call the ``sync_*`` helpers above in their transaction;
    this object is the reindex/overlay SPI and therefore owns its sessions.
    """

    def __init__(self, database_provider: DatabaseProvider) -> None:
        self.database_provider = database_provider

    def upsert(
        self,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        with self.database_provider.session_for(workspace_id).begin() as session:
            self.upsert_in_session(
                session,
                workspace_id=workspace_id,
                source_kind=source_kind,
                source_id=source_id,
            )

    def delete(
        self,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        with self.database_provider.session_for(workspace_id).begin() as session:
            self.delete_in_session(
                session,
                workspace_id=workspace_id,
                source_kind=source_kind,
                source_id=source_id,
            )

    def rebuild(
        self,
        *,
        workspace_id: UUID,
        source_kind: str | None = None,
        source_id: UUID | None = None,
    ) -> dict[str, int]:
        if source_id is not None:
            if source_kind is None:
                raise ValueError("source_kind is required when source_id is set")
            self.upsert(
                workspace_id=workspace_id,
                source_kind=source_kind,
                source_id=source_id,
            )
            return {source_kind: 1}
        with self.database_provider.session_for(workspace_id).begin() as session:
            if not _prepare_fts5(session):
                return {}
            if source_kind is None:
                WorkspaceFts5ProjectionRepository(session, workspace_id).purge_all()
            counts: dict[str, int] = {}
            if source_kind in (None, "context_page", "context_claim"):
                pages = session.scalars(
                    select(ContextPage).where(ContextPage.workspace_id == workspace_id)
                ).all()
                counts["context_page"] = sum(
                    sync_context_page_doc_sources(session, page) for page in pages
                )
                name_rows = session.scalars(
                    select(PageNameIndex).where(
                        PageNameIndex.workspace_id == workspace_id
                    )
                ).all()
                names_by_page: dict[UUID, list[PageNameIndex]] = {}
                for row in name_rows:
                    names_by_page.setdefault(row.page_id, []).append(row)
                counts["page_name_trgm"] = sum(
                    sync_page_name_trigrams(
                        session,
                        workspace_id=workspace_id,
                        page_id=page.id,
                        rows=names_by_page.get(page.id, []),
                    )
                    for page in pages
                )
            if source_kind in (None, "library_item", "library_artifact"):
                items = session.scalars(
                    select(LibraryItem).where(LibraryItem.workspace_id == workspace_id)
                ).all()
                counts["library_item"] = sum(
                    sync_library_doc_sources(session, item) for item in items
                )
            if source_kind in (None, "agent_page"):
                agents = session.scalars(
                    select(AgentPage).where(AgentPage.workspace_id == workspace_id)
                ).all()
                counts["agent_page"] = sum(
                    sync_agent_page_doc_sources(session, page) for page in agents
                )
            return counts

    def upsert_in_session(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        if source_kind == "context_page":
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=source_id,
            )
            if page is not None:
                sync_context_page_doc_sources(session, page)
                sync_page_name_trigrams(
                    session, workspace_id=workspace_id, page_id=page.id
                )
            return
        if source_kind == "context_claim":
            claim = get_in_workspace(
                session,
                ContextClaim,
                workspace_id=workspace_id,
                resource_id=source_id,
            )
            version = (
                get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    resource_id=claim.page_version_id,
                )
                if claim is not None
                else None
            )
            page = (
                get_in_workspace(
                    session,
                    ContextPage,
                    workspace_id=workspace_id,
                    resource_id=version.page_id,
                )
                if version
                else None
            )
            if page is not None:
                sync_context_page_doc_sources(session, page)
            return
        if source_kind in {"library_item", "library_artifact"}:
            item = _library_item_for_source(
                session,
                workspace_id=workspace_id,
                source_kind=source_kind,
                source_id=source_id,
            )
            if item is not None:
                sync_library_doc_sources(session, item)
            return
        if source_kind == "agent_page":
            page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=source_id,
            )
            if page is not None:
                sync_agent_page_doc_sources(session, page)
            return
        raise ValueError(f"unknown source_kind: {source_kind!r}")

    def delete_in_session(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        if not _prepare_fts5(session):
            return
        repository = WorkspaceFts5ProjectionRepository(session, workspace_id)
        repository.delete_document_entity(
            source_kind=source_kind,
            entity_id=source_id,
        )
        if source_kind == "context_page":
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=source_id,
            )
            if page is not None:
                version_ids = session.scalars(
                    select(ContextPageVersion.id).where(
                        ContextPageVersion.page_id == page.id
                    )
                ).all()
                repository.delete_document_versions(
                    source_kind="context_claim",
                    version_ids=version_ids,
                )
            repository.delete_orphan_claim_documents()
            sync_page_name_trigrams(
                session,
                workspace_id=workspace_id,
                page_id=source_id,
                rows=[],
            )
        elif source_kind == "library_item":
            item = get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=source_id,
            )
            if item is not None:
                version_ids = session.scalars(
                    select(LibraryItemVersion.id).where(
                        LibraryItemVersion.library_item_id == item.id
                    )
                ).all()
                repository.delete_document_versions(
                    source_kind="library_artifact",
                    version_ids=version_ids,
                )
            repository.delete_orphan_artifact_documents()


def purge_workspace_search_projections(session: Session, workspace_id: UUID) -> None:
    """Delete all rebuildable search rows owned by a workspace."""

    if not _prepare_fts5(session):
        return
    WorkspaceFts5ProjectionRepository(session, workspace_id).purge_all()


def _prepare_fts5(session: Session) -> bool:
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        _warn_non_sqlite_noop(connection.dialect.name)
        return False
    ensure_fts5_tables(connection)
    return True


@cache
def _warn_non_sqlite_noop(dialect_name: str) -> None:
    warnings.warn(
        f"Tier-B FTS5 projection is disabled for {dialect_name}; configure "
        "a search-plane Indexer for this database",
        RuntimeWarning,
        stacklevel=3,
    )


def _library_item_for_source(
    session: Session,
    *,
    workspace_id: UUID,
    source_kind: str,
    source_id: UUID,
) -> LibraryItem | None:
    if source_kind == "library_item":
        return get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=source_id,
        )
    artifact = get_in_workspace(
        session,
        LibraryArtifact,
        workspace_id=workspace_id,
        resource_id=source_id,
    )
    if artifact is None:
        return None
    version = get_in_workspace(
        session,
        LibraryItemVersion,
        workspace_id=workspace_id,
        resource_id=artifact.library_item_version_id,
    )
    return (
        get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=version.library_item_id,
        )
        if version
        else None
    )

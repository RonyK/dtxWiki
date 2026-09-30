"""DbNativeIndexer — chunk + embed + vector upsert into the db-native backend.

Implements the Indexer write path (Epic-1 §6): for a document it (re)chunks the
source body, **swaps** the content_chunk rows for each (document, content_scope)
so a stale version never lingers, embeds the chunks (contextual-header input),
and upserts vectors via the VectorStore. Context pages index both their
published and latest-draft scopes; Library items index their ready artifact.

v1 re-embeds a document's chunks on every index; chunk_text_hash is stored so a
content-addressed incremental reuse can be layered on later (R8 optimization).
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.container import DatabaseProvider
from backend.llm.base import EmbeddingProvider, EmbeddingRequest
from backend.models.chunk import ContentChunk
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryItem
from backend.services.chunking import chunk_markdown, embedding_input
from backend.services.db_write_intent import acquire_write_intent
from backend.services.entity_ref import EntityKind
from backend.services.library_canonical import (
    get_canonical_library_version,
    get_canonical_markdown_artifact,
    get_effective_library_markdown,
)
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.vector_store import VectorStore


def chunk_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def remove_document_chunks(
    session: Session,
    *,
    document_kind: str,
    document_id: UUID,
) -> int:
    """Remove one document's chunk/vector projection in the ambient transaction."""

    chunks = session.scalars(
        select(ContentChunk).where(
            ContentChunk.document_kind == document_kind,
            ContentChunk.document_id == document_id,
        )
    ).all()
    # ORM per-row delete keeps ChunkEmbedding cleanup correct on SQLite, where
    # foreign-key cascades are not guaranteed to be enabled.
    for chunk in chunks:
        session.delete(chunk)
    session.flush()
    return len(chunks)


class DbNativeIndexer:
    def __init__(
        self,
        *,
        embedder: EmbeddingProvider,
        vector_store: VectorStore,
        database_provider: DatabaseProvider | None = None,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.database_provider = database_provider

    def upsert(
        self,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        provider = self._require_database_provider()
        with provider.session_for(workspace_id).begin() as session:
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
        provider = self._require_database_provider()
        with provider.session_for(workspace_id).begin() as session:
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
            if source_kind in {"context_page", "library_item"}:
                self.upsert(
                    workspace_id=workspace_id,
                    source_kind=source_kind,
                    source_id=source_id,
                )
                return {source_kind: 1}
            if source_kind in {"context_claim", "library_artifact", "agent_page"}:
                return {}
            raise ValueError(f"unknown source_kind: {source_kind!r}")

        provider = self._require_database_provider()
        with provider.session_for(workspace_id).begin() as session:
            counts = {"context_page": 0, "library_item": 0}
            if source_kind in (None, "context_page"):
                page_ids = session.scalars(
                    select(ContextPage.id).where(
                        ContextPage.workspace_id == workspace_id
                    )
                ).all()
                for page_id in page_ids:
                    self._index_context_page(session, page_id)
                counts["context_page"] = len(page_ids)
            if source_kind in (None, "library_item"):
                item_ids = session.scalars(
                    select(LibraryItem.id).where(
                        LibraryItem.workspace_id == workspace_id,
                        LibraryItem.lifecycle_status == "ready",
                    )
                ).all()
                for item_id in item_ids:
                    self._index_library_item(session, item_id)
                counts["library_item"] = len(item_ids)
            if source_kind not in (
                None,
                "context_page",
                "library_item",
                "context_claim",
                "library_artifact",
                "agent_page",
            ):
                raise ValueError(f"unknown source_kind: {source_kind!r}")
            from backend.services.indexing.reindex import prune_orphan_chunks

            counts["pruned_orphans"] = prune_orphan_chunks(session, workspace_id)
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
            page = session.get(ContextPage, source_id)
            if page is None:
                return
            if page.workspace_id != workspace_id:
                raise ValueError("context page belongs to another workspace")
        elif source_kind == "library_item":
            item = session.get(LibraryItem, source_id)
            if item is None:
                return
            if item.workspace_id != workspace_id:
                raise ValueError("library item belongs to another workspace")
        elif source_kind in {"context_claim", "library_artifact", "agent_page"}:
            return
        else:
            raise ValueError(f"unknown source_kind: {source_kind!r}")
        self.index(session=session, source_kind=source_kind, source_id=source_id)

    def delete_in_session(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        if source_kind not in {"context_page", "library_item"}:
            if source_kind in {"context_claim", "library_artifact", "agent_page"}:
                return
            raise ValueError(f"unknown source_kind: {source_kind!r}")
        cross_workspace = session.scalar(
            select(ContentChunk.id)
            .where(
                ContentChunk.document_kind == source_kind,
                ContentChunk.document_id == source_id,
                ContentChunk.workspace_id != workspace_id,
            )
            .limit(1)
        )
        if cross_workspace is not None:
            raise ValueError("indexed document belongs to another workspace")
        self.remove(session=session, source_kind=source_kind, source_id=source_id)

    def _require_database_provider(self) -> DatabaseProvider:
        if self.database_provider is None:
            raise RuntimeError(
                "session-independent indexing requires a DatabaseProvider"
            )
        return self.database_provider

    # Compatibility methods for transaction-local callers. They are not part
    # of the SPI 1.0 Indexer protocol; production composition uses the methods
    # above, while the worker bridge joins its existing transaction explicitly.
    def index(self, *, session: Session, source_kind: str, source_id: UUID) -> None:
        if source_kind == "context_page":
            self._index_context_page(session, source_id)
        elif source_kind == "library_item":
            self._index_library_item(session, source_id)
        else:
            # Fail loud: a typo'd kind would otherwise "succeed" having indexed
            # nothing, leaving the document silently unsearchable.
            raise ValueError(f"unknown source_kind: {source_kind!r}")

    def remove(self, *, session: Session, source_kind: str, source_id: UUID) -> None:
        # Validate like index(): mapping an unknown/typo'd kind to "library_item"
        # would delete the wrong document's chunks (or mask a caller bug) instead
        # of failing loud (Codex F4 / merged I7).
        if source_kind not in {EntityKind.CONTEXT_PAGE, EntityKind.LIBRARY_ITEM}:
            raise ValueError(f"unknown source_kind: {source_kind!r}")
        remove_document_chunks(
            session,
            document_kind=source_kind,
            document_id=source_id,
        )

    # --- context ------------------------------------------------------------

    def _index_context_page(self, session: Session, page_id: UUID) -> None:
        page = session.get(ContextPage, page_id)
        if page is None:
            return
        if page.lifecycle_status == "archived":
            # Reindex is a reconstruction path, not a resurrection path. Drop
            # any stale vector projection instead of recreating chunks for an
            # entity that every read/search surface must hide.
            remove_document_chunks(
                session,
                document_kind=EntityKind.CONTEXT_PAGE.value,
                document_id=page.id,
            )
            return
        self._index_scope(
            session,
            workspace_id=page.workspace_id,
            document_kind="context_page",
            document_id=page.id,
            content_scope="published",
            version_id=page.current_published_version_id,
            title=page.title,
        )
        # Index the latest draft only when it is a distinct, newer version than
        # the published HEAD; otherwise the published scope already covers it.
        #
        # P8.4: "distinct from the published head" is NOT the same as "is a
        # submitted draft". `page_service._set_current_version` points
        # `latest_draft_version_id` at the author's still-private "editing"
        # row on every autosave, so the id comparison alone chunks and embeds
        # an unsubmitted body under content_scope="draft" -- confirmed by
        # reproducing it. `retrieval/semantic.py`'s `_to_result` currently
        # re-checks each hit's `source_version_id` against the per-actor
        # `visible_search_version_ids`, so this has not been shown reachable
        # as an actual search disclosure; that is a property of today's one
        # consumer, not of this projection, so it is excluded here the same
        # way `indexing/doc_source.py::_context_live_versions` already
        # excludes it for FTS.
        draft_id = page.latest_draft_version_id
        draft = None if draft_id is None else session.get(ContextPageVersion, draft_id)
        if (
            draft_id is not None
            and draft_id != page.current_published_version_id
            and draft is not None
            and draft.version_status == "draft"
        ):
            self._index_scope(
                session,
                workspace_id=page.workspace_id,
                document_kind="context_page",
                document_id=page.id,
                content_scope="draft",
                version_id=draft_id,
                title=page.title,
            )
        else:
            self._clear_scope(session, "context_page", page.id, "draft")

    def _index_scope(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        document_kind: str,
        document_id: UUID,
        content_scope: str,
        version_id: UUID | None,
        title: str,
    ) -> None:
        self._clear_scope(session, document_kind, document_id, content_scope)
        if version_id is None:
            return
        version = session.get(ContextPageVersion, version_id)
        if version is None or not (version.body_md or "").strip():
            return
        self._write_chunks(
            session,
            workspace_id=workspace_id,
            document_kind=document_kind,
            document_id=document_id,
            content_scope=content_scope,
            source_version_id=version_id,
            title=title,
            body=version.body_md,
            source_content_hash=version.content_hash,
        )

    # --- library ------------------------------------------------------------

    def _index_library_item(self, session: Session, item_id: UUID) -> None:
        # Serialize the canonical-pointer read with conversion promotion.  The
        # external embed call below may be slow, so PostgreSQL must retain the
        # item row lock until the chunk swap commits; SQLite takes its writer
        # lock before refreshing the pointer.  Without this ordering, an old
        # version's embed job can commit after a newer version was promoted.
        acquire_write_intent(session)
        item = session.scalar(
            select(LibraryItem)
            .where(LibraryItem.id == item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        self._clear_scope(session, "library_item", item_id, "published")
        if item is None:
            return
        if item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
            return
        version = get_canonical_library_version(session, item)
        if version is None:
            return
        effective = get_effective_library_markdown(session, version)
        artifact = get_canonical_markdown_artifact(session, version)
        body = (
            effective.body_markdown
            if effective is not None and effective.overlay is not None
            else (
                artifact.conversion_detail.index_text if artifact is not None else None
            )
        )
        if artifact is None or not (body or "").strip():
            return
        self._write_chunks(
            session,
            workspace_id=item.workspace_id,
            document_kind="library_item",
            document_id=item.id,
            content_scope="published",
            source_version_id=version.id,
            title=item.title or item.uri,
            body=body,
            source_content_hash=(
                effective.content_sha256
                if effective is not None and effective.overlay is not None
                else version.content_hash
            ),
        )

    # --- shared -------------------------------------------------------------

    def _clear_scope(
        self,
        session: Session,
        document_kind: str,
        document_id: UUID,
        content_scope: str,
    ) -> None:
        for chunk in session.scalars(
            select(ContentChunk).where(
                ContentChunk.document_kind == document_kind,
                ContentChunk.document_id == document_id,
                ContentChunk.content_scope == content_scope,
            )
        ).all():
            session.delete(chunk)
        session.flush()

    def _write_chunks(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        document_kind: str,
        document_id: UUID,
        content_scope: str,
        source_version_id: UUID | None,
        title: str,
        body: str,
        source_content_hash: str | None,
    ) -> None:
        chunks = chunk_markdown(body, title=title)
        if not chunks:
            return
        response = self.embedder.embed(
            EmbeddingRequest(texts=[embedding_input(chunk) for chunk in chunks])
        )
        for chunk, vector in zip(chunks, response.embeddings, strict=True):
            row = ContentChunk(
                workspace_id=workspace_id,
                document_kind=document_kind,
                document_id=document_id,
                content_scope=content_scope,
                source_version_id=source_version_id,
                ordinal=chunk.ordinal,
                heading_path=chunk.heading_path,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                chunk_text=chunk.text,
                chunk_text_hash=chunk_text_hash(chunk.text),
                token_count=chunk.token_count,
                source_content_hash=source_content_hash,
            )
            session.add(row)
            session.flush()
            self.vector_store.upsert(
                session=session,
                chunk_id=row.id,
                provider=response.provider,
                model=response.model,
                dim=response.dimensions,
                vector=vector,
            )

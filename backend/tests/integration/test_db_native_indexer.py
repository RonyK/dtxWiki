from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.llm.embedding import DeterministicEmbedding
from backend.models.chunk import ChunkEmbedding, ContentChunk
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.models.workspace import Workspace
from backend.schemas.read import SearchFilters
from backend.services.indexing import DbNativeIndexer
from backend.services.retrieval.semantic import SemanticRetriever
from backend.services.vector_store import BruteForceVectorStore

pytestmark = pytest.mark.integration


def _indexer():
    return DbNativeIndexer(
        embedder=DeterministicEmbedding(dim=64),
        vector_store=BruteForceVectorStore(),
    )


def _ws(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _page(session, workspace_id, slug, title):
    page = ContextPage(
        workspace_id=workspace_id, slug=slug, title=title, page_type="concept"
    )
    session.add(page)
    session.flush()
    return page


def _version(session, page_id, body, status, version_no=1):
    workspace_id = session.get(ContextPage, page_id).workspace_id
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page_id,
        version_no=version_no,
        body_md=body,
        content_hash=f"h{uuid4().hex[:8]}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="t",
        version_status=status,
    )
    session.add(version)
    session.flush()
    return version


def _published_texts(session, scope="published"):
    return {
        chunk.chunk_text
        for chunk in session.scalars(
            select(ContentChunk).where(ContentChunk.content_scope == scope)
        ).all()
    }


def _library_version(session, item, *, version_no, body):
    version = LibraryItemVersion(
        workspace_id=item.workspace_id,
        library_item_id=item.id,
        version_no=version_no,
        content_hash=f"h{uuid4().hex[:8]}",
        raw_storage_ref=f"inline://version-{version_no}",
        processing_status="ready",
    )
    session.add(version)
    session.flush()
    # session_factory (backend/tests/conftest.py) uses a bare create_engine()
    # with no PRAGMA foreign_keys=ON, so run_id/attempt_id/blob_id need not
    # reference other real rows -- only the NOT NULL/CHECK-satisfying columns
    # and the selected_conversion_attempt_id link (required by
    # list_canonical_library_artifacts/get_canonical_markdown_artifact now
    # that A-1 retired the nullable-attempt "belongs by default" shape).
    run = LibraryRun(
        workspace_id=item.workspace_id,
        library_item_version_id=version.id,
        stage="conversion",
        run_seq=1,
        reason="initial",
        status="completed",
        provider_chain_json=[],
        policy_revision="test",
        output_schema_version="1",
        idempotency_key=f"test:{version.id}",
    )
    run.conversion_detail = LibraryRunConversionDetail(
        error_fallback_matrix_json={},
        per_provider_budget_json={},
        total_budget_json={},
    )
    session.add(run)
    session.flush()
    attempt = LibraryAttempt(
        workspace_id=item.workspace_id,
        run_id=run.id,
        library_item_version_id=version.id,
        stage="conversion",
        provider_id="test.provider",
        provider_version="1",
        provider_spi_version="1",
        config_hash="test",
        input_sha256="0" * 64,
        output_schema_version="1",
        status="completed",
        chain_index=0,
    )
    attempt.conversion_detail = LibraryAttemptConversionDetail(
        manual_retry_disposition=None,
        disposition_reason=None,
    )
    session.add(attempt)
    session.flush()
    artifact = LibraryArtifact(
        workspace_id=item.workspace_id,
        library_item_version_id=version.id,
        attempt_id=attempt.id,
        stage="conversion",
        blob_workspace_id=item.workspace_id,
        blob_id=uuid4(),
        blob_epoch=1,
        role="markdown",
        kind="text",
        media_type="text/plain",
        content_sha256=("a" if version_no % 2 else "b") * 64,
        byte_size=len(body.encode("utf-8")),
    )
    artifact.conversion_detail = LibraryArtifactConversionDetail(
        is_complete=True,
        index_text=body,
    )
    session.add(artifact)
    session.flush()
    version.selected_conversion_attempt_id = attempt.id
    session.add(version)
    session.flush()
    return version


def test_index_context_page_published_and_draft_scopes(session_factory):
    indexer = _indexer()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _page(session, wsid, "doc", "Doc")
        published = _version(
            session,
            page.id,
            "# Doc\n\nPublished alpha content.",
            "published_snapshot",
            version_no=1,
        )
        draft = _version(
            session, page.id, "# Doc\n\nDraft beta content here.", "draft", version_no=2
        )
        page.current_published_version_id = published.id
        page.latest_draft_version_id = draft.id
        session.flush()

        indexer.index(session=session, source_kind="context_page", source_id=page.id)

        chunks = session.scalars(select(ContentChunk)).all()
        scopes = {chunk.content_scope for chunk in chunks}
        embeddings = session.scalars(select(ChunkEmbedding)).all()
    assert scopes == {"published", "draft"}
    assert len(embeddings) == len(chunks)
    assert all(chunk.heading_path for chunk in chunks)


def test_index_context_page_excludes_private_editing_row_from_draft_scope(
    session_factory,
):
    """P8.4: an autosave points `latest_draft_version_id` at the author's own
    still-private "editing" row, so "distinct from the published head" alone
    would chunk and embed an unsubmitted body under content_scope="draft"."""

    indexer = _indexer()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _page(session, wsid, "editing-doc", "Editing Doc")
        published = _version(
            session,
            page.id,
            "# Editing Doc\n\nPublished alpha content.",
            "published_snapshot",
            version_no=1,
        )
        editing = _version(
            session,
            page.id,
            "# Editing Doc\n\nUnsubmitted private body.",
            "editing",
            version_no=2,
        )
        # Exactly the pointer state page_service._set_current_version leaves
        # behind after an ordinary autosave on an open editing session.
        page.current_published_version_id = published.id
        page.current_version_id = editing.id
        page.latest_draft_version_id = editing.id
        session.flush()

        indexer.index(session=session, source_kind="context_page", source_id=page.id)

        chunks = session.scalars(select(ContentChunk)).all()
        scopes = {chunk.content_scope for chunk in chunks}
        texts = [chunk.chunk_text for chunk in chunks]
        embeddings = session.scalars(select(ChunkEmbedding)).all()
    assert scopes == {"published"}
    assert not any("Unsubmitted private body" in text for text in texts)
    assert len(embeddings) == len(chunks)


def test_reindex_swaps_published_chunks(session_factory):
    indexer = _indexer()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _page(session, wsid, "doc2", "Doc2")
        v1 = _version(
            session,
            page.id,
            "# Doc2\n\nOriginal content here.",
            "published_snapshot",
            version_no=1,
        )
        page.current_published_version_id = v1.id
        session.flush()
        indexer.index(session=session, source_kind="context_page", source_id=page.id)
        assert any("Original" in t for t in _published_texts(session))

        v1.version_status = "superseded_snapshot"
        session.flush()
        v2 = _version(
            session,
            page.id,
            "# Doc2\n\nCompletely replaced body.",
            "published_snapshot",
            version_no=2,
        )
        page.current_published_version_id = v2.id
        session.flush()
        indexer.index(session=session, source_kind="context_page", source_id=page.id)
        new_texts = _published_texts(session)
    assert any("replaced" in t for t in new_texts)
    assert not any("Original" in t for t in new_texts)  # stale chunks swapped out


def test_index_rejects_unknown_source_kind(session_factory):
    """L7: a typo'd source_kind must fail loudly, not silently index nothing."""
    indexer = _indexer()
    with session_factory.begin() as session:
        wsid = _ws(session)
        with pytest.raises(ValueError, match="unknown source_kind"):
            indexer.index(session=session, source_kind="contextpage", source_id=uuid4())
    _ = wsid


def test_remove_rejects_unknown_source_kind(session_factory):
    """I7 (Codex F4): remove() must validate source_kind like index(), not map
    an unknown kind to library_item and delete the wrong chunks."""
    indexer = _indexer()
    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="unknown source_kind"):
            indexer.remove(
                session=session, source_kind="contextpage", source_id=uuid4()
            )


def test_remove_clears_chunks_and_embeddings(session_factory):
    indexer = _indexer()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _page(session, wsid, "doc3", "Doc3")
        version = _version(
            session, page.id, "# Doc3\n\nSome content.", "published_snapshot"
        )
        page.current_published_version_id = version.id
        session.flush()
        indexer.index(session=session, source_kind="context_page", source_id=page.id)
        assert session.scalars(select(ContentChunk)).all()

        indexer.remove(session=session, source_kind="context_page", source_id=page.id)
        remaining_chunks = session.scalars(select(ContentChunk)).all()
        remaining_embeddings = session.scalars(select(ChunkEmbedding)).all()
    assert remaining_chunks == []
    assert remaining_embeddings == []


def test_library_index_refreshes_cached_current_ready_pointer(session_factory):
    indexer = _indexer()
    with session_factory.begin() as session:
        wsid = _ws(session)
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="markdown",
            uri=f"upload://{uuid4()}",
            title="Canonical refresh",
            lifecycle_status="ready",
        )
        session.add(item)
        session.flush()
        version_one = _library_version(
            session,
            item,
            version_no=1,
            body="# Old canonical\n\noldsemanticmarker",
        )
        version_two = _library_version(
            session,
            item,
            version_no=2,
            body="# New canonical\n\nnewsemanticmarker",
        )
        item.current_version_id = version_two.id
        item.current_ready_version_id = version_one.id
        item_id = item.id
        version_two_id = version_two.id

    stale_session = session_factory()
    try:
        cached = stale_session.get(LibraryItem, item_id)
        assert cached is not None
        assert cached.current_ready_version_id == version_one.id
        stale_session.commit()

        with session_factory.begin() as updater:
            current = updater.get(LibraryItem, item_id)
            assert current is not None
            current.current_ready_version_id = version_two_id

        # The indexer's locking SELECT uses populate_existing, so it must not
        # trust the v1 pointer retained in this session's identity map.
        indexer.index(
            session=stale_session,
            source_kind="library_item",
            source_id=item_id,
        )
        stale_session.commit()
    finally:
        stale_session.close()

    with session_factory.begin() as session:
        chunks = session.scalars(
            select(ContentChunk).where(ContentChunk.document_id == item_id)
        ).all()
    assert chunks
    assert {chunk.source_version_id for chunk in chunks} == {version_two_id}
    assert any("newsemanticmarker" in chunk.chunk_text for chunk in chunks)
    assert all("oldsemanticmarker" not in chunk.chunk_text for chunk in chunks)


def test_library_embed_job_after_archive_only_clears_projection(session_factory):
    indexer = _indexer()
    with session_factory.begin() as session:
        wsid = _ws(session)
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="markdown",
            uri=f"upload://{uuid4()}",
            title="Archived projection",
            lifecycle_status="ready",
        )
        session.add(item)
        session.flush()
        version = _library_version(
            session,
            item,
            version_no=1,
            body="# Archived\n\narchivedsemanticmarker",
        )
        item.current_version_id = version.id
        item.current_ready_version_id = version.id
        session.flush()
        indexer.index(
            session=session,
            source_kind="library_item",
            source_id=item.id,
        )
        assert session.scalars(
            select(ContentChunk).where(ContentChunk.document_id == item.id)
        ).all()

        item.lifecycle_status = "archived"
        session.flush()
        indexer.index(
            session=session,
            source_kind="library_item",
            source_id=item.id,
        )
        remaining = session.scalars(
            select(ContentChunk).where(ContentChunk.document_id == item.id)
        ).all()

    assert remaining == []


def test_semantic_retrieval_rejects_stale_library_version_chunks(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=64)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    semantic = SemanticRetriever(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="markdown",
            uri=f"upload://{uuid4()}",
            title="Stale semantic fence",
            lifecycle_status="ready",
        )
        session.add(item)
        session.flush()
        version_one = _library_version(
            session,
            item,
            version_no=1,
            body="# Old\n\nuniquestalesemanticmarker",
        )
        item.current_version_id = version_one.id
        item.current_ready_version_id = version_one.id
        session.flush()
        indexer.index(
            session=session,
            source_kind="library_item",
            source_id=item.id,
        )

        version_two = _library_version(
            session,
            item,
            version_no=2,
            body="# New\n\ncanonicalreplacementmarker",
        )
        item.current_version_id = version_two.id
        item.current_ready_version_id = version_two.id
        session.flush()
        hits = semantic.retrieve(
            session=session,
            workspace_id=wsid,
            query="uniquestalesemanticmarker",
            scope="all",
            filters=SearchFilters(),
            limit=10,
            actor=None,
        )

    assert hits == []

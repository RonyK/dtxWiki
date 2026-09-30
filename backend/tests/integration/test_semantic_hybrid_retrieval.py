import hashlib
import logging
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.settings import Settings
from backend.llm.embedding import DeterministicEmbedding
from backend.models.chunk import ContentChunk
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
from backend.policy.auth import ActorContext
from backend.schemas.read import SearchFilters
from backend.services.indexing import DbNativeIndexer
from backend.services.indexing.fts5 import sync_library_doc_sources
from backend.services.page_service import sync_page_projections
from backend.services.retrieval.hybrid import HybridRetriever
from backend.services.retrieval.keyword import KeywordRetriever
from backend.services.retrieval.semantic import SemanticRetriever
from backend.services.search_service import SearchService, build_search_service
from backend.services.vector_store import BruteForceVectorStore

pytestmark = pytest.mark.integration


def _actor(role="editor"):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _ws(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _publish(
    session, workspace_id, slug, title, body, scope="published", page_type="concept"
):
    page = ContextPage(
        workspace_id=workspace_id, slug=slug, title=title, page_type=page_type
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=body,
        content_hash=f"h{uuid4().hex[:8]}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="t",
        version_status="published_snapshot" if scope == "published" else "draft",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    if scope == "published":
        page.current_published_version_id = version.id
    else:
        page.latest_draft_version_id = version.id
    sync_page_projections(session, page=page, version=version)
    return page


def _seed_conversion_artifact(session, *, workspace_id, version, index_text, marker):
    """A-1: minimal real Run/Attempt/Artifact triple, selected as canonical.

    session_factory (backend/tests/conftest.py) uses a bare create_engine()
    with no PRAGMA foreign_keys=ON, so run_id/attempt_id/blob_id need not
    reference other real rows -- only the NOT NULL/CHECK-satisfying columns
    and the selected_conversion_attempt_id link (required by
    list_canonical_library_artifacts/get_canonical_markdown_artifact now
    that A-1 retired the nullable-attempt "belongs by default" shape) matter.
    """

    run = LibraryRun(
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        stage="conversion",
        run_seq=1,
        reason="initial",
        status="completed",
        provider_chain_json=[],
        policy_revision="test",
        output_schema_version="1",
        idempotency_key=f"test:{version.id}:{marker}",
    )
    run.conversion_detail = LibraryRunConversionDetail(
        error_fallback_matrix_json={},
        per_provider_budget_json={},
        total_budget_json={},
    )
    session.add(run)
    session.flush()
    attempt = LibraryAttempt(
        workspace_id=workspace_id,
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
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        attempt_id=attempt.id,
        stage="conversion",
        blob_workspace_id=workspace_id,
        blob_id=uuid4(),
        blob_epoch=1,
        role="markdown",
        kind="text",
        media_type="text/plain",
        content_sha256=hashlib.sha256(index_text.encode("utf-8")).hexdigest(),
        byte_size=len(index_text.encode("utf-8")),
    )
    artifact.conversion_detail = LibraryArtifactConversionDetail(
        is_complete=True,
        index_text=index_text,
    )
    session.add(artifact)
    session.flush()
    return attempt, artifact


def _publish_library(
    session,
    workspace_id,
    *,
    uri,
    title,
    body,
    trust_level="unknown",
    lifecycle_status="registered",
    connector_type="upload",
    format_hint="unknown",
):
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type=connector_type,
        uri=uri,
        title=title,
        trust_level=trust_level,
        lifecycle_status=lifecycle_status,
        format_hint=format_hint,
    )
    session.add(item)
    session.flush()
    version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash=f"h{uuid4().hex[:8]}",
        raw_storage_ref="inline://raw",
        processing_status="completed",
    )
    session.add(version)
    session.flush()
    item.current_version_id = version.id
    attempt, _artifact = _seed_conversion_artifact(
        session,
        workspace_id=workspace_id,
        version=version,
        index_text=body,
        marker="publish",
    )
    version.selected_conversion_attempt_id = attempt.id
    session.add(version)
    session.flush()
    sync_library_doc_sources(session, item)
    return item


def _retrieve(retriever, session, workspace_id, query, actor, *, limit=5):
    return retriever.retrieve(
        session=session,
        workspace_id=workspace_id,
        query=query,
        scope="all",
        filters=SearchFilters(),
        limit=limit,
        actor=actor,
    )


def _conversion_attempt(
    session,
    *,
    version: LibraryItemVersion,
    run: LibraryRun,
    chain_index: int,
) -> LibraryAttempt:
    attempt = LibraryAttempt(
        workspace_id=version.workspace_id,
        run_id=run.id,
        library_item_version_id=version.id,
        stage="conversion",
        provider_id=f"test.converter.{chain_index}",
        provider_version="1",
        provider_spi_version="1.0",
        config_hash=f"config-{chain_index}",
        input_sha256="a" * 64,
        output_schema_version="1",
        status="completed",
        chain_index=chain_index,
    )
    attempt.conversion_detail = LibraryAttemptConversionDetail(
        manual_retry_disposition=None,
        disposition_reason=None,
    )
    session.add(attempt)
    session.flush()
    return attempt


def _attempt_artifact(
    session,
    *,
    version: LibraryItemVersion,
    attempt: LibraryAttempt,
    marker: str,
) -> LibraryArtifact:
    artifact = LibraryArtifact(
        workspace_id=version.workspace_id,
        library_item_version_id=version.id,
        attempt_id=attempt.id,
        stage="conversion",
        blob_workspace_id=version.workspace_id,
        blob_id=uuid4(),
        blob_epoch=1,
        role="markdown",
        kind="markdown",
        media_type="text/markdown",
        content_sha256=("b" if attempt.chain_index == 0 else "c") * 64,
        byte_size=len(marker.encode("utf-8")),
        ordinal=0,
    )
    artifact.conversion_detail = LibraryArtifactConversionDetail(
        is_complete=True,
        index_text=f"# Converted\n\n{marker}",
    )
    session.add(artifact)
    session.flush()
    return artifact


def test_library_projections_keep_ready_a_when_latest_b_is_pending(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=64)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            uri="upload://ready-a-pending-b.md",
            title="Ready A Pending B",
            lifecycle_status="ready",
        )
        session.add(item)
        session.flush()
        ready_a = LibraryItemVersion(
            workspace_id=wsid,
            library_item_id=item.id,
            version_no=1,
            content_hash="ready-a",
            raw_storage_ref="inline://ready-a",
            processing_status="completed",
        )
        session.add(ready_a)
        session.flush()
        ready_attempt, ready_artifact = _seed_conversion_artifact(
            session,
            workspace_id=wsid,
            version=ready_a,
            index_text="canonicalreadymarker",
            marker="ready-a",
        )
        ready_a.selected_conversion_attempt_id = ready_attempt.id
        session.add(ready_a)
        item.current_version_id = ready_a.id
        item.current_ready_version_id = ready_a.id

        pending_b = LibraryItemVersion(
            workspace_id=wsid,
            library_item_id=item.id,
            version_no=2,
            content_hash="pending-b",
            raw_storage_ref="inline://pending-b",
            processing_status="converting",
        )
        session.add(pending_b)
        session.flush()
        # Deliberately no selected_conversion_attempt_id here: pending_b must
        # stay non-canonical (list_canonical_library_artifacts returns
        # nothing for a version with no selected attempt), matching what
        # this test asserts below.
        _seed_conversion_artifact(
            session,
            workspace_id=wsid,
            version=pending_b,
            index_text="pendingonlymarker",
            marker="pending-b",
        )
        item.current_version_id = pending_b.id
        session.flush()

        sync_library_doc_sources(session, item)
        indexer.index(session=session, source_kind="library_item", source_id=item.id)

        ready_hits = _retrieve(
            KeywordRetriever(), session, wsid, "canonicalreadymarker", _actor()
        )
        pending_hits = _retrieve(
            KeywordRetriever(), session, wsid, "pendingonlymarker", _actor()
        )
        chunks = session.scalars(
            select(ContentChunk).where(ContentChunk.document_id == item.id)
        ).all()

    assert [hit.id for hit in ready_hits] == [ready_artifact.id]
    assert pending_hits == []
    assert any("canonicalreadymarker" in chunk.chunk_text for chunk in chunks)
    assert all("pendingonlymarker" not in chunk.chunk_text for chunk in chunks)


def test_library_projections_exclude_unselected_attempt_artifacts(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=64)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            uri="upload://selected-attempt.md",
            title="Selected Attempt",
            lifecycle_status="ready",
        )
        session.add(item)
        session.flush()
        version = LibraryItemVersion(
            workspace_id=wsid,
            library_item_id=item.id,
            version_no=1,
            content_hash="selected-attempt",
            raw_storage_ref="inline://selected-attempt",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        run = LibraryRun(
            workspace_id=wsid,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=1,
            reason="initial",
            status="completed",
            provider_chain_json=[],
            policy_revision="test",
            output_schema_version="1",
            idempotency_key="selected-attempt",
        )
        run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json={},
            per_provider_budget_json={},
            total_budget_json={},
        )
        session.add(run)
        session.flush()
        selected_attempt = _conversion_attempt(
            session, version=version, run=run, chain_index=0
        )
        rejected_attempt = _conversion_attempt(
            session, version=version, run=run, chain_index=1
        )
        selected_artifact = _attempt_artifact(
            session,
            version=version,
            attempt=selected_attempt,
            marker="selectedattemptmarker",
        )
        _attempt_artifact(
            session,
            version=version,
            attempt=rejected_attempt,
            marker="rejectedattemptmarker",
        )
        version.selected_conversion_attempt_id = selected_attempt.id
        item.current_version_id = version.id
        item.current_ready_version_id = version.id
        session.flush()

        sync_library_doc_sources(session, item)
        indexer.index(session=session, source_kind="library_item", source_id=item.id)

        selected_hits = _retrieve(
            KeywordRetriever(), session, wsid, "selectedattemptmarker", _actor()
        )
        rejected_hits = _retrieve(
            KeywordRetriever(), session, wsid, "rejectedattemptmarker", _actor()
        )
        chunks = session.scalars(
            select(ContentChunk).where(ContentChunk.document_id == item.id)
        ).all()

    assert [hit.id for hit in selected_hits] == [selected_artifact.id]
    assert rejected_hits == []
    assert any("selectedattemptmarker" in chunk.chunk_text for chunk in chunks)
    assert all("rejectedattemptmarker" not in chunk.chunk_text for chunk in chunks)


def test_semantic_retriever_finds_relevant_concept(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    semantic = SemanticRetriever(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        alpha = _publish(
            session,
            wsid,
            "alpha",
            "Alpha",
            "# Alpha\n\nquantum entanglement physics superposition topic",
        )
        beta = _publish(
            session,
            wsid,
            "beta",
            "Beta",
            "# Beta\n\ncooking pasta recipe kitchen tomato",
        )
        indexer.index(session=session, source_kind="context_page", source_id=alpha.id)
        indexer.index(session=session, source_kind="context_page", source_id=beta.id)

        results = _retrieve(
            semantic, session, wsid, "quantum entanglement physics", _actor()
        )
    assert results
    assert results[0].id == alpha.id
    assert results[0].entity_ref["kind"] == "context_page"
    assert results[0].entity_ref["id"] == str(alpha.id)
    assert results[0].concept_path == "/context/alpha.md"
    assert results[0].heading_path
    assert results[0].source_ref["kind"] == "content_chunk"


def test_semantic_retriever_draft_visibility(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    semantic = SemanticRetriever(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        draft = _publish(
            session,
            wsid,
            "wip",
            "Wip",
            "# Wip\n\nconfidential draft alpha content secret",
            scope="draft",
        )
        indexer.index(session=session, source_kind="context_page", source_id=draft.id)

        editor_hits = _retrieve(
            semantic, session, wsid, "confidential draft alpha", _actor("editor")
        )
        viewer_hits = _retrieve(
            semantic, session, wsid, "confidential draft alpha", _actor("viewer")
        )
    assert any(h.concept_path == "/context/wip.md" for h in editor_hits)
    assert all(h.concept_path != "/context/wip.md" for h in viewer_hits)


def test_hybrid_fuses_keyword_and_semantic(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    hybrid = HybridRetriever(
        keyword=KeywordRetriever(),
        semantic=SemanticRetriever(embedder=embedder, vector_store=store),
    )

    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _publish(
            session,
            wsid,
            "needle",
            "Needle",
            "# Needle\n\nfindable needle keyword and semantic content",
        )
        indexer.index(session=session, source_kind="context_page", source_id=page.id)
        results = _retrieve(hybrid, session, wsid, "needle", _actor())
    # The page is found by both lanes and collapses to one boosted concept hit.
    paths = [r.concept_path for r in results]
    assert "/context/needle.md" in paths
    assert paths.count("/context/needle.md") == 1


def test_keyword_result_carries_outline_location_identity(session_factory):
    keyword = KeywordRetriever()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _publish(
            session,
            wsid,
            "keyword-location",
            "Keyword Location",
            "# Root\n\nintro\n\n## Keyword Section\n\nuniquelexicalmarker body",
        )
        results = _retrieve(
            keyword, session, wsid, "uniquelexicalmarker", _actor()
        )

    [result] = [item for item in results if item.id == page.id]
    assert result.heading_path == ["Root", "Keyword Section"]
    assert result.section_id == "keyword-section"
    assert result.version_id is not None
    assert result.source_ref == {
        "kind": "context_page_version",
        "id": str(result.version_id),
    }


def test_hybrid_keyword_representative_keeps_outline_location(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    hybrid = HybridRetriever(
        keyword=KeywordRetriever(),
        semantic=SemanticRetriever(embedder=embedder, vector_store=store),
    )
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _publish(
            session,
            wsid,
            "hybrid-location",
            "Hybrid Location",
            "# Root\n\nintro\n\n## Hybrid Section\n\nhybridlocationmarker body",
        )
        indexer.index(session=session, source_kind="context_page", source_id=page.id)
        results = _retrieve(
            hybrid, session, wsid, "hybridlocationmarker", _actor()
        )

    [result] = [item for item in results if item.id == page.id]
    assert result.source_ref["kind"] == "context_page_version"
    assert result.heading_path == ["Root", "Hybrid Section"]
    assert result.section_id == "hybrid-section"
    assert result.version_id is not None


def test_search_preserves_semantic_query_case_and_reports_fallback(session_factory):
    class RecordingEmbedding:
        def __init__(self):
            self.delegate = DeterministicEmbedding(dim=128)
            self.texts: list[str] = []

        def embed(self, request):
            self.texts.extend(request.texts)
            return self.delegate.embed(request)

    store = BruteForceVectorStore()
    embedder = RecordingEmbedding()
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    semantic = SemanticRetriever(embedder=embedder, vector_store=store)
    search_service = SearchService(retrievers=[semantic])

    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _publish(
            session,
            wsid,
            "semantic-target",
            "Semantic Target",
            "# Semantic Target\n\nMixedCaseNeedle appears in this body.",
        )
        indexer.index(
            session=session,
            source_kind="context_page",
            source_id=page.id,
        )
        embedder.texts.clear()
        results = search_service.search(
            session=session,
            workspace_id=wsid,
            query="  MixedCaseNeedle  ",
            scope="context_page",
            retrieval_mode="semantic",
            actor=_actor(),
        )

    assert embedder.texts == ["MixedCaseNeedle"]
    assert results.ranked_results
    assert results.tried_lanes == ["entity", "trigram", "semantic"]
    assert "semantic_fallback_used" in results.warnings
    with session_factory() as session:
        keyword_results = SearchService().search(
            session=session,
            workspace_id=wsid,
            query="MixedCaseNeedle",
            scope="context_page",
            retrieval_mode="keyword",
            actor=_actor(),
        )
    assert keyword_results.ranked_results
    assert "semantic_fallback_used" not in keyword_results.warnings


def test_hybrid_normalizes_only_its_keyword_subquery(session_factory):
    class RecordingRetriever:
        def __init__(self):
            self.queries: list[str] = []

        def supported_modes(self):
            return set()

        def retrieve(self, **kwargs):
            self.queries.append(kwargs["query"])
            return []

    keyword = RecordingRetriever()
    semantic = RecordingRetriever()
    hybrid = HybridRetriever(keyword=keyword, semantic=semantic)
    search_service = SearchService(retrievers=[hybrid])

    with session_factory.begin() as session:
        wsid = _ws(session)
        results = search_service.search(
            session=session,
            workspace_id=wsid,
            query="  MiXeDCase Token  ",
            scope="context_page",
            retrieval_mode="hybrid",
            actor=_actor(),
        )

    assert keyword.queries == ["mixedcase token"]
    assert semantic.queries == ["MiXeDCase Token"]
    assert "semantic_fallback_used" not in results.warnings


def test_semantic_excludes_archived_library_item(session_factory):
    """H3: semantic/hybrid must hide archived/hidden Library items (the index
    keeps their chunks because archiving enqueues no remove)."""
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    semantic = SemanticRetriever(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        item = _publish_library(
            session,
            wsid,
            uri="upload://archivable.md",
            title="Archivable",
            body="archivable special marker reference body content",
        )
        indexer.index(session=session, source_kind="library_item", source_id=item.id)

        query = "archivable special marker"
        before = _retrieve(semantic, session, wsid, query, _actor())
        assert any(r.type == "library_item" for r in before)

        # Archive in place — the chunk stays in the index; the query-time filter
        # must drop it.
        item.lifecycle_status = "archived"
        session.flush()
        after = _retrieve(semantic, session, wsid, query, _actor())
    assert all(r.type != "library_item" for r in after)


def test_semantic_excludes_archived_context_page_without_reindex(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    semantic = SemanticRetriever(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _publish(
            session,
            wsid,
            "archived-context",
            "Archived Context",
            "# Archived Context\n\nretained semantic marker body",
        )
        indexer.index(session=session, source_kind="context_page", source_id=page.id)
        assert _retrieve(semantic, session, wsid, "retained semantic marker", _actor())

        page.lifecycle_status = "archived"
        session.flush()
        after = _retrieve(semantic, session, wsid, "retained semantic marker", _actor())

    assert all(result.id != page.id for result in after)


def test_indexer_does_not_recreate_archived_context_chunks(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _publish(
            session,
            wsid,
            "archived-reindex",
            "Archived Reindex",
            "# Archived Reindex\n\nprojection should stay absent",
        )
        indexer.index(session=session, source_kind="context_page", source_id=page.id)
        assert (
            session.scalar(
                select(ContentChunk.id)
                .where(ContentChunk.document_id == page.id)
                .limit(1)
            )
            is not None
        )

        page.lifecycle_status = "archived"
        indexer.index(session=session, source_kind="context_page", source_id=page.id)

        assert (
            session.scalar(
                select(ContentChunk.id)
                .where(ContentChunk.document_id == page.id)
                .limit(1)
            )
            is None
        )


def test_keyword_relaxes_natural_language_questions_in_english_and_korean(
    session_factory,
):
    keyword = KeywordRetriever()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _publish(
            session,
            wsid,
            "rollback-window",
            "Rollback Window",
            "# Rollback Window\n\nThe rollback window is 30 minutes. "
            "롤백 기간은 30분입니다.",
        )
        english = _retrieve(
            keyword,
            session,
            wsid,
            "What is the rollback window?",
            _actor("viewer"),
        )
        korean = _retrieve(
            keyword,
            session,
            wsid,
            "롤백 기간은 얼마인가요?",
            _actor("viewer"),
        )

    assert any(result.id == page.id for result in english)
    assert any(result.id == page.id for result in korean)


def test_semantic_returns_full_window_for_pagination(session_factory):
    """L1: the retriever must not cap at ``limit`` — the dispatcher paginates,
    so a small limit must still yield the full top-K window."""
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    semantic = SemanticRetriever(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        for i in range(8):
            page = _publish(
                session,
                wsid,
                f"p{i}",
                f"P{i}",
                f"# P{i}\n\nshared findable concept token body {i}",
            )
            indexer.index(
                session=session, source_kind="context_page", source_id=page.id
            )
        results = _retrieve(
            semantic,
            session,
            wsid,
            "shared findable concept token",
            _actor(),
            limit=3,
        )
    # Pre-fix this was capped at 3; now the whole over-fetch window is returned.
    assert len(results) > 3


def test_keyword_hides_draft_context_from_viewer(session_factory):
    """L2: keyword context search must not leak draft bodies to non-draft actors."""
    keyword = KeywordRetriever()
    with session_factory.begin() as session:
        wsid = _ws(session)
        _publish(
            session,
            wsid,
            "wip2",
            "Wip2",
            "# Wip2\n\nsecret draft marker body text",
            scope="draft",
        )
        editor_hits = _retrieve(
            keyword, session, wsid, "secret draft marker", _actor("editor")
        )
        viewer_hits = _retrieve(
            keyword, session, wsid, "secret draft marker", _actor("viewer")
        )
        anon_hits = _retrieve(keyword, session, wsid, "secret draft marker", None)
    assert any(h.concept_path == "/context/wip2.md" for h in editor_hits)
    assert all(h.concept_path != "/context/wip2.md" for h in viewer_hits)
    assert all(h.concept_path != "/context/wip2.md" for h in anon_hits)


def test_semantic_warns_when_index_holds_only_a_different_model(session_factory):
    """M1: a model swap without reindex makes search_similar return nothing; the
    retriever must surface 'reindex required' rather than silently empty.

    Captures from the retriever's logger directly (not caplog) so the assertion
    is immune to global logging state left by other tests in a full-suite run.
    """
    store = BruteForceVectorStore()
    indexer = DbNativeIndexer(
        embedder=DeterministicEmbedding(dim=64, model="model-a"), vector_store=store
    )
    semantic = SemanticRetriever(
        embedder=DeterministicEmbedding(dim=64, model="model-b"), vector_store=store
    )

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger("dtxwiki.retrieval.semantic")
    # Make capture immune to global logging state left by other tests in a
    # full-suite run: clear the manager-level disable, re-enable this logger
    # (a CLI test's dictConfig(disable_existing_loggers=True) sets .disabled),
    # set its level, and attach a direct handler (bypasses propagation).
    logging.disable(logging.NOTSET)
    previous_level = log.level
    previous_disabled = log.disabled
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    log.disabled = False
    try:
        with session_factory.begin() as session:
            wsid = _ws(session)
            page = _publish(
                session,
                wsid,
                f"stale-{uuid4().hex[:8]}",
                "Doc",
                "# Doc\n\nfindable content body here",
            )
            indexer.index(
                session=session, source_kind="context_page", source_id=page.id
            )
            results = _retrieve(semantic, session, wsid, "findable content", _actor())
    finally:
        log.removeHandler(handler)
        log.setLevel(previous_level)
        log.disabled = previous_disabled

    assert results == []
    assert any("reindex" in record.getMessage().lower() for record in records)


def test_build_search_service_advertises_modes():
    graph_service = object()
    keyword_only = build_search_service(Settings(), graph_service=graph_service)
    assert keyword_only._supported_modes() == {"keyword"}
    assert keyword_only._graph_service is graph_service

    full = build_search_service(
        Settings(embedding_provider="deterministic", embedding_dim=64),
        graph_service=graph_service,
    )
    assert full._supported_modes() == {"keyword", "semantic", "hybrid"}
    assert full._graph_service is graph_service


# --- I1 (Codex F1): semantic/hybrid must honor the same user SearchFilters as
# keyword, or out-of-filter documents leak into search/hybrid/Ask. ---


def test_semantic_retrieval_applies_context_filters(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    semantic = SemanticRetriever(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        overview = _publish(
            session,
            wsid,
            "ov",
            "Overview",
            "# Overview\n\nshared confusable marker reference body content",
            page_type="overview",
        )
        concept = _publish(
            session,
            wsid,
            "co",
            "Concept",
            "# Concept\n\nshared confusable marker reference body content",
            page_type="concept",
        )
        indexer.index(
            session=session, source_kind="context_page", source_id=overview.id
        )
        indexer.index(session=session, source_kind="context_page", source_id=concept.id)
        hits = semantic.retrieve(
            session=session,
            workspace_id=wsid,
            query="shared confusable marker reference",
            scope="all",
            filters=SearchFilters(context_type=["overview"]),
            limit=10,
            actor=_actor(),
        )
    paths = [h.concept_path for h in hits]
    assert "/context/ov.md" in paths
    assert "/context/co.md" not in paths


def test_semantic_retrieval_applies_library_filters(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    semantic = SemanticRetriever(embedder=embedder, vector_store=store)

    with session_factory.begin() as session:
        wsid = _ws(session)
        upload = _publish_library(
            session,
            wsid,
            uri="upload://a.md",
            title="Uploaded",
            body="shared lib confusable marker reference body content",
            connector_type="upload",
        )
        web = _publish_library(
            session,
            wsid,
            uri="web://b",
            title="WebDoc",
            body="shared lib confusable marker reference body content",
            connector_type="web",
        )
        indexer.index(session=session, source_kind="library_item", source_id=upload.id)
        indexer.index(session=session, source_kind="library_item", source_id=web.id)
        hits = semantic.retrieve(
            session=session,
            workspace_id=wsid,
            query="shared lib confusable marker reference",
            scope="all",
            filters=SearchFilters(connector_type=["upload"]),
            limit=10,
            actor=_actor(),
        )
    titles = [h.title for h in hits if h.type == "library_item"]
    assert "Uploaded" in titles
    assert "WebDoc" not in titles


def test_hybrid_retrieval_does_not_leak_semantic_filter_misses(session_factory):
    store = BruteForceVectorStore()
    embedder = DeterministicEmbedding(dim=128)
    indexer = DbNativeIndexer(embedder=embedder, vector_store=store)
    hybrid = HybridRetriever(
        keyword=KeywordRetriever(),
        semantic=SemanticRetriever(embedder=embedder, vector_store=store),
    )

    with session_factory.begin() as session:
        wsid = _ws(session)
        overview = _publish(
            session,
            wsid,
            "h-ov",
            "HOverview",
            "# HOverview\n\nhybrid shared marker reference body content",
            page_type="overview",
        )
        concept = _publish(
            session,
            wsid,
            "h-co",
            "HConcept",
            "# HConcept\n\nhybrid shared marker reference body content",
            page_type="concept",
        )
        indexer.index(
            session=session, source_kind="context_page", source_id=overview.id
        )
        indexer.index(session=session, source_kind="context_page", source_id=concept.id)
        hits = hybrid.retrieve(
            session=session,
            workspace_id=wsid,
            query="hybrid shared marker reference",
            scope="all",
            filters=SearchFilters(context_type=["overview"]),
            limit=10,
            actor=_actor(),
        )
    paths = [h.concept_path for h in hits]
    assert "/context/h-ov.md" in paths
    # The concept page would leak through the semantic lane without the fix.
    assert "/context/h-co.md" not in paths


# --- I2 (Codex F2): services must be built from the app/server's resolved
# settings, not a module-global Settings() captured at import. ---


def test_create_app_settings_enable_semantic_search():
    from types import SimpleNamespace

    from backend.app.main import create_app
    from backend.app.state import get_search_service_from_app

    app = create_app(
        settings=Settings(embedding_provider="deterministic", embedding_dim=64)
    )
    service = get_search_service_from_app(SimpleNamespace(app=app))
    assert service._supported_modes() == {"keyword", "semantic", "hybrid"}


def test_mcp_search_prefers_injected_service_over_default():
    """The MCP search handler must use the SearchService carried by the
    ToolCallContext (built from the app's settings by the container), not a
    module-global default — the old lazily-built fallback regressed semantic
    apps to keyword-only (B-12)."""

    from contextlib import contextmanager
    from types import SimpleNamespace
    from uuid import uuid4

    from backend.app.container import (
        DatabaseProvider,
        ServiceRegistry,
        ToolCallContext,
    )
    from backend.mcp import read_tools
    from backend.policy.auth import ActorContext
    from backend.schemas.read import SearchPayload

    calls: list[dict] = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        return SearchPayload(
            ranked_results=[],
            supported_retrieval_modes=["keyword", "semantic", "hybrid"],
            permission_note=None,
            next_cursor=None,
        )

    @contextmanager
    def fake_session_factory():
        yield None

    ctx = ToolCallContext(
        actor=ActorContext(
            actor_kind="human",
            actor_id="search-injection-test",
            scopes=("*",),
            workspace_role="editor",
            role="editor",
        ),
        db=DatabaseProvider(None, session_factory=fake_session_factory),
        page_service=None,
        search_service=SimpleNamespace(search=fake_search),
        services=ServiceRegistry(),
    )
    output = read_tools.search({"workspace_id": str(uuid4()), "query": "needle"}, ctx)

    assert calls, "injected search service was not used"
    assert output.envelope.result_status == "succeeded"
    # Without an injected service the registry's settings-free keyword-only
    # fallback is used (contract-test convenience; production always injects).
    fallback = ServiceRegistry().keyword_only_search
    assert fallback._supported_modes() == {"keyword"}

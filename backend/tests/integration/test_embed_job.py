import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.llm.embedding import DeterministicEmbedding
from backend.models.base import Base
from backend.models.chunk import ChunkEmbedding, ContentChunk
from backend.models.context import ContextPage, ContextPageVersion
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.indexing import DbNativeIndexer
from backend.services.job_queue import JobQueueService
from backend.services.vector_store import BruteForceVectorStore
from backend.worker.runtime import OneShotWorker

pytestmark = pytest.mark.integration


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_embed_job_indexes_context_page():
    session_factory = make_session_factory()
    queue = JobQueueService()
    indexer = DbNativeIndexer(
        embedder=DeterministicEmbedding(dim=64),
        vector_store=BruteForceVectorStore(),
    )
    worker = OneShotWorker(job_queue=queue, indexer=indexer)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = ContextPage(
            workspace_id=workspace.id, slug="emb", title="Emb", page_type="concept"
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="# Emb\n\nIndexable content body.",
            content_hash="h1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="t",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_published_version_id = version.id
        session.flush()
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="embed",
            trigger_type="content_version",
            idempotency_key=f"context_page:{page.id}",
            target_ref={"kind": "context_page", "id": str(page.id)},
        )
        page_id = page.id
        workspace_id = workspace.id

    with session_factory.begin() as session:
        result = worker.dispatch_once(
            session=session, workspace_id=workspace_id, job_types={"embed"}
        )

    assert result is not None
    assert result.status == "completed"
    with session_factory.begin() as session:
        chunks = session.scalars(
            select(ContentChunk).where(ContentChunk.document_id == page_id)
        ).all()
        embeddings = session.scalars(select(ChunkEmbedding)).all()
    assert chunks
    assert all(chunk.content_scope == "published" for chunk in chunks)
    assert len(embeddings) == len(chunks)


def test_embed_job_missing_target_fails_terminally():
    """A malformed embed job (missing target_ref.id) must fail visibly, not
    complete as a silent skip that leaves the document unindexed (Codex F3)."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    indexer = DbNativeIndexer(
        embedder=DeterministicEmbedding(dim=64),
        vector_store=BruteForceVectorStore(),
    )
    worker = OneShotWorker(job_queue=queue, indexer=indexer)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="embed",
            trigger_type="content_version",
            idempotency_key="malformed-embed",
            target_ref={"kind": "context_page"},  # missing "id"
        )
        workspace_id = workspace.id

    with session_factory.begin() as session:
        result = worker.dispatch_once(
            session=session, workspace_id=workspace_id, job_types={"embed"}
        )
    assert result is not None
    assert result.status == "failed"
    assert "missing" in (result.detail.get("error") or "").lower()


def test_embed_job_noop_when_no_embedding_provider():
    # Default OneShotWorker (no injected indexer) builds from Settings, which
    # defaults to embedding_provider=none -> the job completes as a no-op.
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="embed",
            trigger_type="content_version",
            idempotency_key="noop",
            target_ref={"kind": "context_page", "id": str(workspace.id)},
        )
        workspace_id = workspace.id

    with session_factory.begin() as session:
        result = worker.dispatch_once(
            session=session, workspace_id=workspace_id, job_types={"embed"}
        )
    assert result is not None
    assert result.status == "completed"
    assert result.detail.get("skipped") == "embedding_provider=none"


def test_embed_job_records_that_semantic_indexing_was_skipped():
    """A misconfigured embedding provider must not look like no provider.

    `build_indexer` degrades so the job cannot fail terminally, but the job then
    indexes symbolically, reports plain success, and is never retried once the
    setting is fixed — leaving the semantic index permanently short of that
    document. The dispatch detail (echoed as JSON per job by the worker entry
    point) is what tells an operator which documents need reindexing.
    """

    from backend.app.container import DatabaseProvider
    from backend.app.settings import Settings
    from backend.services.indexing import build_indexer

    session_factory = make_session_factory()
    queue = JobQueueService()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="embed-degrade-secret",
        embedding_provider="openai_compatible",
        embedding_base_url=None,
    )
    indexer = build_indexer(
        settings, DatabaseProvider(settings, session_factory=session_factory)
    )
    # Symbolic indexing still runs; only the semantic lane dropped out.
    assert len(indexer.indexers) == 1
    worker = OneShotWorker(job_queue=queue, indexer=indexer)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="degraded",
            title="Degraded",
            page_type="concept",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="# Degraded\n\nSymbolically indexable body.",
            content_hash="h-degraded",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="t",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_published_version_id = version.id
        session.flush()
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="embed",
            trigger_type="content_version",
            idempotency_key=f"context_page:{page.id}",
            target_ref={"kind": "context_page", "id": str(page.id)},
        )
        workspace_id = workspace.id

    with session_factory.begin() as session:
        result = worker.dispatch_once(
            session=session, workspace_id=workspace_id, job_types={"embed"}
        )

    assert result is not None
    # It completes: retrying cannot repair a settings mistake.
    assert result.status == "completed"
    assert "DTXWIKI_EMBEDDING_BASE_URL" in str(result.detail.get("semantic_skipped"))
    # And it reaches the worker's output verbatim, which is where an operator
    # actually reads it.
    assert "semantic_skipped" in json.dumps(result.to_payload())

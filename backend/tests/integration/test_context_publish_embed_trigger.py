import pytest
from sqlalchemy import select

from backend.llm.embedding import DeterministicEmbedding
from backend.models.chunk import ContentChunk
from backend.models.job import Job
from backend.models.workspace import Workspace
from backend.schemas.context_write import (
    PublishContextPageRequest,
    ReviewDecisionRequest,
    WriteDraftRequest,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.indexing import DbNativeIndexer
from backend.services.review_service import ReviewService
from backend.services.vector_store import BruteForceVectorStore
from backend.worker.runtime import OneShotWorker

pytestmark = pytest.mark.integration

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _workspace_id(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _context_embed_jobs(session, page_id):
    return [
        job
        for job in session.scalars(select(Job).where(Job.job_type == "embed")).all()
        if job.target_ref.get("kind") == "context_page"
        and job.target_ref.get("id") == str(page_id)
    ]


def test_draft_and_publish_enqueue_context_embed_and_worker_indexes(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Searchable Page",
                page_type="overview",
                body_markdown="# Searchable Page\n\nFindable published content.",
                reason="seed draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page_id = created.page_id

        # Saving a draft enqueues an embed job (draft scope freshness).
        draft_jobs = _context_embed_jobs(session, page_id)
        assert len(draft_jobs) == 1
        assert draft_jobs[0].trigger_type == "context_draft"

        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        service.publish_draft(
            session=session,
            workspace_id=workspace_id,
            page_id=page_id,
            request=PublishContextPageRequest(
                version_id=created.draft_version_id,
                reason="publish",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

        # Publishing enqueues a distinct embed job (published marker) — the gap
        # this fixes: a published page is now auto-indexed without `reindex`.
        markers = {job.trigger_type for job in _context_embed_jobs(session, page_id)}
    assert "context_published" in markers

    # The worker dispatches the embed job and indexes the page's published scope.
    worker = OneShotWorker(
        indexer=DbNativeIndexer(
            embedder=DeterministicEmbedding(dim=64),
            vector_store=BruteForceVectorStore(),
        )
    )
    for _ in range(6):
        with session_factory.begin() as session:
            result = worker.dispatch_once(
                session=session, workspace_id=workspace_id, job_types={"embed"}
            )
        if result is None:
            break

    with session_factory() as session:
        chunks = session.scalars(
            select(ContentChunk).where(ContentChunk.document_id == page_id)
        ).all()
    assert chunks
    assert any(chunk.content_scope == "published" for chunk in chunks)


def test_publish_embed_is_a_noop_without_embedding_provider(session_factory):
    # The default OneShotWorker builds the indexer from Settings (provider=none),
    # so the enqueued embed job completes as a no-op — publish never fails.
    service = ContextWriteService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="No Provider Page",
                page_type="overview",
                body_markdown="# No Provider Page\n\nBody.",
                reason="seed",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page_id = created.page_id

    worker = OneShotWorker()
    with session_factory.begin() as session:
        result = worker.dispatch_once(
            session=session, workspace_id=workspace_id, job_types={"embed"}
        )
    assert result is not None
    assert result.status == "completed"
    with session_factory() as session:
        chunks = session.scalars(
            select(ContentChunk).where(ContentChunk.document_id == page_id)
        ).all()
    assert chunks == []

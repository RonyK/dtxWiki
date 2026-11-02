from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.container import DatabaseProvider
from backend.llm.embedding import DeterministicEmbedding
from backend.models.chunk import ContentChunk
from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.graph import CoAccessPair
from backend.models.workspace import Workspace
from backend.services.indexing import (
    CompositeIndexer,
    DbNativeIndexer,
    Fts5Indexer,
    reindex_workspace,
)
from backend.services.page_service import sync_page_projections
from backend.services.vector_store import BruteForceVectorStore

pytestmark = pytest.mark.integration


def test_reindex_workspace_indexes_published_pages(session_factory):
    provider = DatabaseProvider(None, session_factory=session_factory)
    indexer = CompositeIndexer(
        [
            Fts5Indexer(provider),
            DbNativeIndexer(
                embedder=DeterministicEmbedding(dim=64),
                vector_store=BruteForceVectorStore(),
                database_provider=provider,
            ),
        ]
    )
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
        pages = []
        versions = []
        for slug in ("one", "two"):
            page = ContextPage(
                workspace_id=workspace_id,
                slug=slug,
                title=slug.title(),
                page_type="concept",
            )
            session.add(page)
            session.flush()
            version = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page.id,
                version_no=1,
                body_md=f"# {slug}\n\nbody content for {slug}",
                content_hash=f"h{uuid4().hex[:8]}",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="t",
                version_status="published_snapshot",
            )
            session.add(version)
            session.flush()
            page.current_published_version_id = version.id
            session.flush()
            pages.append(page)
            versions.append(version)

        # This one rebuild restores both curated edges and usage edges. It
        # must not publish each producer's intermediate projection/revision.
        versions[0].body_md += f"\n\n[Two](/p/{workspace_id}/{pages[1].id})"
        source_id, target_id = sorted(page.id for page in pages)
        at = datetime(2026, 1, 1, tzinfo=UTC)
        session.add(
            CoAccessPair(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=source_id,
                target_kind="context_page",
                target_id=target_id,
                support_session_count=2,
                first_observed_at=at,
                occurred_at=at + timedelta(days=1),
            )
        )
        session.flush()
        before_revision = session.get(Workspace, workspace_id).edge_projection_version

    counts = reindex_workspace(provider, indexer, workspace_id)
    with session_factory() as session:
        chunks = session.scalars(select(ContentChunk)).all()
        edges = list(session.scalars(select(EdgeProjection)))
        assert {"wikilink", "co_access"} <= {edge.edge_kind for edge in edges}
        pair = session.scalar(select(CoAccessPair))
        assert pair.support_session_count == 2
        assert len([edge for edge in edges if edge.edge_kind == "co_access"]) == 1
        assert (
            session.get(Workspace, workspace_id).edge_projection_version
            == before_revision + 1
        )

    assert counts["context_pages"] == 2
    assert counts["edge_projections"] == len(edges)
    assert {chunk.document_kind for chunk in chunks} == {"context_page"}
    assert len(chunks) >= 2


def test_reindex_prunes_orphan_chunks(session_factory):
    """M4: deleting a document leaves polymorphic chunks behind (no FK cascade
    possible); reindex must reconcile them."""
    provider = DatabaseProvider(None, session_factory=session_factory)
    vector_indexer = DbNativeIndexer(
        embedder=DeterministicEmbedding(dim=64),
        vector_store=BruteForceVectorStore(),
        database_provider=provider,
    )
    indexer = CompositeIndexer(
        [
            Fts5Indexer(provider),
            vector_indexer,
        ]
    )
    from backend.models.chunk import ChunkEmbedding

    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
        page = ContextPage(
            workspace_id=workspace_id, slug="doomed", title="Doomed",
            page_type="concept",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id, version_no=1,
            body_md="# Doomed\n\nbody content to be orphaned",
            content_hash=f"h{uuid4().hex[:8]}", actor_type="human",
            created_by_actor_kind="human", created_by_actor_id="t",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_published_version_id = version.id
        session.flush()
        vector_indexer.index(
            session=session, source_kind="context_page", source_id=page.id
        )
        assert session.scalars(select(ContentChunk)).all()

        # Orphan the chunks: point them at a document id that no longer resolves
        # (the end-state a deleted page leaves, since document_id is polymorphic
        # and cannot FK-cascade). reindex must reconcile them away.
        orphan_doc_id = uuid4()
        orphan_chunk_ids = []
        for chunk in session.scalars(select(ContentChunk)).all():
            chunk.document_id = orphan_doc_id
            orphan_chunk_ids.append(chunk.id)
        session.flush()

    counts = reindex_workspace(provider, indexer, workspace_id)
    with session_factory() as session:
        orphan_remaining = session.scalars(
            select(ContentChunk).where(ContentChunk.document_id == orphan_doc_id)
        ).all()
        orphan_embeddings = session.scalars(
            select(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(orphan_chunk_ids))
        ).all()

    assert counts["pruned_orphans"] >= 1
    assert orphan_remaining == []
    assert orphan_embeddings == []  # cascaded with their chunks


def test_page_link_projection_keeps_published_and_draft_scopes(session_factory):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        target_a = ContextPage(
            workspace_id=workspace_id,
            slug="published-target",
            title="Published Target",
            page_type="concept",
        )
        target_b = ContextPage(
            workspace_id=workspace_id,
            slug="draft-target",
            title="Draft Target",
            page_type="concept",
        )
        page = ContextPage(
            workspace_id=workspace_id,
            slug="source-links",
            title="Source Links",
            page_type="concept",
        )
        session.add_all([target_a, target_b, page])
        session.flush()
        published = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="Published [[published-target]]",
            content_hash="published-links",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="t",
            version_status="published_snapshot",
        )
        draft = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=2,
            body_md="Draft [[draft-target]]",
            content_hash="draft-links",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="t",
            version_status="draft",
        )
        session.add_all([published, draft])
        session.flush()
        page.current_published_version_id = published.id
        page.current_version_id = published.id
        sync_page_projections(session, page=page, version=published)
        page.latest_draft_version_id = draft.id
        page.current_version_id = draft.id
        sync_page_projections(session, page=page, version=draft)

        links = session.scalars(
            select(PageLink).where(PageLink.source_page_id == page.id)
        ).all()

    assert {(link.source_version_id, link.target_slug) for link in links} == {
        (published.id, "published-target"),
        (draft.id, "draft-target"),
    }

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.chunk import ChunkEmbedding, ContentChunk
from backend.models.workspace import Workspace
from backend.services.vector_store import (
    BruteForceVectorStore,
    pack_vector,
    unpack_vector,
)
from backend.services.vector_store.bruteforce import cosine_similarity

pytestmark = pytest.mark.integration


def _ws(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _chunk(session, workspace_id, *, ordinal=0, scope="published", doc_id=None):
    chunk = ContentChunk(
        workspace_id=workspace_id,
        document_kind="context_page",
        document_id=doc_id or uuid4(),
        content_scope=scope,
        ordinal=ordinal,
        char_start=0,
        char_end=4,
        chunk_text="text",
        chunk_text_hash=f"h{uuid4().hex[:8]}",
        token_count=1,
    )
    session.add(chunk)
    session.flush()
    return chunk


def test_pack_unpack_and_cosine_roundtrip():
    vector = [0.1, 0.2, 0.3, -0.4]
    assert unpack_vector(pack_vector(vector)) == pytest.approx(vector, abs=1e-6)
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0


def test_upsert_and_search_returns_nearest_first(session_factory):
    store = BruteForceVectorStore()
    with session_factory.begin() as session:
        wsid = _ws(session)
        near = _chunk(session, wsid)
        far = _chunk(session, wsid)
        store.upsert(
            session=session,
            chunk_id=near.id,
            provider="local",
            model="m",
            dim=3,
            vector=[1.0, 0.0, 0.0],
        )
        store.upsert(
            session=session,
            chunk_id=far.id,
            provider="local",
            model="m",
            dim=3,
            vector=[0.0, 1.0, 0.0],
        )
        results = store.search_similar(
            session=session,
            workspace_id=wsid,
            query_vector=[0.9, 0.1, 0.0],
            provider="local",
            model="m",
            limit=10,
        )
    assert [cid for cid, _ in results] == [near.id, far.id]
    assert results[0][1] > results[1][1]


def test_upsert_is_idempotent_per_chunk_provider_model(session_factory):
    store = BruteForceVectorStore()
    with session_factory.begin() as session:
        wsid = _ws(session)
        chunk = _chunk(session, wsid)
        store.upsert(
            session=session,
            chunk_id=chunk.id,
            provider="local",
            model="m",
            dim=2,
            vector=[1.0, 0.0],
        )
        store.upsert(
            session=session,
            chunk_id=chunk.id,
            provider="local",
            model="m",
            dim=2,
            vector=[0.0, 1.0],
        )
        rows = session.scalars(
            select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunk.id)
        ).all()
    assert len(rows) == 1
    assert unpack_vector(rows[0].embedding) == pytest.approx([0.0, 1.0])


def test_upsert_rejects_dim_mismatch(session_factory):
    store = BruteForceVectorStore()
    with session_factory.begin() as session:
        wsid = _ws(session)
        chunk = _chunk(session, wsid)
        with pytest.raises(ValueError):
            store.upsert(
                session=session,
                chunk_id=chunk.id,
                provider="local",
                model="m",
                dim=4,
                vector=[1.0, 0.0],
            )


def test_search_filters_by_model_and_scope(session_factory):
    store = BruteForceVectorStore()
    with session_factory.begin() as session:
        wsid = _ws(session)
        pub = _chunk(session, wsid, scope="published")
        draft = _chunk(session, wsid, scope="draft")
        for chunk in (pub, draft):
            store.upsert(
                session=session,
                chunk_id=chunk.id,
                provider="local",
                model="m",
                dim=2,
                vector=[1.0, 0.0],
            )
        # A different model must not surface under model="m".
        store.upsert(
            session=session,
            chunk_id=pub.id,
            provider="local",
            model="other",
            dim=2,
            vector=[1.0, 0.0],
        )
        published_only = store.search_similar(
            session=session,
            workspace_id=wsid,
            query_vector=[1.0, 0.0],
            provider="local",
            model="m",
            limit=10,
            content_scopes={"published"},
        )
        all_scopes = store.search_similar(
            session=session,
            workspace_id=wsid,
            query_vector=[1.0, 0.0],
            provider="local",
            model="m",
            limit=10,
        )
    assert {cid for cid, _ in published_only} == {pub.id}
    assert {cid for cid, _ in all_scopes} == {pub.id, draft.id}


def test_delete_for_chunk(session_factory):
    store = BruteForceVectorStore()
    with session_factory.begin() as session:
        wsid = _ws(session)
        chunk = _chunk(session, wsid)
        store.upsert(
            session=session,
            chunk_id=chunk.id,
            provider="local",
            model="m",
            dim=2,
            vector=[1.0, 0.0],
        )
        store.delete_for_chunk(session=session, chunk_id=chunk.id)
        remaining = session.scalars(select(ChunkEmbedding)).all()
    assert remaining == []

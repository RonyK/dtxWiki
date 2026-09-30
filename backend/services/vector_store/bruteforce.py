"""BruteForceVectorStore — dependency-zero packed-float32 + pure-Python cosine.

Loads candidate embeddings for the active (provider, model) within a workspace
and ranks by cosine similarity in Python. No numpy / sqlite-vec / pgvector
dependency — the local-first default. Encapsulated behind VectorStore so an
ANN-accelerated backend can replace it without touching callers.
"""

from __future__ import annotations

import struct
from math import sqrt
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.models.chunk import ChunkEmbedding, ContentChunk


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (sqrt(norm_a) * sqrt(norm_b))


class BruteForceVectorStore:
    def upsert(
        self,
        *,
        session: Session,
        chunk_id: UUID,
        provider: str,
        model: str,
        dim: int,
        vector: list[float],
    ) -> None:
        if len(vector) != dim:
            raise ValueError(
                f"vector length {len(vector)} does not match declared dim {dim}"
            )
        existing = session.scalar(
            select(ChunkEmbedding).where(
                ChunkEmbedding.chunk_id == chunk_id,
                ChunkEmbedding.provider == provider,
                ChunkEmbedding.model == model,
            )
        )
        blob = pack_vector(vector)
        if existing is not None:
            existing.dim = dim
            existing.embedding = blob
        else:
            workspace_id = session.scalar(
                select(ContentChunk.workspace_id).where(ContentChunk.id == chunk_id)
            )
            if workspace_id is None:
                raise ValueError(f"content chunk {chunk_id} was not found")
            session.add(
                ChunkEmbedding(
                    workspace_id=workspace_id,
                    chunk_id=chunk_id,
                    provider=provider,
                    model=model,
                    dim=dim,
                    embedding=blob,
                )
            )
        session.flush()

    def delete_for_chunk(self, *, session: Session, chunk_id: UUID) -> None:
        session.execute(
            delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunk_id)
        )

    def search_similar(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        query_vector: list[float],
        provider: str,
        model: str,
        limit: int,
        content_scopes: set[str] | None = None,
    ) -> list[tuple[UUID, float]]:
        statement = (
            select(ChunkEmbedding.chunk_id, ChunkEmbedding.embedding)
            .join(ContentChunk, ChunkEmbedding.chunk_id == ContentChunk.id)
            .where(
                ContentChunk.workspace_id == workspace_id,
                ChunkEmbedding.provider == provider,
                ChunkEmbedding.model == model,
            )
        )
        if content_scopes is not None:
            statement = statement.where(
                ContentChunk.content_scope.in_(content_scopes)
            )
        scored: list[tuple[UUID, float]] = []
        for chunk_id, blob in session.execute(statement).all():
            scored.append(
                (chunk_id, cosine_similarity(query_vector, unpack_vector(blob)))
            )
        # Highest similarity first; chunk_id is a stable tiebreaker.
        scored.sort(key=lambda item: (-item[1], str(item[0])))
        return scored[:limit]

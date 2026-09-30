"""VectorStore protocol — the low-level vector persistence + similarity seam."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session


class VectorStore(Protocol):
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
        """Insert or replace the embedding for (chunk, provider, model)."""
        ...

    def delete_for_chunk(self, *, session: Session, chunk_id: UUID) -> None:
        """Drop all embeddings for a chunk (e.g. when the chunk is superseded)."""
        ...

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
        """Return up to ``limit`` (chunk_id, cosine_similarity) for the active
        (provider, model) within the workspace, highest similarity first.

        ``content_scopes`` restricts to published/draft scopes; per-actor
        restricted/draft authorization is applied by the caller (SemanticRetriever)
        after mapping chunks back to their source documents (Epic-1 §7.3-7.4).
        """
        ...

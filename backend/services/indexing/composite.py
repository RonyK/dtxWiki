"""Fan-out Indexer used by Tier-B and replaceable search-plane overlays."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from backend.services.retrieval.protocol import Indexer


class CompositeIndexer:
    def __init__(self, indexers: Sequence[Indexer]) -> None:
        self.indexers = list(indexers)

    def upsert(
        self,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        for indexer in self.indexers:
            indexer.upsert(
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
        for indexer in self.indexers:
            indexer.delete(
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
        combined: dict[str, int] = {}
        for indexer in self.indexers:
            counts = indexer.rebuild(
                workspace_id=workspace_id,
                source_kind=source_kind,
                source_id=source_id,
            )
            for key, value in counts.items():
                combined[key] = combined.get(key, 0) + value
        return combined

    def upsert_in_session(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        """Local worker bridge; not part of the frozen SPI contract."""

        for indexer in self.indexers:
            method = getattr(indexer, "upsert_in_session", None)
            if method is None:
                raise RuntimeError(
                    f"{type(indexer).__name__} cannot join an ambient transaction"
                )
            method(
                session,
                workspace_id=workspace_id,
                source_kind=source_kind,
                source_id=source_id,
            )

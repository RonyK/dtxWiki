"""Stable search-plane contracts shared by Tier-B and backend overlays."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from backend.spi.types import ActorContext, RankedResult, SearchFilters


class Retriever(Protocol):
    def supported_modes(self) -> set[str]:
        """Retrieval modes this implementation serves."""
        ...

    def retrieve(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        query: str,
        scope: str,
        filters: SearchFilters,
        limit: int,
        actor: ActorContext | None,
    ) -> list[RankedResult]:
        """Return a bounded candidate set for planner ranking/pagination.

        ``session`` is the source-of-truth data session for ``workspace_id``.
        Remote implementations may use it only to hydrate and enforce actor
        visibility after querying their own plane; every returned hit must pass
        the core visibility contract. Keyword mode must retain the bounded
        workflow sources served from this session.

        ``limit`` is a candidate budget, not the wire page size. An
        implementation may boundedly over-fetch to survive visibility/filter
        hydration, but must never materialize the whole corpus.

        Splitting workflow sources into another retriever is deferred to the
        Tier-S S1 hook-first change. Direct SearchService construction remains
        responsible for supplying its GraphService collaborator.
        """
        ...


class Indexer(Protocol):
    def upsert(
        self,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        """Create or replace one source in the configured index plane."""
        ...

    def delete(
        self,
        *,
        workspace_id: UUID,
        source_kind: str,
        source_id: UUID,
    ) -> None:
        """Drop all index rows for one source document."""
        ...

    def rebuild(
        self,
        *,
        workspace_id: UUID,
        source_kind: str | None = None,
        source_id: UUID | None = None,
    ) -> dict[str, int]:
        """Rebuild a workspace or narrowed source from source-of-truth."""
        ...

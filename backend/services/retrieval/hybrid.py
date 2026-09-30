"""HybridRetriever — Epic-1 Sprint-1 Phase 6.

Fuses the keyword and semantic retrievers with Reciprocal Rank Fusion so a
document surfaced by both is boosted. The dedup identity is the OKF concept, so
a page found document-level (keyword) and chunk-level (semantic) collapses to
one boosted hit. Per-actor authorization already happened inside each retriever.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from backend.policy.auth import ActorContext
from backend.schemas.read import RankedResult, SearchFilters
from backend.services.retrieval.fusion import (
    DEFAULT_RRF_K,
    ranked_result_key,
    reciprocal_rank_fusion,
)
from backend.services.retrieval.protocol import Retriever


class HybridRetriever:
    def __init__(
        self,
        *,
        keyword: Retriever,
        semantic: Retriever,
        k: int = DEFAULT_RRF_K,
    ) -> None:
        self.keyword = keyword
        self.semantic = semantic
        self.k = k

    def supported_modes(self) -> set[str]:
        return {"hybrid"}

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
        keyword_results = self.keyword.retrieve(
            session=session,
            workspace_id=workspace_id,
            query=query.lower(),
            scope=scope,
            filters=filters,
            limit=limit,
            actor=actor,
        )
        semantic_results = self.semantic.retrieve(
            session=session,
            workspace_id=workspace_id,
            query=query,
            scope=scope,
            filters=filters,
            limit=limit,
            actor=actor,
        )
        return reciprocal_rank_fusion(
            [keyword_results, semantic_results],
            key=ranked_result_key,
            k=self.k,
        )

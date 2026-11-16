"""Reciprocal Rank Fusion — Epic-1 Sprint-1 Phase 6.

Fuses ranked result lists (keyword + semantic) into one ranking without score
calibration: each item scores ``sum 1/(k + rank)`` across the lists it appears
in. Engine-neutral and tuning-light (one constant ``k``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from backend.schemas.read import RankedResult

DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    result_lists: Sequence[Sequence[RankedResult]],
    *,
    key: Callable[[RankedResult], str],
    k: int = DEFAULT_RRF_K,
) -> list[RankedResult]:
    scores: dict[str, float] = {}
    representative: dict[str, RankedResult] = {}
    for results in result_lists:
        for rank, item in enumerate(results):
            identity = key(item)
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (k + rank + 1)
            representative.setdefault(identity, item)
    fused = [
        representative[identity].model_copy(update={"score": score})
        for identity, score in scores.items()
    ]
    fused.sort(key=lambda result: (-result.score, str(result.id)))
    return fused


def ranked_result_document_key(result: RankedResult) -> str:
    """Identify canonical documents while keeping audit results independent."""
    from backend.services.entity_ref import ranked_result_entity_ref

    if result.type not in {
        "context_page",
        "context_claim",
        "library_item",
        "library_artifact",
        "agent_page",
    }:
        return f"{result.type}:{result.id}"
    ref = ranked_result_entity_ref(result)
    return f"{ref.kind}:{ref.id or result.id}"


def ranked_result_key(result: RankedResult) -> str:
    """Deduplicate matching Context versions without changing document identity."""
    document_key = ranked_result_document_key(result)
    if document_key.startswith("context_page:"):
        from backend.services.entity_ref import ranked_result_entity_ref

        version_id = ranked_result_entity_ref(result).version_id
        if version_id is not None:
            return f"{document_key}:{version_id}"
    return document_key

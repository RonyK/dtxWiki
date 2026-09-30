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


def ranked_result_key(result: RankedResult) -> str:
    """Dedup identity for fusion: the OKF concept (document) when present, so a
    document found by both keyword and semantic is boosted, not duplicated."""
    return result.concept_path or f"{result.type}:{result.id}"

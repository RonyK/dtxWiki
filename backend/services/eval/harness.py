"""Retrieval-mode and planner-lane evaluation harness (Epic-1 §6/§13).

Runs a golden query set through a SearchService across retrieval modes and
reports retrieval-only metrics (recall@k, MRR, hit-rate). Retrieval-only by
design: no LLM judge, no extra dependencies. Against a real embedding endpoint
the numbers measure semantic quality; against the deterministic provider they
validate the harness mechanics and the keyword tier.

This is the gate for ranking changes (reranker, query transforms): a change to
ranking is only justified when these numbers move on a golden set.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from backend.policy.auth import ActorContext
from backend.schemas.read import RankedResult, SearchPayload
from backend.services.eval.metrics import (
    hit_rate_at_k,
    recall_at_k,
    reciprocal_rank,
)

LANE_MATCH_REASONS = {
    "entity": frozenset({"exact_id", "exact_slug", "exact_title", "exact_alias"}),
    "trigram": frozenset({"trigram"}),
    "bm25": frozenset({"keyword"}),
    "associative": frozenset({"associative"}),
}
_BM25_RESULT_TYPES = frozenset(
    {
        "context_page",
        "context_claim",
        "library_item",
        "library_artifact",
        "agent_page",
    }
)


@dataclass(frozen=True)
class GoldenQuery:
    query: str
    expected: list[str]
    lane: str | None = None


@dataclass(frozen=True)
class ModeMetrics:
    mode: str
    queries: int
    recall_at_k: float
    mrr: float
    hit_rate: float


@dataclass(frozen=True)
class LaneMetrics:
    lane: str
    queries: int
    recall_at_k: float
    mrr: float
    hit_rate: float


@dataclass(frozen=True)
class Regression:
    scope: str
    name: str
    metric: str
    baseline: float
    current: float


def compare_baseline(
    mode_metrics: Sequence[ModeMetrics],
    lane_metrics: Sequence[LaneMetrics],
    baseline: object,
    *,
    default_threshold: float = 0.05,
) -> list[Regression]:
    """Return quality drops larger than the configured baseline threshold."""

    if not isinstance(baseline, dict):
        raise ValueError("eval baseline must be a JSON object")
    threshold = float(baseline.get("threshold", default_threshold))
    if threshold < 0:
        raise ValueError("eval baseline threshold must be non-negative")
    current = {
        "modes": {item.mode: item for item in mode_metrics},
        "lanes": {item.lane: item for item in lane_metrics},
    }
    regressions: list[Regression] = []
    for scope, name_key in (("modes", "mode"), ("lanes", "lane")):
        expected_scope = baseline.get(scope, {})
        if not isinstance(expected_scope, dict):
            raise ValueError(f"eval baseline '{scope}' must be an object")
        for name, expected in expected_scope.items():
            if not isinstance(expected, dict):
                raise ValueError(f"eval baseline {scope}.{name} must be an object")
            actual = current[scope].get(name)
            if actual is None:
                regressions.append(Regression(scope, str(name), "missing", 1.0, 0.0))
                continue
            for metric in ("recall_at_k", "mrr", "hit_rate"):
                if metric not in expected:
                    continue
                baseline_value = float(expected[metric])
                current_value = float(getattr(actual, metric))
                if baseline_value - current_value > threshold + 1e-9:
                    regressions.append(
                        Regression(
                            scope=scope,
                            name=str(getattr(actual, name_key)),
                            metric=metric,
                            baseline=baseline_value,
                            current=current_value,
                        )
                    )
    return regressions


def load_golden(raw: object) -> list[GoldenQuery]:
    """Parse a golden set: a JSON list of ``{"query": str, "expected": [str]}``."""
    if not isinstance(raw, list):
        raise ValueError("golden set must be a JSON list of query objects")
    golden: list[GoldenQuery] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or "query" not in entry:
            raise ValueError(f"golden entry {index} must have a 'query' field")
        expected = entry.get("expected", [])
        if not isinstance(expected, list):
            raise ValueError(f"golden entry {index} 'expected' must be a list")
        lane = entry.get("lane")
        if lane is not None and lane not in LANE_MATCH_REASONS:
            supported = ", ".join(LANE_MATCH_REASONS)
            raise ValueError(f"golden entry {index} lane must be one of: {supported}")
        golden.append(
            GoldenQuery(
                query=str(entry["query"]),
                expected=[str(item) for item in expected],
                lane=str(lane) if lane is not None else None,
            )
        )
    return golden


def result_identifier(result: RankedResult) -> str:
    """Identifier used to match a hit against the golden set: the OKF concept
    pointer when present, else a stable ``type:id`` fallback."""
    return result.concept_path or f"{result.type}:{result.id}"


def _predicted_identifiers(payload: SearchPayload) -> list[str]:
    return [result_identifier(result) for result in payload.ranked_results]


def evaluate(
    search_service,
    session: Session,
    workspace_id: UUID,
    golden: Sequence[GoldenQuery],
    *,
    modes: Sequence[str],
    k: int = 8,
    actor: ActorContext | None = None,
) -> list[ModeMetrics]:
    """Score the golden set across the given retrieval modes."""
    metrics: list[ModeMetrics] = []
    query_count = len(golden)
    divisor = query_count or 1
    for mode in modes:
        recall_sum = mrr_sum = hit_sum = 0.0
        for item in golden:
            payload = search_service.search(
                session=session,
                workspace_id=workspace_id,
                query=item.query,
                retrieval_mode=mode,
                limit=k,
                actor=actor,
            )
            predicted = _predicted_identifiers(payload)
            recall_sum += recall_at_k(predicted, item.expected, k)
            mrr_sum += reciprocal_rank(predicted, item.expected)
            hit_sum += hit_rate_at_k(predicted, item.expected, k)
        metrics.append(
            ModeMetrics(
                mode=mode,
                queries=query_count,
                recall_at_k=recall_sum / divisor,
                mrr=mrr_sum / divisor,
                hit_rate=hit_sum / divisor,
            )
        )
    return metrics


def evaluate_lanes(
    search_service,
    session: Session,
    workspace_id: UUID,
    golden: Sequence[GoldenQuery],
    *,
    k: int = 8,
    actor: ActorContext | None = None,
) -> list[LaneMetrics]:
    """Score lane-tagged golden queries through the keyword planner path."""

    metrics: list[LaneMetrics] = []
    for lane in LANE_MATCH_REASONS:
        lane_golden = [item for item in golden if item.lane == lane]
        if not lane_golden:
            continue
        recall_sum = mrr_sum = hit_sum = 0.0
        for item in lane_golden:
            payload = search_service.search(
                session=session,
                workspace_id=workspace_id,
                query=item.query,
                retrieval_mode="keyword",
                limit=k,
                actor=actor,
            )
            predicted = [
                result_identifier(result)
                for result in payload.ranked_results
                if _result_belongs_to_lane(result, lane)
            ]
            recall_sum += recall_at_k(predicted, item.expected, k)
            mrr_sum += reciprocal_rank(predicted, item.expected)
            hit_sum += hit_rate_at_k(predicted, item.expected, k)
        divisor = len(lane_golden)
        metrics.append(
            LaneMetrics(
                lane=lane,
                queries=divisor,
                recall_at_k=recall_sum / divisor,
                mrr=mrr_sum / divisor,
                hit_rate=hit_sum / divisor,
            )
        )
    return metrics


def evaluate_profile(
    search_service,
    session: Session,
    workspace_id: UUID,
    golden: Sequence[GoldenQuery],
    *,
    modes: Sequence[str],
    k: int = 8,
    actor: ActorContext | None = None,
) -> tuple[list[ModeMetrics], list[LaneMetrics]]:
    """Run the single CLI/CI profile used by the regression gate.

    Associative-tagged cases measure that planner lane only; including them in
    every retrieval-mode aggregate makes a keyword backend fail for a feature
    the mode does not own.
    """

    mode_golden = [item for item in golden if item.lane != "associative"]
    return (
        evaluate(
            search_service,
            session,
            workspace_id,
            mode_golden,
            modes=modes,
            k=k,
            actor=actor,
        ),
        evaluate_lanes(
            search_service,
            session,
            workspace_id,
            golden,
            k=k,
            actor=actor,
        ),
    )


def requires_associative_lane(golden: Sequence[GoldenQuery]) -> bool:
    return any(item.lane == "associative" for item in golden)


def _result_belongs_to_lane(result: RankedResult, lane: str) -> bool:
    if result.match_reason not in LANE_MATCH_REASONS[lane]:
        return False
    return lane != "bm25" or result.type in _BM25_RESULT_TYPES

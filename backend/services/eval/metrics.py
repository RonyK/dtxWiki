"""Retrieval-quality metrics for the RAG evaluation harness (Epic-1 §6).

Pure functions over a rank-ordered list of predicted identifiers and a set of
expected (relevant) identifiers — no I/O, no dependencies. ``predicted`` is the
retriever's output in rank order (best first); ``expected`` is the golden set.
"""

from collections.abc import Collection, Sequence


def recall_at_k(
    predicted: Sequence[str], expected: Collection[str], k: int
) -> float:
    """Fraction of the expected items present in the top-k predictions."""
    expected_set = set(expected)
    if not expected_set:
        return 0.0
    top_k = list(predicted)[:k]
    found = sum(1 for item in expected_set if item in top_k)
    return found / len(expected_set)


def reciprocal_rank(predicted: Sequence[str], expected: Collection[str]) -> float:
    """1/rank of the first relevant prediction (0.0 when none is relevant)."""
    expected_set = set(expected)
    for rank, item in enumerate(predicted, start=1):
        if item in expected_set:
            return 1.0 / rank
    return 0.0


def hit_rate_at_k(
    predicted: Sequence[str], expected: Collection[str], k: int
) -> float:
    """1.0 when any expected item appears in the top-k predictions, else 0.0."""
    expected_set = set(expected)
    return (
        1.0
        if any(item in expected_set for item in list(predicted)[:k])
        else 0.0
    )

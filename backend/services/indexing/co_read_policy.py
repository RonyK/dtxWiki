"""Shared bounded pairing policy, independent of projection storage adapters."""

from collections.abc import Iterable, Iterator
from datetime import timedelta

from backend.services.indexing.co_read import (
    CoReadPair,
    Read,
    co_read_pairs,
    max_pairs_per_segment,
)

CO_ACCESS_MAX_GAP = timedelta(minutes=30)
CO_ACCESS_ADJACENCY_WINDOW = 5
CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT = 12
CO_ACCESS_MAX_PAIRS_PER_SEGMENT = max_pairs_per_segment(
    window=CO_ACCESS_ADJACENCY_WINDOW,
    max_per_segment=CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT,
)
CO_ACCESS_SOURCE_BATCH_ROWS = 5_000
CO_ACCESS_MIN_SESSIONS = 2


def co_access_pairs(reads: Iterable[Read]) -> Iterator[CoReadPair]:
    """Use the same segmented-neighbor policy for graph and hot-path signals."""
    return co_read_pairs(
        reads,
        max_gap=CO_ACCESS_MAX_GAP,
        window=CO_ACCESS_ADJACENCY_WINDOW,
        max_per_segment=CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT,
    )

"""Bounded, serializable state for appending one conversation's body views.

Completed segments are already reflected in the producer's pair counters.
This cursor retains only the active segment; it never retains historical
conversation pairs or replays the conversation's earlier body views.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from backend.services.indexing.co_read import EntityRef, Read, _Element, _pair_elements
from backend.services.indexing.co_read_policy import (
    CO_ACCESS_ADJACENCY_WINDOW,
    CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT,
    CO_ACCESS_MAX_GAP,
)

PairSpans = dict[tuple[EntityRef, EntityRef], tuple[datetime, datetime]]


def _segment_spans(runs: list[_Element]) -> PairSpans:
    spans = {}
    # Use the exact same pairing primitive as full source replay. The list
    # passed here has at most twelve runs, regardless of conversation length.
    _pair_elements(runs, window=CO_ACCESS_ADJACENCY_WINDOW, into=spans)
    return {key: (span[0], span[1]) for key, span in spans.items()}


def _decode_state(
    state: dict,
) -> tuple[int, tuple[datetime, UUID, UUID], list[_Element]]:
    try:
        if state["version"] != 1 or type(state["segment"]) is not int:
            raise ValueError("unsupported co-read cursor")
        segment = state["segment"]
        encoded_key, encoded_runs = state["last_key"], state["runs"]
        if (
            segment < 0
            or not isinstance(encoded_key, list)
            or len(encoded_key) != 3
            or not isinstance(encoded_runs, list)
            or not 1 <= len(encoded_runs) <= CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT
        ):
            raise ValueError("invalid co-read cursor bounds")
        key = (
            datetime.fromisoformat(encoded_key[0]),
            UUID(encoded_key[1]),
            UUID(encoded_key[2]),
        )
        runs = []
        for encoded in encoded_runs:
            if len(encoded) != 4 or not isinstance(encoded[0], str):
                raise ValueError("invalid co-read cursor run")
            run = _Element(
                entity=(encoded[0], UUID(encoded[1])),
                first_at=datetime.fromisoformat(encoded[2]),
                last_at=datetime.fromisoformat(encoded[3]),
            )
            if run.first_at > run.last_at or run.last_at > key[0]:
                raise ValueError("invalid co-read cursor span")
            runs.append(run)
        return segment, key, runs
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError("invalid co-read cursor") from exc


def advance_co_read_state(
    state: dict | None, read: Read
) -> tuple[dict, PairSpans, int, int]:
    """Append one later view, returning changed spans and its segment/position.

    A repeated entity occupies its existing final run position. A time gap
    or a thirteenth run starts a new segment without retracting any completed
    segment's contributions. The caller handles an out-of-order event through
    its explicit recovery path; this function never mutates its input state.
    """
    key = (read.occurred_at, read.mcp_tool_call_id, read.artifact_id)
    if state is None:
        segment, runs = 0, []
    else:
        segment, previous_key, runs = _decode_state(state)
        if key <= previous_key:
            raise ValueError("co-read cursor requires increasing read order")
        if read.occurred_at - previous_key[0] > CO_ACCESS_MAX_GAP:
            segment += 1
            runs = []

    if (
        len(runs) == CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT
        and runs[-1].entity != read.entity
    ):
        segment += 1
        runs = []

    before = _segment_spans(runs)
    if runs and runs[-1].entity == read.entity:
        runs[-1].last_at = read.occurred_at
    else:
        runs.append(_Element(read.entity, read.occurred_at, read.occurred_at))
    after = _segment_spans(runs)
    changed = {pair: span for pair, span in after.items() if before.get(pair) != span}
    updated = {
        "version": 1,
        "segment": segment,
        "last_key": [
            read.occurred_at.isoformat(),
            str(read.mcp_tool_call_id),
            str(read.artifact_id),
        ],
        "runs": [
            [
                run.entity[0],
                str(run.entity[1]),
                run.first_at.isoformat(),
                run.last_at.isoformat(),
            ]
            for run in runs
        ],
    }
    return updated, changed, segment, len(runs) - 1

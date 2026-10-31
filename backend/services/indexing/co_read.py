"""Pair the documents one MCP conversation read close together.

Both usage signals that say "these two were read together" -- the co_access
graph edge and the hot-path lint finding -- pair reads through this one
function, so the two cannot disagree about what "together" means.

"Together" is a statement about reads adjacent in time, not about a session.
A session is cut into segments wherever two consecutive reads are more than
``max_gap`` apart, so a document read at 09:00 and another read at 17:00 are
never joined. A segment longer than ``max_per_segment`` distinct reads is
split front to back into sub-segments of that size; nothing is dropped, so
the first document of a long session pairs just like the last one. Inside a
sub-segment each read pairs with the next ``window - 1`` reads, which bounds
a session's pairs linearly in its length instead of quadratically.

The function consumes its input as a stream. It holds one session at a time:
the open sub-segment, the session's read count per document, and the pairs it
has already seen in that session. Nothing from an earlier session is kept.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

EntityRef = tuple[str, UUID]


@dataclass(frozen=True, slots=True)
class Read:
    """One focused read of one artifact, from the per-call edge."""

    session_id: UUID
    entity: EntityRef
    occurred_at: datetime
    mcp_tool_call_id: UUID
    artifact_id: UUID


@dataclass(frozen=True, slots=True)
class CoReadPair:
    """One pair read together in one session, emitted once per session.

    ``left``/``right`` are in canonical order. ``first_pair_at`` and
    ``last_pair_at`` are the first and last instants the session completed
    the pair; both are kept because the last one cannot recover the first.
    ``left_reads``/``right_reads`` count every read of each document in the
    whole session, not in one segment.
    """

    left: EntityRef
    right: EntityRef
    session_id: UUID
    first_pair_at: datetime
    last_pair_at: datetime
    left_reads: int
    right_reads: int


@dataclass(slots=True)
class _Element:
    """A run of consecutive reads of one entity inside a segment."""

    entity: EntityRef
    first_at: datetime
    last_at: datetime


def max_pairs_per_segment(*, window: int, max_per_segment: int) -> int:
    """The most pairs one full sub-segment can produce."""

    return sum(
        min(window - 1, max_per_segment - 1 - index) for index in range(max_per_segment)
    )


def co_read_pairs(
    reads: Iterable[Read],
    *,
    max_gap: timedelta,
    window: int,
    max_per_segment: int,
) -> Iterator[CoReadPair]:
    """Yield each pair read together, once per (pair, session).

    ``reads`` must arrive grouped by session and, inside a session, ordered by
    ``(occurred_at, mcp_tool_call_id, artifact_id)``. Both ids are part of the
    order so reads sharing an instant keep the order the calls were made in
    rather than an order invented from entity names. Input that breaks either
    rule raises ``ValueError``: the caller's ORDER BY is the read order, and a
    wrong one would silently pair the wrong documents.
    """

    if window < 2:
        raise ValueError("window must pair a read with at least one neighbour")
    if max_per_segment < 2:
        raise ValueError("max_per_segment must hold at least two reads")
    closed_sessions: set[UUID] = set()
    session_id: UUID | None = None
    previous_key: tuple[datetime, UUID, UUID] | None = None
    current: list[_Element] = []
    pairs: dict[tuple[EntityRef, EntityRef], list[datetime]] = {}
    read_counts: dict[EntityRef, int] = {}

    for read in reads:
        key = (read.occurred_at, read.mcp_tool_call_id, read.artifact_id)
        if read.session_id != session_id:
            if read.session_id in closed_sessions:
                raise ValueError("reads must be grouped by session")
            if session_id is not None:
                _pair_elements(current, window=window, into=pairs)
                yield from _session_pairs(session_id, pairs, read_counts)
                closed_sessions.add(session_id)
            session_id = read.session_id
            current = []
            pairs = {}
            read_counts = {}
        else:
            assert previous_key is not None
            if key < previous_key:
                raise ValueError(
                    "reads must be ordered by occurred_at, tool call, artifact"
                )
            if read.occurred_at - previous_key[0] > max_gap:
                _pair_elements(current, window=window, into=pairs)
                current = []
        previous_key = key
        read_counts[read.entity] = read_counts.get(read.entity, 0) + 1

        if current and current[-1].entity == read.entity:
            current[-1].last_at = read.occurred_at
            continue
        if len(current) == max_per_segment:
            _pair_elements(current, window=window, into=pairs)
            current = []
        current.append(_Element(read.entity, read.occurred_at, read.occurred_at))

    if session_id is not None:
        _pair_elements(current, window=window, into=pairs)
        yield from _session_pairs(session_id, pairs, read_counts)


def _pair_elements(
    elements: list[_Element],
    *,
    window: int,
    into: dict[tuple[EntityRef, EntityRef], list[datetime]],
) -> None:
    for index, earlier in enumerate(elements):
        for later in elements[index + 1 : index + window]:
            if later.entity == earlier.entity:
                continue
            # Tuple order on (kind, UUID) is the order of (kind, str(UUID)):
            # a UUID's text is fixed-width lowercase hex, and UUIDs compare
            # by that same 128-bit value.
            pair = (
                (earlier.entity, later.entity)
                if earlier.entity < later.entity
                else (later.entity, earlier.entity)
            )
            # The pair is complete once the later read happens, and stays
            # observed for as long as that run of reads continues.
            span = into.get(pair)
            if span is None:
                into[pair] = [later.first_at, later.last_at]
            else:
                if later.first_at < span[0]:
                    span[0] = later.first_at
                if later.last_at > span[1]:
                    span[1] = later.last_at


def _session_pairs(
    session_id: UUID,
    pairs: dict[tuple[EntityRef, EntityRef], list[datetime]],
    read_counts: dict[EntityRef, int],
) -> Iterator[CoReadPair]:
    for (left, right), (first_at, last_at) in pairs.items():
        yield CoReadPair(
            left=left,
            right=right,
            session_id=session_id,
            first_pair_at=first_at,
            last_pair_at=last_at,
            left_reads=read_counts[left],
            right_reads=read_counts[right],
        )

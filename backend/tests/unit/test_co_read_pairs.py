from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from backend.services.indexing.co_read import (
    CoReadPair,
    Read,
    co_read_pairs,
    max_pairs_per_segment,
)
from backend.services.indexing.edge_registry import (
    CO_ACCESS_ADJACENCY_WINDOW,
    CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT,
    CO_ACCESS_MAX_GAP,
    CO_ACCESS_MAX_PAIRS_PER_SEGMENT,
)

pytestmark = pytest.mark.unit

NINE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def _ref(name: str) -> tuple[str, UUID]:
    return ("context_page", uuid5(NAMESPACE_URL, f"co-read/{name}"))


def _reads(session_id: UUID, timeline: list[tuple[str, datetime]]) -> list[Read]:
    """One read per entry; call ids ascend with the list, like real calls."""

    return [
        Read(
            session_id=session_id,
            entity=_ref(name),
            occurred_at=at,
            mcp_tool_call_id=UUID(int=index + 1),
            artifact_id=UUID(int=index + 1),
        )
        for index, (name, at) in enumerate(timeline)
    ]


def _pairs(reads: list[Read]) -> list[CoReadPair]:
    return list(
        co_read_pairs(
            reads,
            max_gap=CO_ACCESS_MAX_GAP,
            window=CO_ACCESS_ADJACENCY_WINDOW,
            max_per_segment=CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT,
        )
    )


def _names(pairs: list[CoReadPair], names: list[str]) -> set[frozenset[str]]:
    by_ref = {_ref(name): name for name in names}
    return {frozenset((by_ref[pair.left], by_ref[pair.right])) for pair in pairs}


def test_reads_hours_apart_are_not_read_together():
    """(a) 09:00 A, 09:05 B, 17:00 C: only A and B were read together."""

    reads = _reads(
        uuid4(),
        [
            ("A", NINE),
            ("B", NINE + timedelta(minutes=5)),
            ("C", NINE + timedelta(hours=8)),
        ],
    )

    assert _names(_pairs(reads), ["A", "B", "C"]) == {frozenset({"A", "B"})}


def test_a_long_session_keeps_its_earliest_reads():
    """(b) Thirty reads a minute apart: the first document still pairs.

    The old cap kept the twelve most recent artifacts of a session whose
    reads all counted once, so its opening reads never became an edge.
    """

    names = [f"doc-{index}" for index in range(30)]
    reads = _reads(
        uuid4(),
        [(name, NINE + timedelta(minutes=index)) for index, name in enumerate(names)],
    )

    pairs = _names(_pairs(reads), names)

    assert frozenset({"doc-0", "doc-1"}) in pairs
    assert {name for pair in pairs for name in pair} == set(names)


def test_pairs_grow_linearly_with_session_length():
    """(c) 200 reads stay at or under four pairs per read, not C(200, 2)."""

    names = [f"doc-{index}" for index in range(200)]
    reads = _reads(
        uuid4(),
        [(name, NINE + timedelta(minutes=index)) for index, name in enumerate(names)],
    )

    pairs = _pairs(reads)

    assert len(pairs) <= 200 * (CO_ACCESS_ADJACENCY_WINDOW - 1)
    full_segments, rest = divmod(200, CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT)
    assert len(pairs) == full_segments * CO_ACCESS_MAX_PAIRS_PER_SEGMENT + (
        max_pairs_per_segment(window=CO_ACCESS_ADJACENCY_WINDOW, max_per_segment=rest)
    )


def test_returning_to_a_document_pairs_once_and_never_with_itself():
    """(e) A-B-A is one (A, B) pair spanning both completions, and no A-A."""

    session_id = uuid4()
    reads = _reads(
        session_id,
        [
            ("A", NINE),
            ("B", NINE + timedelta(minutes=1)),
            ("A", NINE + timedelta(minutes=2)),
        ],
    )

    pairs = _pairs(reads)

    assert len(pairs) == 1
    assert {pairs[0].left, pairs[0].right} == {_ref("A"), _ref("B")}
    assert pairs[0].session_id == session_id
    assert pairs[0].first_pair_at == NINE + timedelta(minutes=1)
    assert pairs[0].last_pair_at == NINE + timedelta(minutes=2)


def test_repeated_reads_of_one_document_count_as_one_position():
    """A document read five times in a row does not push its neighbours apart."""

    timeline = [("A", NINE)]
    timeline += [("B", NINE + timedelta(minutes=minute)) for minute in range(1, 6)]
    timeline += [
        (name, NINE + timedelta(minutes=6 + index))
        for index, name in enumerate(("C", "D", "E"))
    ]

    pairs = _names(_pairs(_reads(uuid4(), timeline)), ["A", "B", "C", "D", "E"])

    # A's window of five is A, B, C, D, E -- not A and five copies of B.
    assert frozenset({"A", "E"}) in pairs


def test_a_gap_longer_than_the_limit_starts_a_new_segment():
    at_limit = _reads(uuid4(), [("A", NINE), ("B", NINE + CO_ACCESS_MAX_GAP)])
    past_limit = _reads(
        uuid4(),
        [("A", NINE), ("B", NINE + CO_ACCESS_MAX_GAP + timedelta(seconds=1))],
    )

    assert len(_pairs(at_limit)) == 1
    assert _pairs(past_limit) == []


def test_a_long_segment_is_split_front_to_back_without_dropping_reads():
    names = [f"doc-{index}" for index in range(CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT + 1)]
    reads = _reads(
        uuid4(),
        [(name, NINE + timedelta(minutes=index)) for index, name in enumerate(names)],
    )

    pairs = _names(_pairs(reads), names)

    # The first sub-segment is full; the extra read starts a second one and
    # has nothing to pair with inside it.
    assert len(pairs) == CO_ACCESS_MAX_PAIRS_PER_SEGMENT
    assert frozenset({"doc-0", "doc-1"}) in pairs
    assert all(names[-1] not in pair for pair in pairs)


def test_one_pair_is_emitted_once_per_session_with_its_first_and_last_instant():
    first, second = uuid4(), uuid4()
    reads = _reads(
        first,
        [
            ("A", NINE),
            ("B", NINE + timedelta(minutes=1)),
            ("A", NINE + timedelta(hours=3)),
            ("B", NINE + timedelta(hours=3, minutes=1)),
        ],
    ) + _reads(second, [("A", NINE), ("B", NINE + timedelta(minutes=2))])

    spans = [
        (pair.session_id, pair.first_pair_at, pair.last_pair_at)
        for pair in _pairs(reads)
    ]

    assert spans == [
        (first, NINE + timedelta(minutes=1), NINE + timedelta(hours=3, minutes=1)),
        (second, NINE + timedelta(minutes=2), NINE + timedelta(minutes=2)),
    ]


def test_read_counts_cover_the_whole_session_not_one_segment():
    """A document's read count is the session's, across every segment."""

    reads = _reads(
        uuid4(),
        [
            ("A", NINE),
            ("B", NINE + timedelta(minutes=1)),
            ("A", NINE + timedelta(hours=3)),
            ("A", NINE + timedelta(hours=3, minutes=1)),
        ],
    )

    pairs = _pairs(reads)

    assert len(pairs) == 1
    assert {
        pairs[0].left: pairs[0].left_reads,
        pairs[0].right: pairs[0].right_reads,
    } == {_ref("A"): 3, _ref("B"): 1}


def test_a_session_is_paired_before_the_next_one_is_read():
    """Only one session is held at a time, so a long history streams."""

    first, second, third = UUID(int=1), UUID(int=2), UUID(int=3)
    source = [
        *_reads(first, [("A", NINE), ("B", NINE + timedelta(minutes=1))]),
        *_reads(second, [("C", NINE), ("D", NINE + timedelta(minutes=1))]),
        *_reads(third, [("E", NINE), ("F", NINE + timedelta(minutes=1))]),
    ]
    consumed: list[Read] = []

    def stream():
        for read in source:
            consumed.append(read)
            yield read

    pairs = co_read_pairs(
        stream(),
        max_gap=CO_ACCESS_MAX_GAP,
        window=CO_ACCESS_ADJACENCY_WINDOW,
        max_per_segment=CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT,
    )

    assert next(pairs).session_id == first
    # The first session and the one read that proved it had ended.
    assert len(consumed) == 3


def test_reads_sharing_an_instant_keep_the_call_order():
    """Same-instant reads are ordered by call, not by the documents' names."""

    session_id = uuid4()
    names = ["A", "B", "C", "D", "E", "F"]
    reads = [
        Read(
            session_id=session_id,
            entity=_ref(name),
            occurred_at=NINE,
            mcp_tool_call_id=UUID(int=index + 1),
            artifact_id=uuid4(),
        )
        for index, name in enumerate(names)
    ]

    pairs = _names(_pairs(reads), names)

    assert frozenset({"A", "E"}) in pairs
    assert frozenset({"A", "F"}) not in pairs


@pytest.mark.parametrize(
    "reads",
    [
        pytest.param(
            [
                *_reads(UUID(int=1), [("A", NINE + timedelta(minutes=1))]),
                *_reads(UUID(int=1), [("B", NINE)]),
            ],
            id="out-of-order-in-session",
        ),
        pytest.param(
            [
                *_reads(UUID(int=1), [("A", NINE)]),
                *_reads(UUID(int=2), [("B", NINE)]),
                *_reads(UUID(int=1), [("C", NINE + timedelta(minutes=1))]),
            ],
            id="session-not-grouped",
        ),
    ],
)
def test_input_that_is_not_in_read_order_is_rejected(reads):
    with pytest.raises(ValueError):
        _pairs(reads)


def test_the_documented_segment_ceiling_matches_the_constants():
    assert CO_ACCESS_MAX_PAIRS_PER_SEGMENT == max_pairs_per_segment(
        window=CO_ACCESS_ADJACENCY_WINDOW,
        max_per_segment=CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT,
    )

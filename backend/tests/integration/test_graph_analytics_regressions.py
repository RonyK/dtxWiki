import json
import os
import struct
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, insert, select

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.chunk import ChunkEmbedding, ContentChunk
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.graph import KnowledgeNodeStat
from backend.models.library import LibraryItem
from backend.models.lint import LintFinding, LintRun
from backend.models.mcp_chat import (
    McpChatArtifactLink,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.schemas.read import GraphNeighborEntry, GraphNeighborsPayload
from backend.services import graph_ppr, graph_rank
from backend.services.eval.harness import (
    LaneMetrics,
    ModeMetrics,
    compare_baseline,
)
from backend.services.graph_cache import bump_edge_version, edge_version
from backend.services.graph_ppr import _frontier_edges, _subgraph_cache, subgraph_ppr
from backend.services.graph_service import GraphService
from backend.services.indexing import edge_registry
from backend.services.indexing.edge_registry import (
    CO_ACCESS_ADJACENCY_WINDOW,
    CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT,
    CO_ACCESS_MAX_PAIRS_PER_SEGMENT,
    CO_ACCESS_MIN_SESSIONS,
    refresh_co_access_edge_projection,
)
from backend.services.librarian import _stage_misplaced_pages, run_librarian_tick
from backend.services.page_service import PageService
from backend.services.search_service import SearchService
from backend.services.trail_analysis import (
    HOT_PATH_MIN_SESSIONS,
    _authority_stat_rows,
    _context_pair_session_counts,
    list_hot_concepts,
    recompute_authority,
    run_trail_tick,
)

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _elapsed(call) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


@contextmanager
def _captured_statements(session):
    """Every statement the engine runs inside the block, with its parameters."""

    statements: list[tuple[str, tuple]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _bound_limit(statement: str, parameters: tuple) -> int | None:
    """The LIMIT bound into a captured SQLite statement, or None without one."""

    if statement.rstrip().endswith("LIMIT ? OFFSET ?"):
        return parameters[-2]
    return None


def _publish(service: PageService, slug: str, title: str):
    return service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=f"# {title}\n\nBody.",
        actor_kind="human",
        actor_id="sprint6-c2",
        actor_provenance="browser_session",
    )


def _actor(role="viewer"):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _new_chat_session(
    session, workspace_id, *, occurred_at, name_hint="c2", owner_user_id=None
):
    client = MCPClient(
        workspace_id=workspace_id,
        name=f"{name_hint}-{uuid4()}",
        client_type="generic",
        scopes=[],
        status="active",
    )
    session.add(client)
    session.flush()
    chat = McpChatSession(
        workspace_id=workspace_id,
        client_id=client.id,
        mcp_session_id=str(uuid4()),
        started_at=occurred_at,
        last_activity_at=occurred_at,
        owner_user_id=owner_user_id,
    )
    session.add(chat)
    session.flush()
    return client, chat


def _new_user(session):
    user = User(
        email=f"co-access-{uuid4()}@example.test",
        password_hash="x",
        display_name="Co Access Reader",
    )
    session.add(user)
    session.flush()
    return user.id


def _seed_touches(
    session,
    workspace_id,
    chat,
    client,
    resource_id,
    *,
    occurred_at,
    verb="referenced",
    count=1,
    resource_kind="context_page",
    tool_name="read",
    step=timedelta(0),
):
    """Record ``count`` touches of one artifact the way trace capture does.

    Every tool call writes one per-call edge row (what co_access pairs by read
    order) and the (session, artifact, verb) rollup keeps the count and the
    first/last instant (what hot concepts and hot paths aggregate). Seeding
    both keeps the two projections reading one consistent history.
    """

    last_call_id = None
    for index in range(count):
        tool_call = MCPToolCall(
            client_id=client.id,
            workspace_id=workspace_id,
            request_id=uuid4(),
            tool_name=tool_name,
            result_status="success",
        )
        session.add(tool_call)
        session.flush()
        session.add(
            McpChatToolCallArtifact(
                workspace_id=workspace_id,
                chat_session_id=chat.id,
                mcp_tool_call_id=tool_call.id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                verb=verb,
                occurred_at=occurred_at + step * index,
            )
        )
        last_call_id = tool_call.id
    session.add(
        McpChatArtifactLink(
            workspace_id=workspace_id,
            chat_session_id=chat.id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            verb=verb,
            first_occurred_at=occurred_at,
            last_occurred_at=occurred_at + step * (count - 1),
            occurrence_count=count,
            last_mcp_tool_call_id=last_call_id,
        )
    )
    session.flush()


def _seed_chat_pair(
    session,
    workspace_id,
    left_id,
    right_id,
    *,
    occurred_at,
    verbs=("referenced", "referenced"),
    tool_name="read",
    owner_user_id=None,
):
    client, chat = _new_chat_session(
        session, workspace_id, occurred_at=occurred_at, owner_user_id=owner_user_id
    )
    for page_id, verb in zip((left_id, right_id), verbs, strict=True):
        _seed_touches(
            session,
            workspace_id,
            chat,
            client,
            page_id,
            occurred_at=occurred_at,
            verb=verb,
            tool_name=tool_name,
        )


def _seed_chat_artifacts(
    session,
    workspace_id,
    artifacts,
    *,
    occurred_at,
    verb="referenced",
    step=timedelta(minutes=1),
):
    """Read each artifact ``count`` times in list order, ``step`` apart.

    Every read gets its own instant, ending at ``occurred_at``. Reads that
    all shared one instant would be ordered by their random call ids, and a
    test built on them keeps passing when the time key is dropped from the
    read order.
    """

    client, chat = _new_chat_session(
        session, workspace_id, occurred_at=occurred_at, name_hint="c2-many"
    )
    cursor = occurred_at - step * (sum(count for _, count in artifacts) - 1)
    for page_id, count in artifacts:
        # One focused read is one per-call row, so an occurrence count of N
        # is N calls rather than only a stored counter.
        _seed_touches(
            session,
            workspace_id,
            chat,
            client,
            page_id,
            occurred_at=cursor,
            verb=verb,
            count=count,
            step=step,
        )
        cursor += step * count


def _seed_read_sequence(session, workspace_id, page_ids, *, occurred_at):
    """Read page_ids in order inside one new chat session, one minute apart."""

    client, chat = _new_chat_session(
        session, workspace_id, occurred_at=occurred_at, name_hint="c2-window"
    )
    for offset, page_id in enumerate(page_ids):
        _seed_touches(
            session,
            workspace_id,
            chat,
            client,
            page_id,
            occurred_at=occurred_at + timedelta(minutes=offset),
        )


def test_co_access_rebuild_decays_and_discards_session_identity(
    session_factory, page_service
):
    left = _publish(page_service, "co-left", "Co Left")
    right = _publish(page_service, "co-right", "Co Right")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_chat_pair(session, workspace_id, left.id, right.id, occurred_at=now)
        _seed_chat_pair(
            session,
            workspace_id,
            left.id,
            right.id,
            occurred_at=now - timedelta(days=30),
        )
        before = edge_version(session, workspace_id)
        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 1
        edge = session.scalar(
            select(EdgeProjection).where(EdgeProjection.edge_kind == "co_access")
        )
        assert edge is not None
        assert edge.weight == pytest.approx(1.5, rel=0.02)
        assert edge.provenance == "usage"
        assert edge.source_ref == {"table": "mcp_chat_tool_call_artifact"}
        assert "session" not in json.dumps(edge.source_ref).lower()
        assert edge_version(session, workspace_id) > before


def test_co_access_projection_decay_reads_the_rank_table_half_life(
    session_factory, page_service, monkeypatch
):
    """The co_access half-life has one source: the rank table.

    rank_score never decays co_access because projection already did; the
    two only agree if projection reads the same value.
    """

    monkeypatch.setitem(
        graph_rank.RANK_PARAMS, "co_access", graph_rank.RankParam(0.20, 25, 15)
    )
    left = _publish(page_service, "co-halflife-left", "Co Half-life Left")
    right = _publish(page_service, "co-halflife-right", "Co Half-life Right")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_chat_pair(session, workspace_id, left.id, right.id, occurred_at=now)
        _seed_chat_pair(
            session,
            workspace_id,
            left.id,
            right.id,
            occurred_at=now - timedelta(days=30),
        )
        refresh_co_access_edge_projection(session, workspace_id, now=now)
        edge = session.scalar(
            select(EdgeProjection).where(EdgeProjection.edge_kind == "co_access")
        )

    # One fresh session (1.0) plus one 30-day-old session at a 15-day
    # half-life (0.25); the literal 30 would give 1.5.
    assert edge.weight == pytest.approx(1.25, rel=0.02)


@pytest.mark.parametrize("discovery_tool", ["search", "grep_wiki"])
def test_co_access_ignores_result_lists_from_discovery_tools(
    session_factory, page_service, discovery_tool
):
    """A ranked result list is not evidence that two documents were read.

    Trace capture records search and grep_wiki hits with the search_match
    exposure verb (tests/static/test_mcp_chat_verb_classes.py pins that), and
    co_access reads only consumption verbs off the per-call edge. The same two
    pages, in the same number of sessions, produce a co-access edge when they
    were read and none when they merely appeared in a result list.
    """

    left = _publish(page_service, f"disc-left-{discovery_tool}", "Discovery Left")
    right = _publish(page_service, f"disc-right-{discovery_tool}", "Discovery Right")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for _ in range(CO_ACCESS_MIN_SESSIONS):
            _seed_chat_pair(
                session,
                workspace_id,
                left.id,
                right.id,
                occurred_at=now,
                verbs=("search_match", "search_match"),
                tool_name=discovery_tool,
            )
        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 0

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for _ in range(CO_ACCESS_MIN_SESSIONS):
            _seed_chat_pair(
                session,
                workspace_id,
                left.id,
                right.id,
                occurred_at=now,
                tool_name="read",
            )
        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 1


def test_co_access_builds_from_one_person_reading_across_conversations(
    session_factory,
):
    """A solo workspace still gets a map, and repetition still raises it.

    The support threshold is about evidence, not about how many people
    supplied it: one person returning to the same pair across conversations
    must both cross the threshold and score higher each time they do.
    """

    now = datetime.now(UTC)
    left_id, right_id = uuid4(), uuid4()
    weights: list[float] = []
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        reader = _new_user(session)
        for _ in range(4):
            _seed_chat_pair(
                session,
                workspace_id,
                left_id,
                right_id,
                occurred_at=now,
                owner_user_id=reader,
            )
            refresh_co_access_edge_projection(session, workspace_id, now=now)
            edge = session.scalar(
                select(EdgeProjection).where(EdgeProjection.edge_kind == "co_access")
            )
            weights.append(edge.weight if edge is not None else 0.0)

    # One conversation is below the threshold; every conversation after it
    # both keeps the edge and strengthens it.
    assert weights[0] == 0.0
    assert weights[1] > 0.0
    assert weights == sorted(weights)
    assert weights[-1] > weights[1]


def test_co_access_requires_support_from_more_than_one_session(session_factory):
    """One conversation reading two documents together is the weakest signal.

    The threshold counts sessions, not people, and exists to keep the
    single-observation long tail out of the projection. Returning to the same
    two documents in a second conversation is what turns the pair into an
    edge -- the same person doing so is exactly the case it admits, since a
    solo workspace must still build a map.
    """

    # Deliberately literal. Deriving the expectation from
    # CO_ACCESS_MIN_SESSIONS would make the test restate the constant instead
    # of the behaviour, and it would keep passing if the threshold were
    # lowered to 1 -- the exact regression it exists to catch.
    assert CO_ACCESS_MIN_SESSIONS > 1
    now = datetime.now(UTC)
    left_id, right_id = uuid4(), uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_chat_pair(session, workspace_id, left_id, right_id, occurred_at=now)
        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 0

        _seed_chat_pair(session, workspace_id, left_id, right_id, occurred_at=now)
        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 1


def test_co_access_joins_only_reads_adjacent_in_the_session(session_factory):
    """Distance in the read order decides co-access, not session membership.

    Under the old whole-session clique the first and last document of a long
    session were joined however unrelated they were. Read
    CO_ACCESS_ADJACENCY_WINDOW + 1 documents in order: the neighbours pair,
    and the two ends -- same session, opposite ends -- do not.
    """

    now = datetime.now(UTC)
    read_count = CO_ACCESS_ADJACENCY_WINDOW + 1
    page_ids = [uuid4() for _ in range(read_count)]
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for _ in range(CO_ACCESS_MIN_SESSIONS):
            _seed_read_sequence(session, workspace_id, page_ids, occurred_at=now)
        refresh_co_access_edge_projection(session, workspace_id, now=now)
        pairs = {
            frozenset((source_id, target_id))
            for source_id, target_id in session.execute(
                select(EdgeProjection.source_id, EdgeProjection.target_id).where(
                    EdgeProjection.edge_kind == "co_access"
                )
            ).all()
        }

    assert frozenset((page_ids[0], page_ids[1])) in pairs
    assert frozenset((page_ids[0], page_ids[-1])) not in pairs
    assert frozenset((page_ids[0], page_ids[CO_ACCESS_ADJACENCY_WINDOW])) not in pairs


def test_co_access_splits_long_sessions_and_keeps_their_earliest_reads(
    session_factory,
):
    """A long session is split front to back and no read is dropped.

    The previous rule kept only the twelve artifacts a session returned to
    most, so the opening reads of a long session never became an edge, and
    its test pinned exactly that. Here every read has its own instant, a
    minute apart, so the time key is what puts them in order: without it the
    reads would be ordered by random call id and the exact pair set below
    would not come out. The identical read order is repeated across the
    minimum number of sessions, so the support threshold admits the pairs
    and the segment ceiling is what is being measured.
    """

    now = datetime.now(UTC)
    segments = 5
    artifact_count = CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT * segments
    # Some documents are read two or three times in a row; each run still
    # holds one position in the read order.
    artifacts = [(uuid4(), 1 + index % 3) for index in range(artifact_count)]
    ordered = [page_id for page_id, _count in artifacts]
    expected = {
        frozenset((chunk[index], chunk[later]))
        for start in range(0, artifact_count, CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT)
        for chunk in (ordered[start : start + CO_ACCESS_MAX_ARTIFACTS_PER_SEGMENT],)
        for index in range(len(chunk))
        for later in range(
            index + 1, min(len(chunk), index + CO_ACCESS_ADJACENCY_WINDOW)
        )
    }
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for _ in range(CO_ACCESS_MIN_SESSIONS):
            _seed_chat_artifacts(
                session,
                workspace_id,
                artifacts,
                occurred_at=now,
            )
        emitted = refresh_co_access_edge_projection(session, workspace_id, now=now)
        pairs = {
            frozenset((source_id, target_id))
            for source_id, target_id in session.execute(
                select(EdgeProjection.source_id, EdgeProjection.target_id).where(
                    EdgeProjection.workspace_id == workspace_id,
                    EdgeProjection.edge_kind == "co_access",
                )
            ).all()
        }

    assert emitted == segments * CO_ACCESS_MAX_PAIRS_PER_SEGMENT
    assert emitted < (artifact_count * (artifact_count - 1)) // 2
    assert pairs == expected
    # The first document of the session is still on the map, and so is
    # every other one.
    assert frozenset((ordered[0], ordered[1])) in pairs
    assert {page_id for pair in pairs for page_id in pair} == set(ordered)


def _seed_citation_fanout(session, workspace_id, page_ids, *, occurred_at):
    """One write citing every page, the way trace capture records candidates.

    Capture writes one CITED row per citation candidate of a single tool
    call, all at the call's instant, whether or not the writer kept it.
    """

    client, chat = _new_chat_session(
        session, workspace_id, occurred_at=occurred_at, name_hint="c2-cite"
    )
    tool_call = MCPToolCall(
        client_id=client.id,
        workspace_id=workspace_id,
        request_id=uuid4(),
        tool_name="write_page",
        result_status="success",
    )
    session.add(tool_call)
    session.flush()
    for page_id in page_ids:
        session.add(
            McpChatToolCallArtifact(
                workspace_id=workspace_id,
                chat_session_id=chat.id,
                mcp_tool_call_id=tool_call.id,
                resource_kind="context_page",
                resource_id=page_id,
                verb="cited",
                occurred_at=occurred_at,
            )
        )
        session.add(
            McpChatArtifactLink(
                workspace_id=workspace_id,
                chat_session_id=chat.id,
                resource_kind="context_page",
                resource_id=page_id,
                verb="cited",
                first_occurred_at=occurred_at,
                last_occurred_at=occurred_at,
                occurrence_count=1,
                last_mcp_tool_call_id=tool_call.id,
            )
        )
    session.flush()


def test_citation_candidates_of_one_write_are_not_read_together(session_factory):
    """Eight pages cited by one write pair into nothing, in the graph or lint.

    Under the consumption allowlist one write with eight candidates became
    a run of co-reads, and repeated writes turned them into edges and hot
    paths although nobody read the pages.
    """

    now = datetime.now(UTC)
    page_ids = [uuid4() for _ in range(8)]
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for _ in range(max(CO_ACCESS_MIN_SESSIONS, HOT_PATH_MIN_SESSIONS)):
            _seed_citation_fanout(session, workspace_id, page_ids, occurred_at=now)

        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 0
        assert _context_pair_session_counts(session, workspace_id, now=now) == {}


def test_hot_paths_and_co_access_separate_pages_the_same_way(session_factory):
    """A Library item read between two pages separates them in both signals.

    Hot paths are the page-to-page co_access pairs. Filtering to pages before
    pairing would make two pages adjacent that the graph keeps apart.
    """

    now = datetime.now(UTC)
    first, last = uuid4(), uuid4()
    between = [uuid4() for _ in range(CO_ACCESS_ADJACENCY_WINDOW - 1)]
    sequence = [
        (first, "context_page"),
        *((item, "library_item") for item in between),
        (last, "context_page"),
    ]
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for _ in range(HOT_PATH_MIN_SESSIONS):
            client, chat = _new_chat_session(
                session, workspace_id, occurred_at=now - timedelta(hours=1)
            )
            for offset, (resource_id, kind) in enumerate(sequence):
                _seed_touches(
                    session,
                    workspace_id,
                    chat,
                    client,
                    resource_id,
                    occurred_at=now - timedelta(minutes=len(sequence) - offset),
                    resource_kind=kind,
                )
        refresh_co_access_edge_projection(session, workspace_id, now=now)
        co_access = {
            frozenset((source_id, target_id))
            for source_id, target_id in session.execute(
                select(EdgeProjection.source_id, EdgeProjection.target_id).where(
                    EdgeProjection.workspace_id == workspace_id,
                    EdgeProjection.edge_kind == "co_access",
                )
            ).all()
        }
        hot_paths = _context_pair_session_counts(session, workspace_id, now=now)

    assert frozenset((first, between[0])) in co_access
    assert frozenset((first, last)) not in co_access
    assert hot_paths == {}


def test_co_access_does_not_depend_on_the_fetch_batch(session_factory, monkeypatch):
    """The source streams in batches; pairs spanning a batch edge survive."""

    now = datetime.now(UTC)
    pages = [uuid4() for _ in range(7)]
    results = {}
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for shift in range(3):
            _seed_read_sequence(
                session,
                workspace_id,
                pages[shift:] + pages[:shift],
                occurred_at=now - timedelta(hours=shift + 1),
            )
        for batch_rows in (1, 2, 5_000):
            monkeypatch.setattr(
                edge_registry, "CO_ACCESS_SOURCE_BATCH_ROWS", batch_rows
            )
            results[batch_rows] = sorted(
                (
                    row["source_id"],
                    row["target_id"],
                    round(float(row["weight"]), 9),
                    row["occurred_at"],
                )
                for row in edge_registry._co_access_rows(
                    session, workspace_id, now=now
                )
            )

    assert results[1]
    assert results[1] == results[2] == results[5_000]


def test_a_failed_refresh_keeps_the_projection_and_a_retry_rebuilds_it(
    session_factory, monkeypatch
):
    """A tick that dies mid-stream changes nothing; running it again is enough."""

    now = datetime.now(UTC)
    left_id, right_id = uuid4(), uuid4()

    def weight(session):
        return session.scalar(
            select(EdgeProjection.weight).where(
                EdgeProjection.edge_kind == "co_access"
            )
        )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for _ in range(CO_ACCESS_MIN_SESSIONS):
            _seed_chat_pair(session, workspace_id, left_id, right_id, occurred_at=now)
        refresh_co_access_edge_projection(session, workspace_id, now=now)
        before = weight(session)
        _seed_chat_pair(session, workspace_id, left_id, right_id, occurred_at=now)

    real_pairs = edge_registry.co_access_pairs

    def dies_after_one_pair(reads):
        for index, pair in enumerate(real_pairs(reads)):
            if index:
                raise RuntimeError("tick cancelled")
            yield pair

    monkeypatch.setattr(edge_registry, "co_access_pairs", dies_after_one_pair)
    with pytest.raises(RuntimeError, match="tick cancelled"):
        with session_factory.begin() as session:
            refresh_co_access_edge_projection(session, workspace_id, now=now)
    with session_factory() as session:
        assert weight(session) == pytest.approx(before)

    monkeypatch.setattr(edge_registry, "co_access_pairs", real_pairs)
    with session_factory.begin() as session:
        refresh_co_access_edge_projection(session, workspace_id, now=now)
        assert weight(session) == pytest.approx(before * 1.5)


def test_hot_paths_pair_reads_the_way_co_access_does(session_factory):
    """A hot path is a pair read close together, not two reads of one session.

    Hot paths used to pair every two pages a session touched, from the rollup
    that has no per-read instant. A page read eight hours later in the same
    conversation is not on the path, however many sessions repeat it.
    """

    now = datetime.now(UTC)
    first, near, late = uuid4(), uuid4(), uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for _ in range(HOT_PATH_MIN_SESSIONS):
            client, chat = _new_chat_session(
                session, workspace_id, occurred_at=now - timedelta(hours=9)
            )
            for page_id, read_at in (
                (first, now - timedelta(hours=8, minutes=5)),
                (near, now - timedelta(hours=8)),
                (late, now),
            ):
                _seed_touches(
                    session, workspace_id, chat, client, page_id, occurred_at=read_at
                )

        counts = _context_pair_session_counts(session, workspace_id, now=now)

    assert counts == {tuple(sorted((first, near), key=str)): HOT_PATH_MIN_SESSIONS}


def test_co_access_canonicalizes_pairs_across_reversed_session_rankings(
    session_factory,
):
    now = datetime.now(UTC)
    left_id, right_id = uuid4(), uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_chat_artifacts(
            session,
            workspace_id,
            [(left_id, 2), (right_id, 1)],
            occurred_at=now,
        )
        _seed_chat_artifacts(
            session,
            workspace_id,
            [(left_id, 1), (right_id, 2)],
            occurred_at=now,
        )

        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 1
        edges = session.scalars(
            select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "co_access",
            )
        ).all()

    assert len(edges) == 1
    assert {edges[0].source_id, edges[0].target_id} == {left_id, right_id}
    assert edges[0].weight == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("verbs", "expected_edges"),
    [
        (("search_match", "search_match"), 0),
        (("referenced", "referenced"), 1),
        (("referenced", "search_match"), 0),
    ],
)
def test_co_access_requires_both_artifacts_to_be_consumed(
    session_factory, verbs, expected_edges
):
    now = datetime.now(UTC)
    left_id, right_id = uuid4(), uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        # Enough sessions to clear the support threshold, so the verb filter
        # is the only thing deciding whether the pair becomes an edge.
        for _ in range(CO_ACCESS_MIN_SESSIONS):
            _seed_chat_pair(
                session,
                workspace_id,
                left_id,
                right_id,
                occurred_at=now,
                verbs=verbs,
            )

        assert (
            refresh_co_access_edge_projection(session, workspace_id, now=now)
            == expected_edges
        )


@pytest.mark.parametrize(
    ("verb", "expected_findings"),
    [("search_match", 0), ("referenced", 1)],
)
def test_trail_tick_stages_only_consumed_unlinked_repeated_distant_paths(
    session_factory, page_service, verb, expected_findings
):
    left = _publish(page_service, "trail-left", "Trail Left")
    right = _publish(page_service, "trail-right", "Trail Right")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        placements = {
            node.target_id: node
            for node in session.scalars(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.target_id.in_((left.id, right.id)),
                )
            ).all()
        }
        for side, page_id in (("left", left.id), ("right", right.id)):
            parent = placements[page_id].parent_id
            for depth in range(2):
                folder = WorkspaceTreeNode(
                    workspace_id=workspace_id,
                    section="context",
                    node_kind="folder",
                    parent_id=parent,
                    sort_key=f"{side}-{depth}",
                    title=f"{side} {depth}",
                    slug=f"{side}-{depth}-{uuid4()}",
                    is_system_node=False,
                    can_have_children=True,
                    primary_placement=False,
                    visibility_status="visible",
                )
                session.add(folder)
                session.flush()
                parent = folder.id
            placements[page_id].parent_id = parent
        for _ in range(5):
            _seed_chat_pair(
                session,
                workspace_id,
                left.id,
                right.id,
                occurred_at=now,
                verbs=(verb, verb),
            )
        result = run_trail_tick(
            session, workspace_id, now=now, usage_signals_enabled=True
        )
        assert result["hot_path_findings"] == expected_findings
        finding = session.scalar(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.hot_path_unmaterialized"
            )
        )
        if expected_findings:
            assert finding is not None
            assert json.loads(finding.suggested_action)["session_count"] == 5
        else:
            assert finding is None


def test_hot_concepts_require_consumption_not_search_exposure(
    session_factory, page_service
):
    page = _publish(page_service, "hot-exposure", "Hot Exposure")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_chat_artifacts(
            session,
            workspace_id,
            [(page.id, 5)],
            occurred_at=now,
            verb="search_match",
        )
        assert list_hot_concepts(
            session, workspace_id, actor=_actor(), now=now
        ) == []

        link = session.scalar(
            select(McpChatArtifactLink).where(
                McpChatArtifactLink.resource_id == page.id
            )
        )
        assert link is not None
        link.verb = "referenced"
        session.flush()
        hot = list_hot_concepts(session, workspace_id, actor=_actor(), now=now)
        assert [item["id"] for item in hot] == [page.id]


def test_hot_concepts_batch_hydrate_and_enforce_actor_visibility(
    session_factory, page_service
):
    visible = _publish(page_service, "hot-visible", "Hot Visible")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        draft = ContextPage(
            workspace_id=workspace_id,
            slug="hot-draft",
            title="Hot Draft",
            page_type="concept",
            lifecycle_status="draft",
        )
        restricted = LibraryItem(
            workspace_id=workspace_id,
            connector_type="manual",
            format_hint="markdown",
            uri="https://example.com/hot-restricted",
            title="Hot Restricted",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        sensitive = AgentPage(
            workspace_id=workspace_id,
            category="memory",
            title="Hot Sensitive",
            slug="hot-sensitive",
            lifecycle_status="active",
            sensitivity="private",
        )
        session.add_all((draft, restricted, sensitive))
        session.flush()
        sensitive_version = AgentPageVersion(
            workspace_id=sensitive.workspace_id,
            agent_page_id=sensitive.id,
            version_no=1,
            body_markdown="# Sensitive",
            frontmatter={},
            actor_type="human",
            version_status="active",
        )
        session.add(sensitive_version)
        session.flush()
        sensitive.current_active_version_id = sensitive_version.id

        client = MCPClient(
            workspace_id=workspace_id,
            name=f"hot-{uuid4()}",
            client_type="generic",
            scopes=[],
            status="active",
        )
        session.add(client)
        session.flush()
        tool_call = MCPToolCall(
            client_id=client.id,
            workspace_id=workspace_id,
            request_id=uuid4(),
            tool_name="search",
            result_status="success",
        )
        chat = McpChatSession(
            workspace_id=workspace_id,
            client_id=client.id,
            mcp_session_id=str(uuid4()),
            started_at=now,
            last_activity_at=now,
        )
        session.add_all((tool_call, chat))
        session.flush()
        for kind, entity_id, count in (
            ("context_page", draft.id, 100),
            ("library_item", restricted.id, 90),
            ("agent_page", sensitive.id, 80),
            ("context_page", visible.id, 70),
        ):
            session.add(
                McpChatArtifactLink(
                    workspace_id=chat.workspace_id,
                    chat_session_id=chat.id,
                    resource_kind=kind,
                    resource_id=entity_id,
                    verb="referenced",
                    first_occurred_at=now,
                    last_occurred_at=now,
                    occurrence_count=count,
                    last_mcp_tool_call_id=tool_call.id,
                )
            )
        session.flush()

        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(session.get_bind(), "before_cursor_execute", count_query)
        try:
            viewer = list_hot_concepts(
                session, workspace_id, actor=_actor("viewer"), now=now
            )
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", count_query)
        owner = list_hot_concepts(session, workspace_id, actor=_actor("owner"), now=now)

    assert {item["id"] for item in viewer} == {visible.id}
    assert {item["id"] for item in owner} == {
        visible.id,
        restricted.id,
        sensitive.id,
    }
    assert query_count <= 4


def test_ppr_visibility_and_authority_projection(session_factory, page_service):
    source = _publish(page_service, "ppr-source", "PPR Source")
    visible = _publish(page_service, "ppr-visible", "PPR Visible")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        hidden = ContextPage(
            workspace_id=workspace_id,
            slug="ppr-hidden",
            title="PPR Hidden",
            page_type="concept",
            lifecycle_status="draft",
        )
        session.add(hidden)
        session.flush()
        for target, weight in ((visible.id, 2.0), (hidden.id, 10.0)):
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=source.id,
                    target_kind="context_page",
                    target_id=target,
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=weight,
                    source_ref={},
                )
            )
        assert recompute_authority(session, workspace_id) >= 3
        stats = session.scalars(
            select(KnowledgeNodeStat).where(
                KnowledgeNodeStat.workspace_id == workspace_id
            )
        ).all()
        assert len(stats) >= 3
        results = GraphService().ppr(
            session,
            workspace_id,
            seeds=[("context_page", source.id, 1.0)],
            actor=_actor("viewer"),
        )
        assert visible.id in {result.id for result in results}
        assert hidden.id not in {result.id for result in results}


def test_associative_search_lane_is_opt_in_and_keeps_exact_hit_first(
    session_factory, page_service
):
    source = _publish(page_service, "assoc-source", "Associative Source")
    related = _publish(page_service, "assoc-related", "Associative Related")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=source.id,
                target_kind="context_page",
                target_id=related.id,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        session.flush()
        off = SearchService().search(
            session,
            workspace_id,
            "Associative Source",
            actor=_actor("viewer"),
        )
        on = SearchService(associative_lane=True).search(
            session,
            workspace_id,
            "Associative Source",
            actor=_actor("viewer"),
        )
        assert "associative" not in off.tried_lanes
        assert on.ranked_results[0].id == source.id
        assert any(
            result.id == related.id and result.match_reason == "associative"
            for result in on.ranked_results
        )
        assert "associative" in on.tried_lanes


def test_ppr_cache_uses_database_revision_across_sessions(session_factory):
    seed, first_target, second_target = uuid4(), uuid4(), uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=seed,
                target_kind="context_page",
                target_id=first_target,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        bump_edge_version(session, workspace_id)

    with session_factory() as session:
        first = subgraph_ppr(session, workspace_id, [("context_page", seed, 1.0)])
        assert first_target in {result.entity_id for result in first}

    with session_factory.begin() as session:
        session.execute(
            delete(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        )
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=seed,
                target_kind="context_page",
                target_id=second_target,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        bump_edge_version(session, workspace_id)

    with session_factory() as session:
        second = subgraph_ppr(session, workspace_id, [("context_page", seed, 1.0)])
        ids = {result.entity_id for result in second}
        assert second_target in ids
        assert first_target not in ids


def test_ppr_frontier_reserves_capacity_for_incoming_edges(session_factory):
    seed = uuid4()
    incoming_id = uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        outgoing = [
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=seed,
                target_kind="context_page",
                target_id=uuid4(),
                edge_kind="wikilink",
                provenance="curated",
                weight=10.0 - index,
                source_ref={},
            )
            for index in range(8)
        ]
        incoming = EdgeProjection(
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=incoming_id,
            target_kind="context_page",
            target_id=seed,
            edge_kind="wikilink",
            provenance="curated",
            weight=0.1,
            source_ref={},
        )
        session.add_all([*outgoing, incoming])
        session.flush()

        rows = _frontier_edges(
            session,
            workspace_id,
            {("context_page", seed)},
            limit=4,
        )

    assert len(rows) == 4
    assert incoming.id in {row.id for row in rows}
    assert {row.id for row in outgoing} & {row.id for row in rows}


def test_ppr_frontier_reservation_is_not_spent_by_intra_frontier_edges(
    session_factory,
):
    """An A->B edge inside the frontier tops BOTH directional batches. Reserving a
    positional slice let it consume the incoming quota while adding no incoming
    edge, starving true-incoming edges on exactly the mutually-linked seeds
    associative search produces."""

    left = uuid4()
    right = uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        shared = [
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=left,
                target_kind="context_page",
                target_id=right,
                edge_kind=edge_kind,
                provenance="curated",
                weight=100.0 - index,
                source_ref={},
            )
            for index, edge_kind in enumerate(
                (
                    "wikilink",
                    "tree",
                    "citation_page",
                    "citation_source",
                    "promoted_from",
                )
            )
        ]
        outgoing = [
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=left,
                target_kind="context_page",
                target_id=uuid4(),
                edge_kind="wikilink",
                provenance="curated",
                weight=90.0 - index,
                source_ref={},
            )
            for index in range(10)
        ]
        incoming = [
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=uuid4(),
                target_kind="context_page",
                target_id=right,
                edge_kind="wikilink",
                provenance="curated",
                weight=10.0 - index,
                source_ref={},
            )
            for index in range(10)
        ]
        session.add_all([*shared, *outgoing, *incoming])
        session.flush()

        rows = _frontier_edges(
            session,
            workspace_id,
            {("context_page", left), ("context_page", right)},
            limit=10,
        )
        selected_ids = {row.id for row in rows}
        incoming_ids = {edge.id for edge in incoming}

    assert len(rows) == 10
    assert selected_ids & incoming_ids, "true-incoming edges were starved"


def test_ppr_ties_use_context_trust_and_cut_usage_after_curated(
    session_factory, page_service
):
    source = _publish(page_service, "trust-source", "Trust Source")
    human = _publish(page_service, "trust-human", "Trust Human")
    agent = _publish(page_service, "trust-agent", "Trust Agent")
    library_node = uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.execute(
            delete(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        )
        agent_page = session.get(ContextPage, agent.id)
        agent_version = session.get(
            ContextPageVersion, agent_page.current_published_version_id
        )
        agent_version.created_by_actor_kind = "agent"
        for kind, target in (
            ("context_page", human.id),
            ("context_page", agent.id),
            ("library_item", library_node),
        ):
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=source.id,
                    target_kind=kind,
                    target_id=target,
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=1.0,
                    source_ref={},
                )
            )
        usage_target = uuid4()
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=source.id,
                target_kind="context_page",
                target_id=usage_target,
                edge_kind="co_access",
                provenance="usage",
                weight=100.0,
                source_ref={},
            )
        )
        bump_edge_version(session, workspace_id)
        results = subgraph_ppr(
            session,
            workspace_id,
            [("context_page", source.id, 1.0)],
            top_k=10,
            node_budget=4,
        )

    by_id = {result.entity_id: result for result in results}
    assert by_id[human.id].trust_prior > by_id[agent.id].trust_prior
    assert by_id[library_node].trust_prior == 0
    assert usage_target not in by_id


def test_mermaid_projection_escapes_labels_and_is_opt_in():
    neighbor = GraphNeighborEntry(
        kind="context_page",
        id=uuid4(),
        title='Injected "]\nclick evil',
        path="/context/target.md",
        edge_kind="wikilink",
        direction="out",
    )
    payload = GraphNeighborsPayload(outbound=[neighbor])
    rendered = GraphService.render_context_page_mermaid('Seed "]', payload)
    assert rendered.startswith("graph LR")
    # The frontend renders with Mermaid securityLevel=strict. Its click
    # directives fail parsing there, so the server emits the planned static
    # fallback and leaves navigation to the structured neighbor lists.
    assert "\n  click " not in rendered
    assert '\nclick evil"]' not in rendered
    assert "#quot;" in rendered
    assert "\\]" not in rendered
    assert payload.mermaid is None


def _add_embedding(session, workspace_id, page_id, vector):
    chunk = ContentChunk(
        workspace_id=workspace_id,
        document_kind="context_page",
        document_id=page_id,
        content_scope="published",
        ordinal=0,
        char_start=0,
        char_end=4,
        chunk_text="Body",
        chunk_text_hash=str(uuid4()),
        token_count=1,
    )
    session.add(chunk)
    session.flush()
    session.add(
        ChunkEmbedding(
            workspace_id=chunk.workspace_id,
            chunk_id=chunk.id,
            provider="deterministic",
            model="test",
            dim=len(vector),
            embedding=struct.pack(f"<{len(vector)}f", *vector),
        )
    )


def test_librarian_uses_existing_embeddings_and_stages_duplicate(
    session_factory, page_service
):
    left = _publish(page_service, "lib-left", "Librarian Left")
    right = _publish(page_service, "lib-right", "Librarian Right")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _add_embedding(session, workspace_id, left.id, [1.0, 0.0])
        _add_embedding(session, workspace_id, right.id, [0.99, 0.01])
        result = run_librarian_tick(
            session,
            workspace_id,
            embedding_provider="deterministic",
        )
        assert result["near_duplicate_findings"] == 1
        assert session.scalar(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.semantic_near_duplicate"
            )
        )
        assert session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.task_type == "near_duplicate_verdict"
            )
        )
    with session_factory.begin() as session:
        assert run_librarian_tick(
            session,
            workspace_id,
            embedding_provider="none",
        ) == {"skipped": "embedding_provider=none"}


def test_librarian_caps_recent_pages_and_avoids_pairwise_sql(
    session_factory, monkeypatch
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        pages = [
            ContextPage(
                workspace_id=workspace_id,
                slug=f"librarian-cap-{index}",
                title=f"Librarian Cap {index}",
                page_type="concept",
                lifecycle_status="published",
            )
            for index in range(205)
        ]
        session.add_all(pages)
        session.flush()
        versions = [
            ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page.id,
                version_no=1,
                body_md=f"# {page.title}",
                content_hash=uuid4().hex,
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="librarian-cap-test",
                version_status="published_snapshot",
            )
            for page in pages
        ]
        session.add_all(versions)
        session.flush()
        for page, version in zip(pages, versions, strict=True):
            page.current_version_id = version.id
            page.current_published_version_id = version.id
        chunks = [
            ContentChunk(
                workspace_id=workspace_id,
                document_kind="context_page",
                document_id=page.id,
                content_scope="published",
                ordinal=0,
                char_start=0,
                char_end=4,
                chunk_text="Body",
                chunk_text_hash=uuid4().hex,
                token_count=1,
            )
            for page in pages
        ]
        session.add_all(chunks)
        session.flush()
        session.add_all(
            ChunkEmbedding(
                workspace_id=chunk.workspace_id,
                chunk_id=chunk.id,
                provider="deterministic",
                model="cap-test",
                dim=2,
                embedding=struct.pack("<2f", 1.0, 0.0),
            )
            for chunk in chunks
        )
        session.flush()

        observed: dict[str, int] = {}

        def no_duplicates(_session, _run, _workspace_id, _pages, vectors, _active):
            observed["duplicate_vectors"] = len(vectors)
            return 0

        def no_misplaced(_session, _run, _workspace_id, _pages, vectors, _active):
            observed["misplaced_vectors"] = len(vectors)
            return 0

        monkeypatch.setattr(
            "backend.services.librarian._stage_near_duplicates", no_duplicates
        )
        monkeypatch.setattr(
            "backend.services.librarian._stage_misplaced_pages", no_misplaced
        )
        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(session.get_bind(), "before_cursor_execute", count_query)
        try:
            result = run_librarian_tick(
                session,
                workspace_id,
                embedding_provider="deterministic",
                embedding_model="cap-test",
            )
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", count_query)

    assert result["analyzed_pages"] == 200
    assert result["truncated"] == 1
    assert observed == {"duplicate_vectors": 200, "misplaced_vectors": 200}
    assert query_count <= 8


def test_librarian_stages_a_misplaced_page_finding(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        folder = WorkspaceTreeNode(
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            sort_key="misplaced-folder",
            title="Placement Folder",
            slug="placement-folder",
            visibility_status="visible",
        )
        pages = [
            ContextPage(
                workspace_id=workspace_id,
                slug=f"placement-{index}",
                title=f"Placement {index}",
                page_type="concept",
                lifecycle_status="published",
            )
            for index in range(4)
        ]
        session.add_all([folder, *pages])
        session.flush()
        session.add_all(
            WorkspaceTreeNode(
                workspace_id=workspace_id,
                section="context",
                node_kind="context_page",
                parent_id=folder.id,
                sort_key=f"placement-{index}",
                title=page.title,
                target_kind="context_page",
                target_id=page.id,
                primary_placement=True,
                visibility_status="visible",
            )
            for index, page in enumerate(pages)
        )
        run = LintRun(
            workspace_id=workspace_id,
            lint_scope={"kind": "maintenance"},
            trigger_type="librarian_tick",
            status="completed",
            finding_count_by_severity={},
        )
        session.add(run)
        session.flush()
        created = _stage_misplaced_pages(
            session,
            run,
            workspace_id,
            {page.id: page for page in pages},
            {
                pages[0].id: [1.0, 0.0],
                pages[1].id: [1.0, 0.0],
                pages[2].id: [1.0, 0.0],
                pages[3].id: [-1.0, 0.0],
            },
            set(),
        )

        finding = session.scalar(
            select(LintFinding).where(LintFinding.rule_id == "kb.misplaced_page")
        )
        assert created == 1
        assert finding is not None
        assert finding.target_id == pages[3].id


def test_eval_baseline_gate_reports_only_material_drops():
    modes = [ModeMetrics("keyword", 1, 0.90, 0.80, 1.0)]
    lanes = [LaneMetrics("entity", 1, 1.0, 1.0, 1.0)]
    baseline = {
        "threshold": 0.05,
        "modes": {"keyword": {"recall_at_k": 0.96, "mrr": 0.82}},
        "lanes": {"entity": {"recall_at_k": 1.0}},
    }
    regressions = compare_baseline(modes, lanes, baseline)
    assert [(item.scope, item.name, item.metric) for item in regressions] == [
        ("modes", "keyword", "recall_at_k")
    ]


@pytest.mark.parametrize(
    ("current_recall", "is_regression"),
    [
        (0.95, False),
        (0.9499999995, False),
        (0.949999, True),
    ],
)
def test_eval_baseline_gate_honors_epsilon_at_threshold(
    current_recall,
    is_regression,
):
    regressions = compare_baseline(
        [ModeMetrics("keyword", 1, current_recall, 1.0, 1.0)],
        [],
        {
            "threshold": 0.05,
            "modes": {"keyword": {"recall_at_k": 1.0}},
        },
    )

    assert bool(regressions) is is_regression


def _seed_ppr_star(session, workspace_id):
    """Ten thousand edges fanning out of one seed; returns the seed."""

    seed = uuid4()
    session.execute(
        insert(EdgeProjection),
        [
            {
                "workspace_id": workspace_id,
                "source_kind": "context_page",
                "source_id": seed,
                "target_kind": "context_page",
                "target_id": uuid4(),
                "edge_kind": "wikilink",
                "provenance": "curated",
                "weight": 1.0,
                "source_ref": {},
            }
            for _ in range(10_000)
        ],
    )
    return seed


def _cold_ppr(session, workspace_id, seed):
    # Drop the memoized subgraph so every run pays the full cold path
    # (frontier SQL + power iteration). The oversized request proves the
    # budget is clamped rather than trusted.
    _subgraph_cache.clear()
    return subgraph_ppr(
        session,
        workspace_id,
        [("context_page", seed, 1.0)],
        top_k=20,
        node_budget=20_000,
    )


def test_ppr_work_is_bounded_at_10k_edges(session_factory, monkeypatch):
    # The 50ms budget (§19.5.7) rests on PPR never materialising more than
    # PPR_NODE_BUDGET nodes, whatever the fan-out or the requested budget. That
    # bound is asserted on every run, as work rather than time: a 10k-edge star
    # must load one seed plus 499 neighbours, where dropping the bound loads all
    # 10k, and the SQL it issues must carry that same bound. Under four xdist
    # workers on a shared CI runner a healthy run measured 59ms, so a timing
    # gate failed without a regression; the wall clock lives in the opt-in
    # benchmark below.
    loaded = []
    frontier_sizes = []
    ranked_pages = []
    original_load = graph_ppr._load_subgraph
    original_frontier = graph_ppr._frontier_edges
    original_priors = graph_ppr.context_page_trust_priors

    def spy_load(session, workspace_id, seeds, *, node_budget):
        edges = original_load(session, workspace_id, seeds, node_budget=node_budget)
        nodes = set(seeds)
        for source_kind, source_id, target_kind, target_id, _weight in edges:
            nodes.update(((source_kind, source_id), (target_kind, target_id)))
        loaded.append((node_budget, len(edges), len(nodes)))
        return edges

    def spy_frontier(session, workspace_id, frontier, *, limit):
        rows = original_frontier(session, workspace_id, frontier, limit=limit)
        frontier_sizes.append(len(rows))
        return rows

    def spy_priors(session, workspace_id, page_ids, **kwargs):
        ranked_pages.append(len(page_ids))
        return original_priors(session, workspace_id, page_ids, **kwargs)

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_ppr_star(session, workspace_id)

        try:
            assert graph_ppr.PPR_NODE_BUDGET == 500
            with (
                monkeypatch.context() as patch,
                _captured_statements(session) as statements,
            ):
                patch.setattr(graph_ppr, "_load_subgraph", spy_load)
                patch.setattr(graph_ppr, "_frontier_edges", spy_frontier)
                patch.setattr(graph_ppr, "context_page_trust_priors", spy_priors)
                result = _cold_ppr(session, workspace_id, seed)

            assert loaded == [(500, 499, 500)]
            assert frontier_sizes == [500]
            assert len(result) == 20

            # The rank vector is the loaded subgraph: the budgeted nodes, no more.
            assert ranked_pages == [500]
            # The single hop reads edges once per direction, and both reads
            # carry the clamped budget rather than the 20_000 asked for.
            edge_reads = [
                (statement, parameters)
                for statement, parameters in statements
                if "FROM edge_projection" in statement
            ]
            assert [_bound_limit(*read) for read in edge_reads] == [500, 500]
            # Revision lookup, those two reads and one trust-prior read: the
            # whole cold path.
            assert len(statements) <= 4, "unexpected extra statements"
        finally:
            _subgraph_cache.clear()


def test_ppr_power_iteration_stops_after_12_steps(session_factory, monkeypatch):
    # Rank mass moves one hop per power iteration, so along a path the nodes
    # holding any rank count the iterations that ran: 12 of them reach the seed
    # and the twelve nodes after it, and no further. The loader is replaced
    # because the real one never reaches past two hops.
    path = [("context_page", uuid4()) for _ in range(30)]
    edges = tuple(
        (*source, *target, 1.0) for source, target in zip(path, path[1:], strict=False)
    )

    def path_subgraph(_session, _workspace_id, _seeds, *, node_budget):
        return edges

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        try:
            monkeypatch.setattr(graph_ppr, "_load_subgraph", path_subgraph)
            result = subgraph_ppr(
                session, workspace_id, [(*path[0], 1.0)], top_k=len(path)
            )
        finally:
            _subgraph_cache.clear()

    assert {item.entity_id for item in result if item.score > 0} == {
        node_id for _kind, node_id in path[:13]
    }


@pytest.fixture
def benchmark_lane(request):
    """Gate for wall-clock budgets: opt-in, and never beside xdist workers.

    A latency measured on a shared runner proves nothing about the code, so the
    functional lanes assert counted work and the budgets run only when asked
    for, one process at a time: ``make test-backend-benchmarks``.
    """

    if os.environ.get("DTXWIKI_TEST_BENCHMARKS") != "1":
        pytest.skip("wall-clock budget; run `make test-backend-benchmarks`")
    if hasattr(request.config, "workerinput"):
        pytest.fail(
            "DTXWIKI_TEST_BENCHMARKS=1 under pytest-xdist: a budget timed beside "
            "other workers is not a measurement; run the lane without -n, as "
            "`make test-backend-benchmarks` does",
            pytrace=False,
        )


def test_ppr_latency_benchmark_10k_edges(session_factory, benchmark_lane):
    """19.5.7 budget: a cold 10k-edge PPR in <= 50ms, best of three."""

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_ppr_star(session, workspace_id)

        try:
            # The first run is the unmeasured warm-up.
            _cold_ppr(session, workspace_id, seed)
            elapsed = min(
                _elapsed(lambda: _cold_ppr(session, workspace_id, seed))
                for _ in range(3)
            )
            assert elapsed <= 0.05, f"PPR budget exceeded: {elapsed:.3f}s"
        finally:
            _subgraph_cache.clear()


def _seed_authority_chain(session, workspace_id):
    """A 100k-edge chain on top of the workspace's own provisioned graph.

    Returns the distinct nodes the workspace already carried, the chain's nodes
    in order, and the edge rows exactly as ``recompute_authority`` selects them.
    """

    # A provisioned workspace already carries tree edges of its own, so
    # the budget is measured against the seeded graph plus the 100k this
    # test adds, not against 100k alone.
    seeded_nodes = _distinct_edge_endpoints(session, workspace_id)
    nodes = [uuid4() for _ in range(100_001)]
    session.execute(
        insert(EdgeProjection),
        [
            {
                "workspace_id": workspace_id,
                "source_kind": "context_page",
                "source_id": nodes[index],
                "target_kind": "context_page",
                "target_id": nodes[index + 1],
                "edge_kind": "wikilink",
                "provenance": "curated",
                "weight": 1.0,
                "source_ref": {},
            }
            for index in range(100_000)
        ],
    )

    edge_rows = session.execute(
        select(
            EdgeProjection.source_kind,
            EdgeProjection.source_id,
            EdgeProjection.target_kind,
            EdgeProjection.target_id,
            EdgeProjection.edge_kind,
            EdgeProjection.weight,
        ).where(EdgeProjection.workspace_id == workspace_id)
    ).all()
    return seeded_nodes, nodes, edge_rows


def _metered_edges(rows, *, probe_budget):
    """Wrap authority-calculation input so the algorithm reports its own work.

    The calculation is a pure function over its edges, so the edges are the one
    place to watch it from. Every wrapper delegates to the value it stands for,
    leaving the ranks exactly as the plain call computes them; each only counts.
    ``passes`` and ``rows`` count reads of the edge sequence, ``transitions`` the
    weights scaled into transition probabilities (by whichever of ``*`` and ``/``
    reaches a weight first, so the order the code writes them in is immaterial),
    ``probes`` the hash-table probes made on node ids. A probe past
    ``probe_budget`` raises on the spot, so a quadratic node index fails at once
    instead of running for hours.
    """

    meter = SimpleNamespace(passes=0, rows=0, transitions=0, probes=0)

    def probe():
        meter.probes += 1
        if meter.probes > probe_budget:
            raise AssertionError(f"node ids probed more than {probe_budget} times")

    class Weight(float):
        def __mul__(self, other):
            meter.transitions += 1
            return float(self) * other

        __rmul__ = __mul__

        def __truediv__(self, other):
            meter.transitions += 1
            return float(self) / other

    class NodeId(UUID):
        def __hash__(self):
            probe()
            return super().__hash__()

        def __eq__(self, other):
            probe()
            return super().__eq__(other)

    class Edges:
        def __iter__(self):
            meter.passes += 1
            for source_kind, source_id, target_kind, target_id, kind, weight in rows:
                meter.rows += 1
                yield (
                    source_kind,
                    NodeId(int=source_id.int),
                    target_kind,
                    NodeId(int=target_id.int),
                    kind,
                    Weight(weight),
                )

    return Edges(), meter


def test_authority_work_is_bounded_at_100k_edges(session_factory):
    # The 5s budget (§19.5.7) rests on the calculation doing a bounded amount of
    # work per edge. That is counted on every run instead of timed (the wall
    # clock lives in the opt-in benchmark below): the edges are read once, one
    # transition is built per edge, the node index is probed a constant number
    # of times per edge, and rank propagates for the 20-iteration cap, no more.
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded_nodes, nodes, edge_rows = _seed_authority_chain(session, workspace_id)

    edges, meter = _metered_edges(edge_rows, probe_budget=8 * len(edge_rows))
    stats = _authority_stat_rows(
        edges,
        workspace_id=workspace_id,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert len(stats) == 100_001 + seeded_nodes
    assert (meter.passes, meter.rows) == (1, len(edge_rows))
    # A co_access edge also builds its reverse, so two per edge is the ceiling;
    # these are all wikilinks, which build exactly one.
    assert meter.transitions == len(edge_rows)
    # Four probes per edge here (a hit costs a hash and a compare, a miss a hash
    # and a store), budgeted at eight so that only a super-linear index trips it.
    assert 0 < meter.probes <= 8 * len(edge_rows)

    # Along a chain rank travels one hop per iteration, so a node's value only
    # depends on min(position, iterations) and levels off at position ==
    # iterations: the plateau starts at 20 only if exactly 20 iterations ran.
    authority = {row["entity_id"].int: row["authority"] for row in stats}
    chain = [authority[node.int] for node in nodes]
    plateau = chain[-1]
    assert chain.index(plateau) == 20
    assert set(chain[20:]) == {plateau}


def test_authority_builds_at_most_two_transitions_per_edge():
    # A co_access edge is mutual, so it builds its reverse as well; every other
    # kind builds one. Two per edge is therefore the ceiling on inner-loop work.
    kinds = ["wikilink", "co_access", "wikilink", "co_access", "co_access"]
    ids = [uuid4() for _ in range(len(kinds) + 1)]
    rows = [
        ("context_page", ids[index], "context_page", ids[index + 1], kind, 1.0)
        for index, kind in enumerate(kinds)
    ]
    edges, meter = _metered_edges(rows, probe_budget=8 * len(rows))

    _authority_stat_rows(
        edges, workspace_id=uuid4(), now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    assert meter.transitions == len(rows) + kinds.count("co_access")
    assert meter.transitions <= 2 * len(rows)


def test_authority_latency_benchmark_100k_edges(session_factory, benchmark_lane):
    """19.5.7 budget: authority over a 100k-edge graph in <= 5s, best of two."""

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded_nodes, _nodes, edge_rows = _seed_authority_chain(session, workspace_id)

        def calculate():
            return _authority_stat_rows(
                edge_rows,
                workspace_id=workspace_id,
                now=datetime.now(UTC),
            )

        # The algorithm budget is measured apart from DB scheduling noise.
        # Persistence and revision-based no-op behavior have separate tests.
        stats = calculate()
        elapsed = min(_elapsed(calculate) for _ in range(2))
        assert len(stats) == 100_001 + seeded_nodes
        assert elapsed <= 5.0, f"authority budget exceeded: {elapsed:.3f}s"


def _distinct_edge_endpoints(session, workspace_id) -> int:
    """Count the nodes a workspace's existing edges already contribute.

    Provisioning gives every workspace a tree, and the tree projects edges, so
    a test that adds edges of its own cannot assume it started from nothing.
    """

    rows = session.execute(
        select(
            EdgeProjection.source_kind,
            EdgeProjection.source_id,
            EdgeProjection.target_kind,
            EdgeProjection.target_id,
        ).where(EdgeProjection.workspace_id == workspace_id)
    ).all()
    endpoints = set()
    for source_kind, source_id, target_kind, target_id in rows:
        endpoints.add((source_kind, source_id))
        endpoints.add((target_kind, target_id))
    return len(endpoints)


def test_authority_projection_skips_unchanged_edge_revision(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded_nodes = _distinct_edge_endpoints(session, workspace_id)
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=uuid4(),
                target_kind="context_page",
                target_id=uuid4(),
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        session.flush()
        assert recompute_authority(session, workspace_id) == 2 + seeded_nodes

        statements: list[str] = []

        def capture(_conn, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(session.get_bind(), "before_cursor_execute", capture)
        try:
            assert recompute_authority(session, workspace_id) == 2 + seeded_nodes
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", capture)

        assert not any("FROM edge_projection" in statement for statement in statements)

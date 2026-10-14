"""F08-T1: bench_local corpus preparation and SQL phase profile."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

import pytest
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.orm import aliased, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.base import Base
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import (
    DEFAULT_WORKSPACE_SLUG,
    DEFAULT_WORKSPACE_TITLE,
    seed_default_workspace,
)
from scripts.bench.graph import bench_local

pytestmark = pytest.mark.integration

# Fixed ids and instants so two databases are copies of one corpus. P0 is a
# draft, P1 already links to the seed, and P3 is the seed; the rest are
# eligible backlinkers.
WORKSPACE_ID = UUID("f0800000-0000-4000-8000-00000000ffff")
PAGE_IDS = tuple(UUID(f"f0800000-0000-4000-8000-{index:012d}") for index in range(7))
DRAFT, LINKER, FIRST, SEED, SECOND, THIRD, FOURTH = PAGE_IDS
LINKER_EDGE_ID = UUID("f0800000-0000-4000-8000-00000000eeee")
PUBLISHED_AT = datetime(2026, 9, 1, tzinfo=UTC)

# A statement tagged ranked_candidates holds its execute call at least this long.
SLEEP_MS = 5.0


def _published_at(page_id: UUID) -> datetime:
    return PUBLISHED_AT + timedelta(minutes=PAGE_IDS.index(page_id))


def _corpus_session_factory():
    """A database built the way conftest's ``session_factory`` is.

    Its default workspace has the fixed ``WORKSPACE_ID``, which backlink ids
    are derived from, as ``insert_workspace`` would create it.
    """

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            Workspace(
                id=WORKSPACE_ID,
                slug=DEFAULT_WORKSPACE_SLUG,
                title=DEFAULT_WORKSPACE_TITLE,
                name=DEFAULT_WORKSPACE_TITLE,
            )
        )
        session.flush()
        seed_default_workspace(session)
    return factory


def _workspace_id(session) -> UUID:
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _seed_page(
    session,
    workspace_id: UUID,
    page_id: UUID,
    *,
    published: bool,
    created_at: datetime,
):
    page = ContextPage(
        id=page_id,
        workspace_id=workspace_id,
        slug=f"bench-{page_id.hex[-4:]}",
        title=f"Bench {page_id.hex[-4:]}",
        page_type="concept",
        lifecycle_status="published" if published else "draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md="# Bench\n\nBody.",
        content_hash=f"bench-{page_id.hex}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot" if published else "draft",
        created_at=created_at,
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    if published:
        page.current_published_version_id = version.id
    else:
        page.latest_draft_version_id = version.id


def _wikilink(
    workspace_id: UUID,
    source_id: UUID,
    target_id: UUID,
    weight: float,
    **columns,
):
    return EdgeProjection(
        workspace_id=workspace_id,
        source_kind="context_page",
        source_id=source_id,
        target_kind="context_page",
        target_id=target_id,
        edge_kind="wikilink",
        provenance="curated",
        weight=weight,
        source_ref={"table": "page_links"},
        **columns,
    )


def _seed_backlink_corpus(factory) -> UUID:
    with factory.begin() as session:
        workspace_id = _workspace_id(session)
        # Reverse insertion order: the sources must come from UUID order.
        for page_id in reversed(PAGE_IDS):
            _seed_page(
                session,
                workspace_id,
                page_id,
                published=page_id != DRAFT,
                created_at=_published_at(page_id),
            )
        session.add(
            _wikilink(
                workspace_id,
                LINKER,
                SEED,
                7.0,
                id=LINKER_EDGE_ID,
                created_at=PUBLISHED_AT,
                last_seen_at=PUBLISHED_AT,
            )
        )
    return workspace_id


def _backlinks(factory, workspace_id: UUID) -> list[tuple]:
    with factory() as session:
        return [
            tuple(row)
            for row in session.execute(
                select(
                    EdgeProjection.id,
                    EdgeProjection.source_kind,
                    EdgeProjection.source_id,
                    EdgeProjection.target_kind,
                    EdgeProjection.target_id,
                    EdgeProjection.edge_kind,
                    EdgeProjection.provenance,
                    EdgeProjection.weight,
                    EdgeProjection.occurred_at,
                    EdgeProjection.source_ref,
                    EdgeProjection.last_seen_at,
                    EdgeProjection.created_at,
                )
                .where(
                    EdgeProjection.workspace_id == workspace_id,
                    EdgeProjection.target_id == SEED,
                )
                .order_by(EdgeProjection.source_id)
            )
        ]


def test_add_hub_backlinks_is_deterministic():
    inserted: list[list[tuple[UUID, float]]] = []
    rows: list[list[tuple]] = []
    for factory in (_corpus_session_factory(), _corpus_session_factory()):
        workspace_id = _seed_backlink_corpus(factory)
        # Four pages qualify: the draft, the seed and the linker do not.
        with pytest.raises(ValueError, match="found 4"):
            with factory.begin() as session:
                bench_local.add_hub_backlinks(
                    session, workspace_id, seed_page_id=SEED, count=5
                )
        assert len(_backlinks(factory, workspace_id)) == 1
        with factory.begin() as session:
            inserted.append(
                bench_local.add_hub_backlinks(
                    session, workspace_id, seed_page_id=SEED, count=4
                )
            )
        rows.append(_backlinks(factory, workspace_id))

    assert inserted[0] == [(FIRST, 1.0), (SECOND, 2.0), (THIRD, 3.0), (FOURTH, 1.0)]
    assert inserted[1] == inserted[0]
    # Ids and timestamps included: no random id and no wall clock.
    assert rows[0] == rows[1]

    def row(edge_id, source_id, weight, table, seen_at):
        return (
            edge_id,
            "context_page",
            source_id,
            "context_page",
            SEED,
            "wikilink",
            "curated",
            weight,
            None,
            {"table": table},
            seen_at,
            seen_at,
        )

    def backlink(source_id, weight):
        # The id comes from the endpoints; both instants from the source's
        # published version.
        return row(
            uuid5(
                bench_local.HUB_BACKLINK_ID_NAMESPACE,
                f"{WORKSPACE_ID}:{SEED}:{source_id}",
            ),
            source_id,
            weight,
            "bench_local",
            _published_at(source_id),
        )

    assert rows[0] == [
        # The existing linker is skipped, not duplicated or reweighted.
        row(LINKER_EDGE_ID, LINKER, 7.0, "page_links", PUBLISHED_AT),
        backlink(FIRST, 1.0),
        backlink(SECOND, 2.0),
        backlink(THIRD, 3.0),
        backlink(FOURTH, 1.0),
    ]


def _edge_rows(session, workspace_id: UUID, edge_kind: str) -> set[tuple]:
    return {
        tuple(row)
        for row in session.execute(
            select(
                EdgeProjection.id,
                EdgeProjection.source_kind,
                EdgeProjection.source_id,
                EdgeProjection.target_kind,
                EdgeProjection.target_id,
                EdgeProjection.weight,
                EdgeProjection.provenance,
            ).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == edge_kind,
            )
        )
    }


def _identities(rows: set[tuple]) -> set[tuple]:
    return {row[1:5] for row in rows}


def test_project_tree_edges_adds_tree_rows_and_keeps_wikilinks(
    session_factory, page_service
):
    pages = [
        page_service.write_page(
            workspace_slug="default",
            slug=slug,
            title=slug.title(),
            body_md=f"# {slug.title()}\n\nBody.",
            actor_kind="human",
            actor_id="tester",
            actor_provenance="browser_session",
        )
        for slug in ("alpha", "beta", "gamma")
    ]
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.add(_wikilink(workspace_id, pages[0].id, pages[1].id, 2.0))
        session.add(_wikilink(workspace_id, pages[2].id, pages[1].id, 1.0))
        session.flush()
        expected_tree = _identities(_edge_rows(session, workspace_id, "tree"))
        # The synthetic corpus places pages in folders but writes no tree rows.
        session.execute(
            delete(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "tree",
            )
        )
    with session_factory() as session:
        wikilinks = _edge_rows(session, workspace_id, "wikilink")
    assert len(wikilinks) == 2
    assert {("context_page", page.id) for page in pages} <= {
        (target_kind, target_id) for _, _, target_kind, target_id in expected_tree
    }

    with session_factory.begin() as session:
        result = bench_local.project_tree_edge_rows(session, workspace_id)

    with session_factory() as session:
        projected = _identities(_edge_rows(session, workspace_id, "tree"))
        assert _edge_rows(session, workspace_id, "wikilink") == wikilinks
    # The rebuild also projects the seeded folder skeleton, which the
    # per-node write-through never touched, so the pages' rows are a subset.
    assert expected_tree <= projected
    assert result == {"tree_rows_before": 0, "tree_rows": len(projected)}


def _sleep_ms(ms: float) -> float:
    """Hold the calling statement at least ``ms`` on the perf_counter clock."""

    deadline = time.perf_counter() + ms / 1_000
    while (remaining := deadline - time.perf_counter()) > 0:
        time.sleep(remaining)
    return ms


def _register_sleep(engine) -> None:
    # StaticPool: every session and the profile replay share this connection.
    connection = engine.raw_connection()
    try:
        connection.driver_connection.create_function("bench_sleep_ms", 1, _sleep_ms)
    finally:
        connection.close()


def _execute_fetchall_ms(engine, statement: str, *, repeats: int = 5) -> float:
    """Time ``statement`` directly: execute plus fetchall, fastest of repeats."""

    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        timings = []
        for _ in range(repeats):
            started = time.perf_counter()
            cursor.execute(statement)
            cursor.fetchall()
            timings.append((time.perf_counter() - started) * 1_000)
        cursor.close()
    finally:
        connection.close()
    return min(timings)


def test_sql_profile_groups_statements_by_phase_tag(session_factory):
    with session_factory() as session:
        workspace_id = _workspace_id(session)
    engine = session_factory.kw["bind"]
    _register_sleep(engine)

    def one_request() -> None:
        with session_factory() as session:
            session.execute(
                select(EdgeProjection.id)
                .where(EdgeProjection.workspace_id == workspace_id)
                .execution_options(graph_rank_phase="ranked_candidates")
            ).all()
            # A known lower bound, spent inside the execute call itself.
            session.execute(
                select(func.bench_sleep_ms(SLEEP_MS)).execution_options(
                    graph_rank_phase="ranked_candidates"
                )
            ).all()
            session.execute(
                select(EdgeProjection.id).where(
                    EdgeProjection.workspace_id == workspace_id,
                    EdgeProjection.edge_kind == "wikilink",
                )
            ).all()
            # A column named edge_projection_version is not the table.
            session.execute(
                select(Workspace.edge_projection_version).where(
                    Workspace.id == workspace_id
                )
            ).all()

    profile = bench_local.profile_sql_requests(engine, one_request, requests=3)

    assert profile["requests"] == 3
    assert len(profile["per_request"]) == 3
    for request in profile["per_request"]:
        assert {group: item["statements"] for group, item in request.items()} == {
            "edge_projection_untagged": 1,
            "other": 1,
            "ranked_candidates": 2,
        }
        ranked = request["ranked_candidates"]
        assert ranked["execute_only_ms"] >= SLEEP_MS
        assert ranked["fetch_inclusive_ms"] >= SLEEP_MS
        # A generous ceiling: a unit or scale slip cannot hide under it.
        assert ranked["execute_only_ms"] < 20 * SLEEP_MS
        assert ranked["fetch_inclusive_ms"] < 20 * SLEEP_MS
    summary = profile["summary"]["ranked_candidates"]
    assert summary["statements"]["p50"] == 2
    assert summary["execute_only_ms"]["p50"] >= SLEEP_MS
    assert summary["fetch_inclusive_ms"]["p50"] >= SLEEP_MS
    explained = profile["explain_query_plan"]
    assert [(item["group"], item["executions"]) for item in explained] == [
        ("ranked_candidates", 3),
        ("edge_projection_untagged", 3),
    ]
    for item in explained:
        assert "edge_projection" in item["statement"]
        assert item["plan"]
        assert any("edge_projection" in step["detail"] for step in item["plan"])


def test_sql_profile_times_the_fetch_of_a_streaming_statement(session_factory):
    engine = session_factory.kw["bind"]
    with session_factory.begin() as session:
        session.execute(text("CREATE TABLE profile_numbers (k INTEGER NOT NULL)"))
        session.execute(text("CREATE INDEX profile_numbers_k ON profile_numbers (k)"))
        session.execute(
            text(
                "WITH RECURSIVE n(x) AS "
                "(SELECT 0 UNION ALL SELECT x + 1 FROM n WHERE x < 99999) "
                "INSERT INTO profile_numbers SELECT x / 2 FROM n"
            )
        )
    # Grouped in index order, so SQLite hands out each group as it reaches it
    # and the cursor execute returns with the first of 50,000, the way a rank
    # denominator over many sources does. Two such statements share the tag,
    # and an untagged one lands in "other".
    grouped = "SELECT k, count(*) FROM profile_numbers GROUP BY k"
    half = "SELECT k, count(*) FROM profile_numbers WHERE k < 25000 GROUP BY k"
    untagged = "SELECT k FROM profile_numbers WHERE k < 10000"

    def one_request() -> None:
        with session_factory() as session:
            for statement in (grouped, half):
                session.execute(
                    text(statement).execution_options(
                        graph_rank_phase="rank_denominators"
                    )
                ).all()
            session.execute(text(untagged)).all()

    profile = bench_local.profile_sql_requests(engine, one_request, requests=3)
    direct_ms = _execute_fetchall_ms(engine, grouped)
    both_ms = direct_ms + _execute_fetchall_ms(engine, half)
    untagged_ms = _execute_fetchall_ms(engine, untagged)

    for request in profile["per_request"]:
        item = request["rank_denominators"]
        assert item["statements"] == 2
        # The group adds up every statement it holds.
        assert item["rows"] == 75_000
        # Execute alone stops at each statement's first group ...
        assert item["execute_only_ms"] < 0.2 * both_ms
        # ... so only the fetch-inclusive time carries their cost, once each.
        assert 0.5 * both_ms <= item["fetch_inclusive_ms"] <= 3 * both_ms
        other = request["other"]
        assert other["rows"] == 20_000
        assert 0.5 * untagged_ms <= other["fetch_inclusive_ms"] <= 3 * untagged_ms
    summary = profile["summary"]["rank_denominators"]
    assert summary["rows"]["p50"] == 75_000
    assert summary["fetch_inclusive_ms"]["p50"] >= 0.5 * both_ms


def test_explain_flags_a_full_scan_of_an_aliased_edge_projection(session_factory):
    with session_factory() as session:
        workspace_id = _workspace_id(session)
    engine = session_factory.kw["bind"]
    edge = aliased(EdgeProjection, name="d")

    def one_request() -> None:
        with session_factory() as session:
            # No index leads with weight, so SQLite reads a covering index end
            # to end: "SCAN d USING COVERING INDEX ...", a scan all the same.
            session.execute(select(edge.id).where(edge.weight > 1.0)).all()
            session.execute(
                select(edge.id).where(
                    edge.workspace_id == workspace_id,
                    edge.target_kind == "context_page",
                    edge.target_id == SEED,
                )
            ).all()
            # Hand-written SQL may alias the table without AS.
            session.execute(
                text("SELECT e.id FROM edge_projection e WHERE e.weight > 1.0")
            ).all()

    profile = bench_local.profile_sql_requests(engine, one_request, requests=1)

    explained = profile["explain_query_plan"]
    assert [
        (item["edge_projection_aliases"], item["edge_projection_scan"])
        for item in explained
    ] == [(["d"], True), (["d"], False), (["e"], True)]
    # The plan names the alias alone, which is why the aliases are needed.
    assert explained[0]["plan"][0]["detail"].startswith("SCAN d")
    assert explained[1]["plan"][0]["detail"].startswith("SEARCH d ")
    assert explained[2]["plan"][0]["detail"].startswith("SCAN e")

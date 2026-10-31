"""The ranked Local Graph collector's statements: few, indexed, dialect-neutral.

Each frontier uses two window statements (one per stored orientation), plus
one selected-node induced-relation statement. Rank arithmetic stays in Python:
SQLite and PostgreSQL disagree on log/power.
"""

from __future__ import annotations

import math
import os
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import GraphBudget, GraphQueryRequest, GraphSeed
from backend.scripts.seed_dev import seed_default_workspace
from backend.services import graph_service as graph_service_module
from backend.services.graph_query_service import GraphQueryService
from backend.services.graph_rank import (
    citation_weight,
    rank_score,
    tree_weight,
    wikilink_weight,
)
from backend.services.graph_service import (
    HYDRATORS,
    RANK_DENOMINATOR_BATCH_SIZE,
    GraphService,
)
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

pytestmark = pytest.mark.integration

RANK_PHASES = ("ranked_candidates", "rank_denominators", "induced_relations")


def _postgres_url() -> str | None:
    value = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


def _normalized(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.casefold()).strip()


def _workspace_id(session) -> UUID:
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _actor(role: str = "viewer") -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _page(session, workspace_id, *, published=True):
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=f"shape-{uuid4()}",
        title="Shape",
        page_type="concept",
        lifecycle_status="published" if published else "draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        id=uuid4(),
        page_id=page.id,
        version_no=1,
        body_md="# Shape",
        content_hash=f"shape-{page.id}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot" if published else "draft",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    if published:
        page.current_published_version_id = version.id
    else:
        page.latest_draft_version_id = version.id
    return page


def _folder(session, workspace_id):
    folder = WorkspaceTreeNode(
        id=uuid4(),
        workspace_id=workspace_id,
        section="context",
        node_kind="folder",
        sort_key=uuid4().hex,
        title="Folder",
        slug=f"shape-folder-{uuid4()}",
        visibility_status="visible",
    )
    session.add(folder)
    session.flush()
    return folder


def _edge(session, workspace_id, source, target, kind, weight=1.0):
    def ref(value):
        if isinstance(value, WorkspaceTreeNode):
            return ("workspace_tree_node", value.id)
        return ("context_page", value.id)

    (source_kind, source_id), (target_kind, target_id) = ref(source), ref(target)
    session.add(
        EdgeProjection(
            workspace_id=workspace_id,
            source_kind=source_kind,
            source_id=source_id,
            target_kind=target_kind,
            target_id=target_id,
            edge_kind=kind,
            provenance="usage" if kind == "co_access" else "curated",
            weight=weight,
            source_ref={},
        )
    )


def _seeds_with_every_kind(session, workspace_id, count: int) -> list[UUID]:
    seeds = [_page(session, workspace_id) for _ in range(count)]
    for seed in seeds:
        for _ in range(3):
            _edge(session, workspace_id, _page(session, workspace_id), seed, "wikilink")
        _edge(session, workspace_id, seed, _page(session, workspace_id), "wikilink")
        _edge(
            session, workspace_id, seed, _page(session, workspace_id), "citation_page"
        )
        _edge(session, workspace_id, _folder(session, workspace_id), seed, "tree")
        partner = _page(session, workspace_id)
        first, second = sorted((seed, partner), key=lambda page: str(page.id))
        _edge(session, workspace_id, first, second, "co_access", 3.0)
    return [seed.id for seed in seeds]


def _capture(engine, operation):
    captured: list[tuple[str | None, str, object]] = []

    def listener(_conn, _cursor, statement, parameters, context, executemany):
        if executemany or not statement.lstrip().casefold().startswith(
            ("select", "with")
        ):
            return
        captured.append(
            (
                context.execution_options.get("graph_rank_phase"),
                statement,
                parameters,
            )
        )

    event.listen(engine, "before_cursor_execute", listener)
    try:
        operation()
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    return captured


def _query(session, workspace_id, seeds, *, direction="both", edges=150):
    return GraphQueryService().query(
        session,
        workspace_id,
        request=GraphQueryRequest(
            seeds=[GraphSeed(kind="context_page", id=seed) for seed in seeds],
            direction=direction,
            budget=GraphBudget(nodes=250, edges=edges),
        ),
        actor=_actor(),
        usage_signals_enabled=True,
    )


@pytest.mark.parametrize("seed_count", [1, 8])
def test_statement_shape(session_factory, seed_count):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeds = _seeds_with_every_kind(session, workspace_id, seed_count)
    engine = session_factory.kw["bind"]

    payload = {}

    def run() -> None:
        with session_factory() as session:
            payload["scene"] = _query(session, workspace_id, seeds)

    captured = _capture(engine, run)

    ranked = [row for row in captured if row[0] == "ranked_candidates"]
    denominators = [row for row in captured if row[0] == "rank_denominators"]
    # Pages that write links or citations, and folders placing a seed.
    page_sources = seed_count * 4
    folder_sources = seed_count
    assert len(ranked) == 4
    induced = [row for row in captured if row[0] == "induced_relations"]
    assert len(induced) == 1
    # Per family: one statement for the stored totals and one grouped
    # count; no source here is large enough for a capped statement.
    assert (
        1
        <= len(denominators)
        <= 2 * math.ceil(page_sources / RANK_DENOMINATOR_BATCH_SIZE)
        + 2 * math.ceil(folder_sources / RANK_DENOMINATOR_BATCH_SIZE)
    )
    edge_statements = [
        _normalized(statement)
        for _phase, statement, _parameters in captured
        # The table, not workspace.edge_projection_version.
        if re.search(r"\bedge_projection\b", statement.casefold())
    ]
    # No per-seed statement: every edge_projection read is a tagged one.
    assert len(edge_statements) == len(ranked) + len(denominators) + len(induced)
    for statement in edge_statements:
        assert "edge_projection.workspace_id =" in statement
    for _phase, statement, _parameters in captured:
        lowered = statement.casefold()
        for function in ("log(", "ln(", "power(", "julianday"):
            assert function not in lowered, (function, statement)
    kinds = {edge.edge_kind for edge in payload["scene"].edges}
    assert kinds == {"wikilink", "citation_page", "tree", "co_access"}


def test_statements_search_edge_projection_by_index(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeds = _seeds_with_every_kind(session, workspace_id, 4)
        # Background relations the seeds do not touch.
        pool = [_page(session, workspace_id) for _ in range(60)]
        for index, source in enumerate(pool):
            for offset in range(1, 36):
                _edge(
                    session,
                    workspace_id,
                    source,
                    pool[(index + offset) % len(pool)],
                    "wikilink",
                )
        edge_count = session.scalar(
            select(func.count())
            .select_from(EdgeProjection)
            .where(EdgeProjection.workspace_id == workspace_id)
        )
    assert edge_count >= 2_000
    engine = session_factory.kw["bind"]
    with engine.connect() as connection:
        connection.exec_driver_sql("ANALYZE")

    def run() -> None:
        with session_factory() as session:
            _query(session, workspace_id, seeds)

    captured = [row for row in _capture(engine, run) if row[0] in RANK_PHASES]
    denominators = [row for row in captured if row[0] == "rank_denominators"]
    assert sum(row[0] == "ranked_candidates" for row in captured) == 4
    assert sum(row[0] == "induced_relations" for row in captured) == 1
    # The seeds' pages and their folders are counted in one statement.
    assert 1 <= len(denominators) <= 3
    assert {"context_page", "workspace_tree_node"} <= {
        value for row in denominators for value in row[2]
    }

    with engine.connect() as connection:
        for phase, statement, parameters in captured:
            plan = [
                str(row[-1])
                for row in connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + statement, parameters
                )
            ]
            edge_steps = [step for step in plan if "edge_projection" in step]
            assert edge_steps, (phase, plan)
            assert all(
                not step.startswith("SCAN edge_projection") for step in edge_steps
            ), (phase, plan)
            assert any(
                step.startswith("SEARCH edge_projection") for step in edge_steps
            ), (phase, plan)


def _backlink_fixture(session_factory) -> tuple[UUID, UUID]:
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id)
        focused = _page(session, workspace_id)
        spread = _page(session, workspace_id)
        _edge(session, workspace_id, focused, seed, "wikilink", 1.0)
        _edge(session, workspace_id, spread, seed, "wikilink", 2.0)
        for _ in range(5):
            _edge(
                session, workspace_id, spread, _page(session, workspace_id), "wikilink"
            )
        # A citation the viewer cannot see must not move the visible one.
        _edge(
            session,
            workspace_id,
            seed,
            _page(session, workspace_id),
            "citation_page",
            2.0,
        )
        _edge(
            session,
            workspace_id,
            seed,
            _page(session, workspace_id, published=False),
            "citation_page",
            5.0,
        )
        folder = _folder(session, workspace_id)
        _edge(session, workspace_id, folder, seed, "tree")
        _edge(session, workspace_id, folder, _page(session, workspace_id), "tree")
        return workspace_id, seed.id


def _ranked_pairs(session_factory, workspace_id, seed_id):
    service = GraphService()
    actor = _actor()
    with session_factory() as session:
        edges = service.ranked_projection_edges(
            session,
            workspace_id,
            seeds=(("context_page", seed_id),),
            actor=actor,
            edge_kinds=("citation_page", "tree", "wikilink"),
            neighbor_kinds=tuple(sorted(HYDRATORS)),
            occurred_from=None,
            occurred_to=None,
            direction="both",
            edge_budget=150,
            now=NOW,
            hydration_context=service.hydration_context(session, workspace_id, actor),
        )
    return [
        ((edge.edge_kind, edge.direction), round(edge.rank_score, 9)) for edge in edges
    ]


def _shape_of(pairs):
    """Identity-free: fixture ids differ per database, kinds and ranks do not."""

    return sorted(pairs)


NOW = datetime(2026, 9, 25, tzinfo=UTC)
EXPECTED_SHAPE = sorted(
    [
        # One link from a page linking only the seed.
        (
            ("wikilink", "in"),
            round(rank_score("wikilink", wikilink_weight(1.0, 1), None, NOW), 9),
        ),
        # Two from a page linking six.
        (
            ("wikilink", "in"),
            round(rank_score("wikilink", wikilink_weight(2.0, 6), None, NOW), 9),
        ),
        # The seed's visible citations total 2; the draft's 5 is not counted.
        (
            ("citation_page", "out"),
            round(rank_score("citation_page", citation_weight(2.0, 2.0), None, NOW), 9),
        ),
        # The folder holds the seed and one other visible page.
        (("tree", "in"), round(rank_score("tree", tree_weight(1), None, NOW), 9)),
    ]
)


def test_ranked_projection_sql_runs_on_sqlite(session_factory):
    workspace_id, seed_id = _backlink_fixture(session_factory)
    pairs = _ranked_pairs(session_factory, workspace_id, seed_id)
    assert _shape_of(pairs) == EXPECTED_SHAPE


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires a dedicated PostgreSQL migration test database",
)
def test_ranked_projection_sql_runs_on_postgres():
    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    drop_reflected_postgres_metadata(engine)
    try:
        command.upgrade(config, "head")
        postgres_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with postgres_factory.begin() as session:
            seed_default_workspace(session)
        postgres_workspace, postgres_seed = _backlink_fixture(postgres_factory)
        actual = _shape_of(
            _ranked_pairs(postgres_factory, postgres_workspace, postgres_seed)
        )
    finally:
        drop_reflected_postgres_metadata(engine)
        engine.dispose()

    assert actual == EXPECTED_SHAPE


def test_a_large_source_is_read_only_up_to_the_row_cap(session_factory, monkeypatch):
    """An Inbox-sized folder costs a bounded read, not one check per child."""

    monkeypatch.setattr(graph_service_module, "RANK_DENOMINATOR_ROW_CAP", 8)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id)
        inbox = _folder(session, workspace_id)
        _edge(session, workspace_id, inbox, seed, "tree")
        for _ in range(40):
            _edge(session, workspace_id, inbox, _page(session, workspace_id), "tree")
        seed_id = seed.id
    engine = session_factory.kw["bind"]

    def run() -> None:
        with session_factory() as session:
            _query(session, workspace_id, [seed_id])

    capped = [
        _normalized(statement)
        for phase, statement, _parameters in _capture(engine, run)
        if phase == "rank_denominators" and " limit " in _normalized(statement)
    ]

    # Initial frontier, second frontier and induced-rank reads are separate
    # actor-visible denominator phases; each remains bounded by the row cap.
    assert 1 <= len(capped) <= 3

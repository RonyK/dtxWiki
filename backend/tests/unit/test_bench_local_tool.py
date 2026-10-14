"""F08-T1: bench_local reports edge kinds and can bench one named seed."""

from __future__ import annotations

import sys
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import EdgeProjection
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from scripts.bench.graph import bench_local

pytestmark = pytest.mark.unit


def _viewer() -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role="viewer",
    )


def _publish(page_service, slug: str):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=slug.title(),
        body_md=f"# {slug.title()}\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )


def test_response_details_count_edges_by_kind():
    details = bench_local._response_details(
        {
            "schema_version": "graph.v1",
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [
                {"edge_kind": "wikilink"},
                {"edge_kind": "tree"},
                {"edge_kind": "wikilink"},
            ],
            "completeness": {"capped": False},
            "truncated": False,
            "warnings": [],
        }
    )

    assert details["edges_by_kind"] == {"tree": 1, "wikilink": 2}
    # Sorted by kind, so two result files diff line by line.
    assert list(details["edges_by_kind"]) == ["tree", "wikilink"]
    assert details["edges"] == 3


def test_seed_page_option_benches_one_explicit_profile(
    session_factory, page_service, monkeypatch
):
    hub = _publish(page_service, "hub")
    leaf = _publish(page_service, "leaf")
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        # One link gives both pages degree 1: the corpus has no degree-zero
        # page, which the hub/median/isolated selection refuses to run on.
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=leaf.id,
                target_kind="context_page",
                target_id=hub.id,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_local.py",
            "--db",
            "corpus.db",
            "--workspace-id",
            str(workspace_id),
            "--seed-page",
            str(hub.id),
        ],
    )

    args = bench_local.parse_args()

    assert args.seed_page == hub.id
    with session_factory() as session:
        with pytest.raises(RuntimeError, match="degree-zero"):
            bench_local.select_seed_profiles(session, workspace_id, actor=_viewer())
        profiles = bench_local.resolve_seed_profiles(
            session,
            workspace_id,
            actor=_viewer(),
            seed_page=args.seed_page,
        )
    assert profiles == {
        "explicit": {"page_id": hub.id, "degree": 1, "selection": "explicit"}
    }

    # Backlinks are written into the named seed, so they cannot run without one.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_local.py",
            "--db",
            "corpus.db",
            "--workspace-id",
            str(workspace_id),
            "--add-hub-backlinks",
            "10",
        ],
    )
    with pytest.raises(SystemExit):
        bench_local.parse_args()


def test_edge_projection_aliases_skip_keywords_and_lookalike_names():
    statement = (
        "SELECT d.id FROM edge_projection AS d "
        'JOIN "edge_projection" AS "up" ON up.target_id = d.source_id '
        "LEFT OUTER JOIN edge_projection e ON e.source_id = d.target_id "
        "WHERE d.id IN (SELECT edge_projection.id FROM edge_projection "
        "WHERE edge_projection.weight > 1) "
        "AND d.workspace_id IN (SELECT workspace.id FROM workspace "
        "WHERE workspace.edge_projection_version > 0)"
    )

    assert bench_local.edge_projection_aliases(statement) == ["d", "up", "e"]
    # Keywords in lowercase SQL are keywords too.
    assert bench_local.edge_projection_aliases(
        "select d.id from edge_projection as d where d.weight > 1"
    ) == ["d"]
    # A keyword after the bare table name is not an alias, nor is an index.
    assert (
        bench_local.edge_projection_aliases(
            "SELECT id FROM edge_projection INDEXED BY ix_edge_projection_source "
            "WHERE weight > 1 ORDER BY id"
        )
        == []
    )


@pytest.mark.parametrize(
    ("detail", "scan"),
    [
        ("SCAN edge_projection", True),
        ("SCAN d", True),
        # Every index entry is still visited.
        ("SCAN d USING INDEX ix_edge_projection_source", True),
        ("SCAN d USING COVERING INDEX ix_edge_projection_source", True),
        # SQLite before 3.36.
        ("SCAN TABLE edge_projection AS d", True),
        # Names compare without case or schema.
        ("SCAN EDGE_PROJECTION", True),
        ("SCAN main.edge_projection", True),
        # An automatic index is built by reading the whole table.
        ("SEARCH d USING AUTOMATIC COVERING INDEX (provenance=?)", True),
        ("SEARCH edge_projection USING AUTOMATIC INDEX (target_id=?)", True),
        ("SEARCH d USING INDEX ix_edge_projection_target (workspace_id=?)", False),
        ("SEARCH edge_projection USING INTEGER PRIMARY KEY (rowid=?)", False),
        ("SCAN dd", False),
        ("SCAN context_page", False),
        ("USE TEMP B-TREE FOR ORDER BY", False),
    ],
)
def test_plan_scan_flag_counts_every_scan_of_edge_projection(detail, scan):
    assert bench_local.plan_scans_edge_projection([detail], ["d"]) is scan

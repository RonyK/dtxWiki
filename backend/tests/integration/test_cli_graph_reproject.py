from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pg8000.dbapi
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from backend.app.db import make_engine, make_session_factory
from backend.cli.main import app
from backend.models.base import Base
from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.graph import GraphHistoryCoverage, GraphObservation
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.graph_observation_service import GraphObservationService
from backend.services.page_service import PageService


@dataclass(frozen=True)
class _WorkspaceFixture:
    id: UUID
    slug: str
    page_ids: tuple[UUID, UUID, UUID]
    expected_targets: tuple[tuple[UUID, UUID], ...]


@dataclass(frozen=True)
class _CoverageState:
    id: UUID
    basis: str
    status: str
    coverage_from: datetime | None
    watermark: datetime | None
    extractor_version: str
    created_at: datetime
    updated_at: datetime

    @property
    def retained_evidence(self):
        return (
            self.id,
            self.basis,
            self.coverage_from,
            self.watermark,
            self.extractor_version,
            self.created_at,
        )


@pytest.mark.parametrize("selector_kind", ["slug", "id"])
def test_graph_reproject_repairs_one_workspace_once_and_is_repeatable(
    tmp_path: Path,
    selector_kind: str,
):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / f"selector-{selector_kind}.sqlite",
        workspace_slugs=("default", "other"),
    )
    selected = fixtures["default"]
    untouched = fixtures["other"]
    _erase_authored_link_projections(session_factory, selected.id)
    _erase_authored_link_projections(session_factory, untouched.id)

    selected_version_before = _edge_projection_version(session_factory, selected.id)
    untouched_version_before = _edge_projection_version(session_factory, untouched.id)
    selected_observations_before = _observation_ids(session_factory, selected.id)
    untouched_observations_before = _observation_ids(session_factory, untouched.id)
    non_wikilink_before = _non_wikilink_edge_snapshot(
        session_factory,
        selected.id,
    )
    selector = selected.slug if selector_kind == "slug" else str(selected.id)

    first = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            selector,
            "--db",
            database_url,
        ],
    )

    assert first.exit_code == 0, first.output
    assert "page_link_changes=+6/~0/-0" in first.output
    assert "edge_changes=+3/~0/-0" in first.output
    _assert_authored_link_projection(session_factory, selected)
    assert _page_link_snapshot(session_factory, untouched.id) == []
    assert _wikilink_edge_snapshot(session_factory, untouched.id) == []
    assert _edge_projection_version(
        session_factory,
        selected.id,
    ) == selected_version_before + 1
    assert (
        _edge_projection_version(session_factory, untouched.id)
        == untouched_version_before
    )
    assert (
        _observation_ids(session_factory, selected.id)
        == selected_observations_before
    )
    assert (
        _observation_ids(session_factory, untouched.id)
        == untouched_observations_before
    )
    assert (
        _non_wikilink_edge_snapshot(session_factory, selected.id)
        == non_wikilink_before
    )

    page_links_after_first = _page_link_semantic_snapshot(
        session_factory,
        selected.id,
    )
    page_link_rows_after_first = _page_link_identity_snapshot(
        session_factory,
        selected.id,
    )
    wikilinks_after_first = _wikilink_edge_snapshot(session_factory, selected.id)
    version_after_first = _edge_projection_version(session_factory, selected.id)

    second = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            selector,
            "--db",
            database_url,
        ],
    )

    assert second.exit_code == 0, second.output
    assert "page_link_changes=+0/~0/-0" in second.output
    assert "edge_changes=+0/~0/-0" in second.output
    assert (
        _page_link_semantic_snapshot(session_factory, selected.id)
        == page_links_after_first
    )
    assert (
        _page_link_identity_snapshot(session_factory, selected.id)
        == page_link_rows_after_first
    )
    assert (
        _wikilink_edge_snapshot(session_factory, selected.id)
        == wikilinks_after_first
    )
    assert _edge_projection_version(
        session_factory,
        selected.id,
    ) == version_after_first
    assert (
        _observation_ids(session_factory, selected.id)
        == selected_observations_before
    )
    assert (
        _non_wikilink_edge_snapshot(session_factory, selected.id)
        == non_wikilink_before
    )


def test_graph_reproject_all_workspaces_repairs_each_workspace(tmp_path: Path):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / "all-workspaces.sqlite",
        workspace_slugs=("default", "other"),
    )
    versions_before = {
        fixture.id: _edge_projection_version(session_factory, fixture.id)
        for fixture in fixtures.values()
    }
    observations_before = {
        fixture.id: _observation_ids(session_factory, fixture.id)
        for fixture in fixtures.values()
    }
    non_wikilinks_before = {
        fixture.id: _non_wikilink_edge_snapshot(session_factory, fixture.id)
        for fixture in fixtures.values()
    }
    for fixture in fixtures.values():
        _erase_authored_link_projections(session_factory, fixture.id)

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--all-workspaces",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    for fixture in fixtures.values():
        _assert_authored_link_projection(session_factory, fixture)
        assert (
            _edge_projection_version(session_factory, fixture.id)
            == versions_before[fixture.id] + 1
        )
        assert (
            _observation_ids(session_factory, fixture.id)
            == observations_before[fixture.id]
        )
        assert (
            _non_wikilink_edge_snapshot(session_factory, fixture.id)
            == non_wikilinks_before[fixture.id]
        )


def test_graph_reproject_requires_exactly_one_workspace_selector(tmp_path: Path):
    database_url, _session_factory, _fixtures = _database_with_linked_workspaces(
        tmp_path / "selector-contract.sqlite",
        workspace_slugs=("default",),
    )
    runner = CliRunner()

    missing = runner.invoke(
        app,
        ["graph", "reproject", "--db", database_url],
    )
    conflicting = runner.invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            "default",
            "--all-workspaces",
            "--db",
            database_url,
        ],
    )

    assert missing.exit_code == 2, missing.output
    assert conflicting.exit_code == 2, conflicting.output
    assert "--workspace" in missing.output
    assert "--all-workspaces" in missing.output
    assert "--workspace" in conflicting.output
    assert "--all-workspaces" in conflicting.output


def test_graph_reproject_rolls_back_only_the_failing_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / "rollback.sqlite",
        workspace_slugs=("default", "other"),
    )
    successful = fixtures["default"]
    failing = fixtures["other"]
    for fixture in fixtures.values():
        _erase_authored_link_projections(session_factory, fixture.id)
    failing_before = _workspace_projection_snapshot(session_factory, failing.id)
    successful_version_before = _edge_projection_version(
        session_factory,
        successful.id,
    )

    from backend.cli.commands import graph as graph_commands

    original = graph_commands.reproject_workspace_graph

    def _fail_after_reproject(session, workspace_id: UUID):
        report = original(session, workspace_id)
        if workspace_id == failing.id:
            raise RuntimeError("injected graph reprojection failure")
        return report

    monkeypatch.setattr(
        graph_commands,
        "reproject_workspace_graph",
        _fail_after_reproject,
    )

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--all-workspaces",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 1, result.output
    assert "other" in result.output
    assert "error_type=RuntimeError" in result.output
    assert "injected graph reprojection failure" not in result.output
    _assert_authored_link_projection(session_factory, successful)
    assert _edge_projection_version(
        session_factory,
        successful.id,
    ) == successful_version_before + 1
    assert _workspace_projection_snapshot(session_factory, failing.id) == failing_before

    verbose_result = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--all-workspaces",
            "--verbose-errors",
            "--db",
            database_url,
        ],
    )
    assert verbose_result.exit_code == 1, verbose_result.output
    assert "injected graph reprojection failure" in verbose_result.output
    assert _workspace_projection_snapshot(session_factory, failing.id) == failing_before


def test_graph_reproject_rejects_cross_page_version_pointer_atomically(
    tmp_path: Path,
):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / "cross-page-pointer.sqlite",
        workspace_slugs=("default",),
    )
    fixture = fixtures["default"]
    _erase_authored_link_projections(session_factory, fixture.id)
    with session_factory.begin() as session:
        corrupted = session.get(ContextPage, fixture.page_ids[0])
        pointer_owner = session.get(ContextPage, fixture.page_ids[1])
        assert corrupted is not None
        assert pointer_owner is not None
        assert pointer_owner.current_version_id is not None
        corrupted.current_version_id = pointer_owner.current_version_id
    before = _workspace_projection_snapshot(session_factory, fixture.id)

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            fixture.slug,
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 1, result.output
    assert "code=invalid_context_version_pointer" in result.output
    assert "transaction=rolled_back" in result.output
    assert "context history repair" in result.output
    assert _workspace_projection_snapshot(session_factory, fixture.id) == before


def test_graph_reproject_does_not_use_legacy_edge_refresh_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / "legacy-edge-tripwire.sqlite",
        workspace_slugs=("default",),
    )
    fixture = fixtures["default"]
    _erase_authored_link_projections(session_factory, fixture.id)

    from backend.services import graph_reprojection
    from backend.services.indexing import edge_registry

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("legacy edge refresh path was called")

    for module in (edge_registry, graph_reprojection):
        monkeypatch.setattr(
            module,
            "rebuild_edge_projection",
            _forbidden,
            raising=False,
        )
        monkeypatch.setattr(
            module,
            "refresh_page_edge_projection",
            _forbidden,
            raising=False,
        )

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            fixture.slug,
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    _assert_authored_link_projection(session_factory, fixture)


@pytest.mark.parametrize("postgres_code", ("40P01", "23505"))
def test_graph_reproject_retries_one_pg8000_retryable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    postgres_code: str,
):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / f"postgres-retry-{postgres_code}.sqlite",
        workspace_slugs=("default",),
    )
    fixture = fixtures["default"]
    _erase_authored_link_projections(session_factory, fixture.id)
    revision_before = _edge_projection_version(session_factory, fixture.id)

    from backend.cli.commands import graph as graph_commands

    original = graph_commands.reproject_workspace_graph
    attempts = 0

    def _fail_once(session, workspace_id: UUID):
        nonlocal attempts
        attempts += 1
        report = original(session, workspace_id)
        if attempts == 1:
            raise _pg8000_dbapi_error(postgres_code)
        return report

    monkeypatch.setattr(
        graph_commands,
        "reproject_workspace_graph",
        _fail_once,
    )

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            fixture.slug,
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert attempts == 2
    assert f"code={postgres_code} attempt=2/2" in result.output
    _assert_authored_link_projection(session_factory, fixture)
    assert (
        _edge_projection_version(session_factory, fixture.id)
        == revision_before + 1
    )


def test_graph_reproject_stops_after_second_postgres_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / "deadlock-exhausted.sqlite",
        workspace_slugs=("default",),
    )
    fixture = fixtures["default"]
    _erase_authored_link_projections(session_factory, fixture.id)
    before = _workspace_projection_snapshot(session_factory, fixture.id)

    from backend.cli.commands import graph as graph_commands

    original = graph_commands.reproject_workspace_graph
    attempts = 0

    def _always_deadlock(session, workspace_id: UUID):
        nonlocal attempts
        attempts += 1
        original(session, workspace_id)
        raise _pg8000_dbapi_error("40P01")

    monkeypatch.setattr(
        graph_commands,
        "reproject_workspace_graph",
        _always_deadlock,
    )

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            fixture.slug,
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 1, result.output
    assert attempts == 2
    assert "code=40P01 attempt=2/2" in result.output
    assert "code=40P01 transaction=rolled_back" in result.output
    assert _workspace_projection_snapshot(session_factory, fixture.id) == before


def test_graph_reproject_does_not_retry_pg8000_non_retryable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / "postgres-non-retryable.sqlite",
        workspace_slugs=("default",),
    )
    fixture = fixtures["default"]
    _erase_authored_link_projections(session_factory, fixture.id)
    before = _workspace_projection_snapshot(session_factory, fixture.id)

    from backend.cli.commands import graph as graph_commands

    original = graph_commands.reproject_workspace_graph
    attempts = 0

    def _fail(session, workspace_id: UUID):
        nonlocal attempts
        attempts += 1
        original(session, workspace_id)
        raise _pg8000_dbapi_error("42P01")

    monkeypatch.setattr(graph_commands, "reproject_workspace_graph", _fail)

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            fixture.slug,
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 1, result.output
    assert attempts == 1
    assert "retrying" not in result.output
    assert "code=42P01 transaction=rolled_back" in result.output
    assert _workspace_projection_snapshot(session_factory, fixture.id) == before


@pytest.mark.parametrize(
    "error",
    (
        # Not a database error, even though it carries a pg8000-shaped mapping.
        RuntimeError({"C": "40P01", "M": "application error"}),
        # A driver error whose code is not a SQLSTATE is not a retry signal,
        # and its text must not be echoed as one.
        DBAPIError.instance(
            "graph reproject",
            {},
            pg8000.dbapi.ProgrammingError({"C": "40P01 injected", "M": "x"}),
            pg8000.dbapi.Error,
        ),
    ),
    ids=("non-database-mapping", "malformed-sqlstate"),
)
def test_graph_reproject_retries_only_database_sqlstates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / "postgres-not-a-sqlstate.sqlite",
        workspace_slugs=("default",),
    )
    fixture = fixtures["default"]
    _erase_authored_link_projections(session_factory, fixture.id)
    before = _workspace_projection_snapshot(session_factory, fixture.id)

    from backend.cli.commands import graph as graph_commands

    original = graph_commands.reproject_workspace_graph
    attempts = 0

    def _fail(session, workspace_id: UUID):
        nonlocal attempts
        attempts += 1
        original(session, workspace_id)
        raise error

    monkeypatch.setattr(graph_commands, "reproject_workspace_graph", _fail)

    result = CliRunner().invoke(
        app,
        ["graph", "reproject", "--workspace", fixture.slug, "--db", database_url],
    )

    assert result.exit_code == 1, result.output
    assert attempts == 1
    assert "retrying" not in result.output
    assert "code=reproject_failed transaction=rolled_back" in result.output
    assert "40P01" not in result.output
    assert _workspace_projection_snapshot(session_factory, fixture.id) == before


def test_graph_reproject_backfills_page_link_domain_event_times(tmp_path: Path):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / "page-link-domain-times.sqlite",
        workspace_slugs=("default",),
    )
    fixture = fixtures["default"]
    page_service = PageService(session_factory)

    early_target = _write_fixture_page(
        page_service,
        slug="created-before-source",
        body_md="# Created before source",
    )
    later_source = _write_fixture_page(
        page_service,
        slug="source-created-later",
        body_md="# Source\n\n[[created-before-source]]",
    )
    early_source = _write_fixture_page(
        page_service,
        slug="source-before-target",
        body_md="# Source before target\n\n[[created-after-source]]",
    )
    late_target = _write_fixture_page(
        page_service,
        slug="created-after-source",
        body_md="# Created after source",
    )

    with session_factory() as session:
        later_source_version = session.get(
            ContextPageVersion,
            later_source.version_id,
        )
        early_source_version = session.get(
            ContextPageVersion,
            early_source.version_id,
        )
        early_target_page = session.get(ContextPage, early_target.id)
        late_target_page = session.get(ContextPage, late_target.id)
        assert later_source_version is not None
        assert early_source_version is not None
        assert early_target_page is not None
        assert late_target_page is not None
        assert later_source_version.created_at > early_target_page.created_at
        assert late_target_page.created_at > early_source_version.created_at
        later_source_time = later_source_version.created_at
        early_source_time = early_source_version.created_at
        early_target_time = early_target_page.created_at
        late_target_time = late_target_page.created_at

    _erase_authored_link_projections(session_factory, fixture.id)
    result = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            fixture.slug,
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    later_source_link = _page_link_for_version(
        session_factory,
        later_source.id,
        later_source.version_id,
        target_slug=early_target.slug,
    )
    early_source_link = _page_link_for_version(
        session_factory,
        early_source.id,
        early_source.version_id,
        target_slug=late_target.slug,
    )
    assert later_source_link.target_page_id == early_target.id
    assert later_source_link.created_at == later_source_time
    assert later_source_link.created_at == max(
        later_source_time,
        early_target_time,
    )
    assert early_source_link.target_page_id == late_target.id
    assert early_source_link.created_at == late_target_time
    assert early_source_link.created_at == max(
        early_source_time,
        late_target_time,
    )


def test_graph_reproject_demotes_only_changed_source_history_coverage(
    tmp_path: Path,
):
    database_url, session_factory, fixtures = _database_with_linked_workspaces(
        tmp_path / "history-coverage.sqlite",
        workspace_slugs=("default",),
    )
    fixture = fixtures["default"]
    page_service = PageService(session_factory)
    no_coverage_source = _write_fixture_page(
        page_service,
        slug="changed-without-coverage",
        body_md="# Changed without coverage\n\n[[default-alpha]]",
    )
    unaffected_complete = _write_fixture_page(
        page_service,
        slug="unaffected-complete",
        body_md="# Unaffected complete",
    )
    unaffected_reconstructed = _write_fixture_page(
        page_service,
        slug="unaffected-reconstructed",
        body_md="# Unaffected reconstructed",
    )
    affected_complete_id = fixture.page_ids[0]
    affected_reconstructed_id = fixture.page_ids[1]
    affected_partial_id = fixture.page_ids[2]

    coverage_specs = (
        (affected_complete_id, "domain_events", "complete", "complete.v7"),
        (
            affected_reconstructed_id,
            "mixed",
            "reconstructed",
            "reconstructed.v4",
        ),
        (affected_partial_id, "observations", "partial", "partial.v2"),
        (
            unaffected_complete.id,
            "domain_events",
            "complete",
            "unaffected-complete.v3",
        ),
        (
            unaffected_reconstructed.id,
            "mixed",
            "reconstructed",
            "unaffected-reconstructed.v5",
        ),
    )
    with session_factory.begin() as session:
        coverage_service = GraphObservationService()
        for stream_id, basis, status, extractor_version in coverage_specs:
            page = session.get(ContextPage, stream_id)
            assert page is not None
            version = session.get(ContextPageVersion, page.current_version_id)
            assert version is not None
            coverage_service.set_coverage(
                session,
                fixture.id,
                stream_kind="context_page",
                stream_id=stream_id,
                basis=basis,
                status=status,
                coverage_from=version.created_at,
                watermark=version.created_at,
                extractor_version=extractor_version,
            )
        session.execute(
            delete(GraphHistoryCoverage).where(
                GraphHistoryCoverage.workspace_id == fixture.id,
                GraphHistoryCoverage.stream_kind == "context_page",
                GraphHistoryCoverage.stream_id == no_coverage_source.id,
            )
        )

    _erase_authored_link_projections(session_factory, fixture.id)
    before = _coverage_snapshot(session_factory, fixture.id)
    assert no_coverage_source.id not in before

    first = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            fixture.slug,
            "--db",
            database_url,
        ],
    )

    assert first.exit_code == 0, first.output
    after_first = _coverage_snapshot(session_factory, fixture.id)
    changed_edge_sources = {
        row[0] for row in _wikilink_edge_snapshot(session_factory, fixture.id)
    }
    assert {
        affected_complete_id,
        affected_reconstructed_id,
        affected_partial_id,
        no_coverage_source.id,
    }.issubset(changed_edge_sources)
    assert unaffected_complete.id not in changed_edge_sources
    assert unaffected_reconstructed.id not in changed_edge_sources
    for stream_id in (affected_complete_id, affected_reconstructed_id):
        assert after_first[stream_id].status == "partial"
        assert (
            after_first[stream_id].retained_evidence
            == before[stream_id].retained_evidence
        )
    assert after_first[affected_partial_id] == before[affected_partial_id]
    assert after_first[unaffected_complete.id] == before[unaffected_complete.id]
    assert (
        after_first[unaffected_reconstructed.id]
        == before[unaffected_reconstructed.id]
    )
    assert no_coverage_source.id not in after_first

    second = CliRunner().invoke(
        app,
        [
            "graph",
            "reproject",
            "--workspace",
            fixture.slug,
            "--db",
            database_url,
        ],
    )

    assert second.exit_code == 0, second.output
    assert _coverage_snapshot(session_factory, fixture.id) == after_first


def _pg8000_dbapi_error(code: str) -> DBAPIError:
    """Build the error SQLAlchemy raises for a pg8000 server error.

    pg8000's legacy API raises IntegrityError for 23505 and ProgrammingError
    for other server errors such as 40P01, with the server fields in args[0].
    ``DBAPIError.instance`` is the wrapper SQLAlchemy itself applies.
    """

    driver_class = (
        pg8000.dbapi.IntegrityError
        if code == "23505"
        else pg8000.dbapi.ProgrammingError
    )
    driver_error = driver_class(
        {"S": "ERROR", "C": code, "M": "injected PostgreSQL server error"}
    )
    return DBAPIError.instance(
        "graph reproject",
        {},
        driver_error,
        pg8000.dbapi.Error,
    )


def _database_with_linked_workspaces(
    path: Path,
    *,
    workspace_slugs: tuple[str, ...],
):
    database_url = f"sqlite+pysqlite:///{path}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        for slug in workspace_slugs:
            seed_default_workspace(
                session,
                slug=slug,
                title=slug.replace("-", " ").title(),
            )

    page_service = PageService(session_factory)
    fixtures = {
        slug: _seed_link_cycle(page_service, session_factory, slug)
        for slug in workspace_slugs
    }
    return database_url, session_factory, fixtures


def _write_fixture_page(
    page_service: PageService,
    *,
    slug: str,
    body_md: str,
):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=slug.replace("-", " ").title(),
        body_md=body_md,
        actor_kind="human",
        actor_id="graph-reproject-test",
        actor_provenance="browser_session",
    )


def _seed_link_cycle(page_service, session_factory, workspace_slug: str):
    initial_pages = [
        page_service.write_page(
            workspace_slug=workspace_slug,
            slug=f"{workspace_slug}-{name}",
            title=name.title(),
            body_md=f"# {name.title()}",
            actor_kind="human",
            actor_id="graph-reproject-test",
            actor_provenance="browser_session",
        )
        for name in ("alpha", "beta", "gamma")
    ]
    for index, source in enumerate(initial_pages):
        target = initial_pages[(index + 1) % len(initial_pages)]
        page_service.write_page(
            workspace_slug=workspace_slug,
            slug=source.slug,
            title=source.title,
            body_md=(
                f"# {source.title}\n\n"
                f"[[{target.slug}]]\n\n"
                f'[Open target](</context?target={target.id}> "Target")'
            ),
            actor_kind="human",
            actor_id="graph-reproject-test",
            actor_provenance="browser_session",
            base_version_id=source.version_id,
            base_content_hash=source.content_hash,
        )

    with session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == workspace_slug)
        )
        assert workspace is not None
        sentinel = EdgeProjection(
            workspace_id=workspace.id,
            source_kind="context_page",
            source_id=initial_pages[0].id,
            target_kind="context_page",
            target_id=initial_pages[2].id,
            edge_kind="citation_page",
            provenance="curated",
            weight=7.0,
            occurred_at=datetime(2025, 1, 2, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 3, tzinfo=UTC),
            source_ref={"source_table": "graph_reproject_test"},
        )
        session.add(sentinel)
        session.flush()
        workspace_id = workspace.id

    page_ids = tuple(page.id for page in initial_pages)
    assert len(page_ids) == 3
    expected_targets = tuple(
        (page_ids[index], page_ids[(index + 1) % len(page_ids)])
        for index in range(len(page_ids))
    )
    return _WorkspaceFixture(
        id=workspace_id,
        slug=workspace_slug,
        page_ids=page_ids,
        expected_targets=expected_targets,
    )


def _erase_authored_link_projections(session_factory, workspace_id: UUID) -> None:
    with session_factory.begin() as session:
        session.execute(delete(PageLink).where(PageLink.workspace_id == workspace_id))
        session.execute(
            delete(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "wikilink",
            )
        )


def _assert_authored_link_projection(session_factory, fixture: _WorkspaceFixture):
    assert _page_link_semantic_snapshot(session_factory, fixture.id) == sorted(
        (
            source_id,
            target_id,
            syntax,
            1,
        )
        for source_id, target_id in fixture.expected_targets
        for syntax in ("context_url", "wikilink")
    )
    edge_rows = _wikilink_edge_snapshot(session_factory, fixture.id)
    assert [row[:3] for row in edge_rows] == sorted(
        (source_id, target_id, 2.0)
        for source_id, target_id in fixture.expected_targets
    )


def _page_link_snapshot(session_factory, workspace_id: UUID):
    with session_factory() as session:
        return list(
            session.scalars(
                select(PageLink)
                .where(PageLink.workspace_id == workspace_id)
                .order_by(PageLink.source_page_id, PageLink.link_syntax)
            )
        )


def _page_link_semantic_snapshot(session_factory, workspace_id: UUID):
    with session_factory() as session:
        rows = session.execute(
            select(
                PageLink.source_page_id,
                PageLink.target_page_id,
                PageLink.link_syntax,
                PageLink.occurrence_count,
            )
            .where(PageLink.workspace_id == workspace_id)
            .order_by(
                PageLink.source_page_id,
                PageLink.target_page_id,
                PageLink.link_syntax,
            )
        ).all()
    return list(rows)


def _page_link_identity_snapshot(session_factory, workspace_id: UUID):
    """Every persisted PageLink value, including stable row provenance."""

    with session_factory() as session:
        rows = session.execute(
            select(
                PageLink.id,
                PageLink.workspace_id,
                PageLink.source_page_id,
                PageLink.source_version_id,
                PageLink.link_syntax,
                PageLink.target_slug,
                PageLink.target_page_id,
                PageLink.occurrence_count,
                PageLink.first_snippet,
                PageLink.created_at,
            )
            .where(PageLink.workspace_id == workspace_id)
            .order_by(PageLink.id)
        ).all()
    return list(rows)


def _page_link_for_version(
    session_factory,
    source_page_id: UUID,
    source_version_id: UUID,
    *,
    target_slug: str,
) -> PageLink:
    with session_factory() as session:
        row = session.scalar(
            select(PageLink).where(
                PageLink.source_page_id == source_page_id,
                PageLink.source_version_id == source_version_id,
                PageLink.link_syntax == "wikilink",
                PageLink.target_slug == target_slug,
            )
        )
        assert row is not None
        return row


def _wikilink_edge_snapshot(session_factory, workspace_id: UUID):
    with session_factory() as session:
        rows = session.execute(
            select(
                EdgeProjection.source_id,
                EdgeProjection.target_id,
                EdgeProjection.weight,
                EdgeProjection.id,
                EdgeProjection.created_at,
                EdgeProjection.last_seen_at,
            )
            .where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "wikilink",
            )
            .order_by(EdgeProjection.source_id, EdgeProjection.target_id)
        ).all()
    return list(rows)


def _non_wikilink_edge_snapshot(session_factory, workspace_id: UUID):
    with session_factory() as session:
        rows = session.execute(
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
                EdgeProjection.created_at,
                EdgeProjection.last_seen_at,
            )
            .where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind != "wikilink",
            )
            .order_by(EdgeProjection.id)
        ).all()
    return list(rows)


def _observation_ids(session_factory, workspace_id: UUID):
    with session_factory() as session:
        return list(
            session.scalars(
                select(GraphObservation.id)
                .where(GraphObservation.workspace_id == workspace_id)
                .order_by(GraphObservation.id)
            )
        )


def _coverage_snapshot(
    session_factory,
    workspace_id: UUID,
) -> dict[UUID, _CoverageState]:
    with session_factory() as session:
        rows = session.scalars(
            select(GraphHistoryCoverage)
            .where(
                GraphHistoryCoverage.workspace_id == workspace_id,
                GraphHistoryCoverage.stream_kind == "context_page",
            )
            .order_by(GraphHistoryCoverage.stream_id)
        ).all()
        return {
            row.stream_id: _CoverageState(
                id=row.id,
                basis=row.basis,
                status=row.status,
                coverage_from=row.coverage_from,
                watermark=row.watermark,
                extractor_version=row.extractor_version,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        }


def _edge_projection_version(session_factory, workspace_id: UUID) -> int:
    with session_factory() as session:
        version = session.scalar(
            select(Workspace.edge_projection_version).where(
                Workspace.id == workspace_id
            )
        )
    assert version is not None
    return version


def _workspace_projection_snapshot(session_factory, workspace_id: UUID):
    return (
        _page_link_identity_snapshot(session_factory, workspace_id),
        _wikilink_edge_snapshot(session_factory, workspace_id),
        _non_wikilink_edge_snapshot(session_factory, workspace_id),
        _observation_ids(session_factory, workspace_id),
        _edge_projection_version(session_factory, workspace_id),
    )

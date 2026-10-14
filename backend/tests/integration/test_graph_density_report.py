from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event, select
from typer.testing import CliRunner

from backend.app.db import make_engine, make_session_factory
from backend.cli.main import app
from backend.models.base import Base
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.workspace import Workspace
from backend.services.graph_density import (
    RELATION_SUPPLY_MEDIAN_DEGREE_GATE,
    RelationDensityReport,
    relation_density_report,
)
from backend.services.graph_reprojection import reproject_workspace_graph
from backend.tests.utils.workspace_graph_corpora import (
    WorkspaceGraphCorpus,
    WorkspaceGraphCorpusBuilder,
    _add_library_artifact,
    build_synthetic_corpus,
)
from backend.tests.utils.workspace_graph_fixture_identity import fixture_time

_EXPECTED_REPORT_KEYS = {
    "report_version",
    "workspace_id",
    "workspace_slug",
    "visibility_scope",
    "percentile_method",
    "context_pages",
    "degree_0_pages",
    "degree_1_2_pages",
    "degree_3_plus_pages",
    "pages_with_links",
    "orphan_ratio",
    "p25_degree",
    "median_degree",
    "p75_degree",
    "wikilink_edges",
    "citation_page_edges",
    "library_items",
    "library_items_cited",
    "library_items_cited_ratio",
    "citation_source_edges",
    "citation_source_records",
    "restricted_library_items",
    "restricted_library_items_cited",
    "restricted_citation_source_edges",
    "restricted_citation_source_records",
    "gate_threshold",
    "gate_passed",
    "guidance_code",
    "measured_at",
    "edge_projection_version_before",
    "edge_projection_version_after",
    "edge_projection_version_stable",
}


def test_relation_density_report_uses_undirected_simple_degree_and_directed_counts(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        corpus = _context_corpus(session, "density-simple", context_count=8)
        a, b, c, d, e, f, g, h = corpus.context_ids
        av, bv, cv, dv, _ev, fv, gv, _hv = corpus.context_version_ids

        # A-B appears twice with different source syntax and once in reverse.
        # Syntax duplicates collapse to two directed pairs; degree sees one pair.
        _add_page_link(session, corpus, a, av, b, syntax="wikilink", occurrences=3)
        _add_page_link(session, corpus, a, av, b, syntax="context_url")
        _add_page_link(session, corpus, b, bv, a)
        _add_page_link(session, corpus, a, av, c)
        _add_page_link(session, corpus, b, bv, d)

        # Self, unresolved, and non-head links affect neither degree nor counts.
        _add_page_link(session, corpus, f, fv, f)
        _add_page_link(session, corpus, g, gv, None, target_slug="missing")
        stale_version = _add_non_head_context_version(session, corpus, h)
        _add_page_link(session, corpus, h, stale_version.id, a)

        _add_context_page_citation(session, av, dv)
        _add_context_page_citation(session, av, corpus.context_version_ids[4])
        _add_context_page_citation(session, bv, cv)
        _add_context_page_citation(session, cv, dv)
        _add_context_page_citation(session, cv, dv)  # duplicate raw record
        _add_context_page_citation(session, dv, cv)  # reverse direction
        _add_context_page_citation(session, fv, fv)  # self

        # EdgeProjection is explicitly not the report source of truth.
        session.add(
            EdgeProjection(
                workspace_id=corpus.workspace_id,
                source_kind="context_page",
                source_id=g,
                target_kind="context_page",
                target_id=h,
                edge_kind="wikilink",
                provenance="curated",
                weight=99.0,
                source_ref={"stale": True},
            )
        )

    report = _report(session_factory, corpus.workspace_id)

    assert RELATION_SUPPLY_MEDIAN_DEGREE_GATE == 2
    assert report.report_version == "graph-degree-report.v1"
    assert report.workspace_id == corpus.workspace_id
    assert report.workspace_slug == "workspace-graph-density-simple"
    assert report.visibility_scope == "operator_full"
    assert report.percentile_method == "nearest_rank"
    assert report.context_pages == 8
    assert report.degree_0_pages == 3
    assert report.degree_1_2_pages == 1
    assert report.degree_3_plus_pages == 4
    assert report.pages_with_links == 5
    assert report.pages_with_links + report.degree_0_pages == report.context_pages
    assert report.orphan_ratio == pytest.approx(3 / 8)
    assert report.p25_degree == 0
    # Median is the conventional midpoint average; p25/p75 use nearest rank.
    assert report.median_degree == 2
    assert report.p75_degree == 3
    assert report.wikilink_edges == 4
    assert report.citation_page_edges == 5
    assert report.gate_threshold == 2
    assert report.gate_passed is True
    assert report.guidance_code == "degree_1_2"
    assert report.measured_at.tzinfo is not None
    assert report.measured_at.utcoffset() == UTC.utcoffset(report.measured_at)
    assert report.edge_projection_version_before == 0
    assert report.edge_projection_version_after == 0
    assert report.edge_projection_version_stable is True


def test_relation_density_report_excludes_nonvisible_nonhead_and_other_workspace_rows(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        corpus = _context_corpus(
            session,
            "density-visibility",
            context_count=2,
        )
        builder = WorkspaceGraphCorpusBuilder(
            session,
            corpus_key="density-visibility-extra",
            title="Visibility extras",
        )
        archived = builder.add_context(0, slug="archived", title="Archived")
        draft = builder.add_context(
            1,
            slug="draft",
            title="Draft",
            published=False,
        )
        extra = builder.finish("density-visibility-extra")
        session.get(ContextPage, archived).lifecycle_status = "archived"

        other = _context_corpus(session, "density-other-tenant", context_count=2)
        a, b = corpus.context_ids
        av, bv = corpus.context_version_ids
        archived_v, draft_v = extra.context_version_ids
        _add_page_link(session, corpus, a, av, b)
        _add_page_link(session, corpus, a, av, a)
        _add_legacy_context_page_citation(session, a, bv)
        _add_page_link(session, extra, archived, archived_v, draft)
        _add_page_link(session, extra, draft, draft_v, archived)
        stale_b = _add_non_head_context_version(session, corpus, b)
        _add_page_link(session, corpus, b, stale_b.id, a)
        _add_context_page_citation(session, archived_v, bv)
        _add_context_page_citation(session, draft_v, av)
        _add_page_link(
            session,
            other,
            other.context_ids[0],
            other.context_version_ids[0],
            other.context_ids[1],
        )
        session.add(
            EdgeProjection(
                workspace_id=corpus.workspace_id,
                source_kind="context_page",
                source_id=b,
                target_kind="context_page",
                target_id=a,
                edge_kind="citation_page",
                provenance="curated",
                weight=1.0,
                source_ref={"stale": True},
            )
        )

    report = _report(session_factory, corpus.workspace_id)

    assert report.context_pages == 2
    assert report.wikilink_edges == 1
    assert report.citation_page_edges == 1
    assert report.pages_with_links == 2
    assert report.pages_with_links + report.degree_0_pages == report.context_pages
    assert report.median_degree == 1


def test_relation_density_report_accepts_only_current_head_claim_citations(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        corpus = _context_corpus(session, "density-claim-source", context_count=3)
        a, b, _c = corpus.context_ids
        av, bv, cv = corpus.context_version_ids
        current_claim = _add_claim(session, corpus, av, "Current claim")
        stale_b = _add_non_head_context_version(session, corpus, b)
        stale_claim = _add_claim(session, corpus, stale_b.id, "Stale claim")
        _add_claim_context_page_citation(session, current_claim.id, bv)
        _add_claim_context_page_citation(session, stale_claim.id, cv)

    report = _report(session_factory, corpus.workspace_id)

    assert report.context_pages == 3
    assert report.citation_page_edges == 1
    assert report.pages_with_links == 2
    assert report.pages_with_links + report.degree_0_pages == report.context_pages
    assert report.median_degree == 1


def test_relation_density_report_folds_historical_library_sources_to_item_identity(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        builder = WorkspaceGraphCorpusBuilder(
            session,
            corpus_key="density-library",
            title="Library density",
        )
        for index in range(5):
            builder.add_context(index, slug=f"page-{index}", title=f"Page {index}")
        trusted_id, trusted_v1 = builder.add_library(
            0,
            title="Trusted cited",
            uri="https://example.test/trusted",
        )
        restricted_id, restricted_version = builder.add_library(
            1,
            title="Restricted cited",
            uri="https://example.test/restricted",
            restricted=True,
        )
        builder.add_library(
            2,
            title="Trusted uncited",
            uri="https://example.test/uncited",
        )
        hidden_id, hidden_version = builder.add_library(
            3,
            title="Hidden",
            uri="https://example.test/hidden",
        )
        no_canonical_id, no_canonical_version = builder.add_library(
            4,
            title="No canonical head",
            uri="https://example.test/no-canonical",
        )
        corpus = builder.finish("density-library")

        trusted = session.get(LibraryItem, trusted_id)
        assert trusted is not None
        trusted_v2 = LibraryItemVersion(
            workspace_id=corpus.workspace_id,
            library_item_id=trusted_id,
            version_no=2,
            content_hash="trusted-v2",
            fetched_at=fixture_time(9_000),
            raw_storage_ref="fixture://density-library/trusted-v2",
            processing_status="ready",
            previous_version_id=trusted_v1,
            created_at=fixture_time(9_000),
            updated_at=fixture_time(9_000),
        )
        session.add(trusted_v2)
        session.flush()
        trusted.current_version_id = trusted_v2.id
        trusted.current_ready_version_id = trusted_v2.id

        hidden = session.get(LibraryItem, hidden_id)
        no_canonical = session.get(LibraryItem, no_canonical_id)
        assert hidden is not None and no_canonical is not None
        hidden.lifecycle_status = "hidden"
        no_canonical.current_version_id = None
        no_canonical.current_ready_version_id = None

        historical_artifact = _add_library_artifact(
            session,
            workspace_id=corpus.workspace_id,
            library_item_version_id=trusted_v1,
            fixture_key="density-library-historical",
        )
        p0, p1, p2, p3, p4 = corpus.context_ids
        p0v, p1v, p2v, p3v, p4v = corpus.context_version_ids
        session.get(ContextPage, p4).lifecycle_status = "archived"

        # Six visible raw records become three distinct Context-item edges.
        _add_library_citation(session, p0v, library_version_id=trusted_v1)
        _add_library_citation(session, p0v, artifact_id=historical_artifact.id)
        # Legacy rows may carry both references. Derived-artifact identity wins.
        _add_library_citation(
            session,
            p0v,
            library_version_id=restricted_version,
            artifact_id=historical_artifact.id,
        )
        _add_library_citation(session, p1v, library_version_id=trusted_v1)
        _add_library_citation(session, p2v, library_version_id=restricted_version)
        _add_library_citation(session, p2v, library_version_id=restricted_version)

        # All four records below are excluded by source/head/item visibility.
        _add_library_citation(session, p3v, library_version_id=hidden_version)
        _add_library_citation(session, p3v, library_version_id=no_canonical_version)
        stale_p0 = _add_non_head_context_version(session, corpus, p0)
        _add_library_citation(
            session,
            stale_p0.id,
            library_version_id=restricted_version,
        )
        _add_library_citation(session, p4v, library_version_id=trusted_v1)
        session.add(
            EdgeProjection(
                workspace_id=corpus.workspace_id,
                source_kind="context_page",
                source_id=p3,
                target_kind="library_item_version",
                target_id=trusted_v1,
                edge_kind="citation_source",
                provenance="curated",
                weight=1.0,
                source_ref={"stale": True},
            )
        )

    report = _report(session_factory, corpus.workspace_id)

    assert report.library_items == 3
    assert report.library_items_cited == 2
    assert report.library_items_cited_ratio == pytest.approx(2 / 3)
    assert report.citation_source_edges == 3
    assert report.citation_source_records == 6
    assert report.restricted_library_items == 1
    assert report.restricted_library_items_cited == 1
    assert report.restricted_citation_source_edges == 1
    assert report.restricted_citation_source_records == 2


def test_relation_density_report_changes_only_after_url_links_are_reprojected(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        builder = WorkspaceGraphCorpusBuilder(
            session,
            corpus_key="density-url-reproject",
            title="URL reproject density",
        )
        beta = builder.add_context(1, slug="beta", title="Beta")
        gamma = builder.add_context(2, slug="gamma", title="Gamma")
        builder.add_context(
            0,
            slug="alpha",
            title="Alpha",
            body_md=(
                "# Alpha\n\n"
                f"[Beta](/context?target={beta})\n\n"
                f"[Gamma](/context?target={gamma})"
            ),
        )
        corpus = builder.finish("density-url-reproject")

    before = _report(session_factory, corpus.workspace_id)
    with session_factory.begin() as session:
        reprojection = reproject_workspace_graph(session, corpus.workspace_id)
    after = _report(session_factory, corpus.workspace_id)

    assert before.wikilink_edges == 0
    assert before.pages_with_links == 0
    assert before.median_degree == 0
    assert reprojection.page_links == 2
    assert reprojection.wikilink_edges == 2
    assert after.wikilink_edges == 2
    assert after.pages_with_links == 3
    assert after.pages_with_links + after.degree_0_pages == after.context_pages
    assert after.median_degree == 1
    assert after.edge_projection_version_before == (
        before.edge_projection_version_after + 1
    )
    assert after.edge_projection_version_stable is True


def test_relation_density_report_classifies_synthetic_and_sparse_corpora(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        synthetic = build_synthetic_corpus(
            session,
            documents=1_000,
            profile="synthetic",
            seed=42,
        )[0]
        sparse = build_synthetic_corpus(
            session,
            documents=1_000,
            profile="sparse",
            seed=41,
        )[0]

    synthetic_report = _report(session_factory, synthetic.workspace_id)
    sparse_report = _report(session_factory, sparse.workspace_id)

    assert synthetic_report.median_degree == 5
    assert synthetic_report.gate_passed is True
    assert synthetic_report.guidance_code == "degree_3_plus"
    assert sparse_report.median_degree == 0
    assert sparse_report.gate_passed is False
    assert sparse_report.guidance_code == "degree_0"
    assert sparse_report.degree_0_pages > 0
    assert sparse_report.degree_1_2_pages > 0
    assert sparse_report.orphan_ratio is not None
    assert sparse_report.orphan_ratio > 0.5


def test_relation_density_report_uses_a_fixed_six_statement_read(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        corpus = _context_corpus(session, "density-query-count", context_count=20)

    statements: list[str] = []
    engine = session_factory.kw["bind"]

    def _record_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record_statement)
    try:
        report = _report(session_factory, corpus.workspace_id)
    finally:
        event.remove(engine, "before_cursor_execute", _record_statement)

    assert report.context_pages == 20
    assert len(statements) == 6
    assert all(
        statement.lstrip().upper().startswith("SELECT") for statement in statements
    )


def test_relation_density_report_marks_revision_change_or_disappearance_unstable(
    session_factory,
    monkeypatch,
) -> None:
    from backend.services import graph_density

    with session_factory.begin() as session:
        corpus = _context_corpus(session, "density-revision-marker", context_count=2)

    original = graph_density.load_graph_density_workspace_row

    def _changed_revision(session, workspace_id):
        row = original(session, workspace_id)
        assert row is not None
        return replace(row, edge_projection_version=row.edge_projection_version + 1)

    monkeypatch.setattr(
        graph_density,
        "load_graph_density_workspace_row",
        _changed_revision,
    )
    changed = _report(session_factory, corpus.workspace_id)
    assert changed.edge_projection_version_after == 1
    assert changed.edge_projection_version_stable is False

    monkeypatch.setattr(
        graph_density,
        "load_graph_density_workspace_row",
        lambda _session, _workspace_id: None,
    )
    disappeared = _report(session_factory, corpus.workspace_id)
    assert disappeared.edge_projection_version_after is None
    assert disappeared.edge_projection_version_stable is False


def test_graph_density_library_visibility_matches_read_contract(
    session_factory,
) -> None:
    from backend.repositories import graph_density_repo
    from backend.services.actor_visible_read import library_item_visible
    from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES

    assert set(graph_density_repo._EXCLUDED_LIBRARY_LIFECYCLE_STATUSES) == set(
        EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
    )

    statuses = ("pending", "completed", "ready", "ready_partial", "failed")
    with session_factory.begin() as session:
        builder = WorkspaceGraphCorpusBuilder(
            session,
            corpus_key="density-library-visibility-contract",
            title="Library visibility contract",
        )
        rows: list[tuple[LibraryItem, LibraryItemVersion]] = []
        for index, status in enumerate(statuses):
            item_id, version_id = builder.add_library(
                index,
                title=f"Status {status}",
                uri=f"https://example.test/status/{status}",
            )
            item = session.get(LibraryItem, item_id)
            version = session.get(LibraryItemVersion, version_id)
            assert item is not None and version is not None
            item.current_ready_version_id = None
            version.processing_status = status
            rows.append((item, version))
        corpus = builder.finish("density-library-visibility-contract")
        session.flush()

        expected = {
            item.id
            for item, version in rows
            if library_item_visible(
                item,
                None,
                workspace_id=corpus.workspace_id,
                version=version,
            )
        }
        actual = {
            row.item_id
            for row in graph_density_repo._load_library_heads(
                session,
                corpus.workspace_id,
            )
        }

    assert actual == expected


def test_empty_singleton_and_gate_boundary_have_explicit_semantics(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        empty = WorkspaceGraphCorpusBuilder(
            session,
            corpus_key="density-empty",
            title="Empty density",
        ).finish("density-empty")
        singleton = _context_corpus(session, "density-singleton", context_count=1)
        cycle = _context_corpus(session, "density-gate-boundary", context_count=4)
        for index, source in enumerate(cycle.context_ids):
            target = cycle.context_ids[(index + 1) % len(cycle.context_ids)]
            _add_page_link(
                session,
                cycle,
                source,
                cycle.context_version_ids[index],
                target,
            )

    empty_report = _report(session_factory, empty.workspace_id)
    singleton_report = _report(session_factory, singleton.workspace_id)
    boundary_report = _report(session_factory, cycle.workspace_id)

    assert empty_report.context_pages == 0
    assert empty_report.p25_degree is None
    assert empty_report.median_degree is None
    assert empty_report.p75_degree is None
    assert empty_report.orphan_ratio is None
    assert empty_report.library_items_cited_ratio is None
    assert empty_report.guidance_code == "no_context_pages"
    assert empty_report.gate_passed is False

    assert singleton_report.context_pages == 1
    assert singleton_report.degree_0_pages == 1
    assert singleton_report.pages_with_links == 0
    assert singleton_report.orphan_ratio == 1
    assert singleton_report.p25_degree == 0
    assert singleton_report.median_degree == 0
    assert singleton_report.p75_degree == 0
    assert singleton_report.guidance_code == "degree_0"

    assert boundary_report.median_degree == 2
    assert boundary_report.guidance_code == "degree_1_2"
    assert boundary_report.gate_passed is True


def test_graph_degree_report_cli_emits_versioned_json_and_reports_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url, workspace_id, workspace_slug = _cli_database(
        tmp_path / "degree-report.sqlite"
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "graph",
            "degree-report",
            "--workspace",
            workspace_slug,
            "--db",
            database_url,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == _EXPECTED_REPORT_KEYS
    assert {field.name for field in fields(RelationDensityReport)} == (
        _EXPECTED_REPORT_KEYS
    )
    assert payload["report_version"] == "graph-degree-report.v1"
    assert payload["workspace_id"] == str(workspace_id)
    assert payload["workspace_slug"] == workspace_slug
    assert payload["visibility_scope"] == "operator_full"
    assert payload["percentile_method"] == "nearest_rank"
    assert payload["median_degree"] == 1
    assert payload["gate_threshold"] == 2
    assert payload["gate_passed"] is False
    assert payload["guidance_code"] == "degree_1_2"
    assert payload["edge_projection_version_stable"] is True
    assert datetime.fromisoformat(payload["measured_at"]).tzinfo is not None

    missing = runner.invoke(
        app,
        [
            "graph",
            "degree-report",
            "--workspace",
            "missing-workspace",
            "--db",
            database_url,
        ],
    )
    assert missing.exit_code == 1
    assert "workspace not found: missing-workspace" in missing.output

    from backend.cli.commands import graph as graph_commands

    original_select_targets = graph_commands._select_targets

    def _fail_workspace_lookup(*_args, **_kwargs):
        raise RuntimeError("workspace lookup detail")

    monkeypatch.setattr(
        graph_commands,
        "_select_targets",
        _fail_workspace_lookup,
    )
    default_lookup_failure = runner.invoke(
        app,
        [
            "graph",
            "degree-report",
            "--workspace",
            workspace_slug,
            "--db",
            database_url,
        ],
    )
    assert default_lookup_failure.exit_code == 1
    assert "stage=workspace_lookup" in default_lookup_failure.output
    assert "error_type=RuntimeError" in default_lookup_failure.output
    assert "workspace lookup detail" not in default_lookup_failure.output

    verbose_lookup_failure = runner.invoke(
        app,
        [
            "graph",
            "degree-report",
            "--workspace",
            workspace_slug,
            "--db",
            database_url,
            "--verbose-errors",
        ],
    )
    assert verbose_lookup_failure.exit_code == 1
    assert "workspace lookup detail" in verbose_lookup_failure.output
    monkeypatch.setattr(
        graph_commands,
        "_select_targets",
        original_select_targets,
    )

    def _fail_report(_session, _workspace_id):
        raise RuntimeError("density diagnostic detail")

    monkeypatch.setattr(graph_commands, "relation_density_report", _fail_report)
    default_failure = runner.invoke(
        app,
        [
            "graph",
            "degree-report",
            "--workspace",
            workspace_slug,
            "--db",
            database_url,
        ],
    )
    assert default_failure.exit_code == 1
    assert "error_type=RuntimeError" in default_failure.output
    assert "density diagnostic detail" not in default_failure.output

    verbose_failure = runner.invoke(
        app,
        [
            "graph",
            "degree-report",
            "--workspace",
            workspace_slug,
            "--db",
            database_url,
            "--verbose-errors",
        ],
    )
    assert verbose_failure.exit_code == 1
    assert "density diagnostic detail" in verbose_failure.output


def test_graph_degree_report_cli_warns_but_succeeds_for_unstable_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from backend.cli.commands import graph as graph_commands

    database_url, workspace_id, workspace_slug = _cli_database(
        tmp_path / "degree-report-unstable.sqlite"
    )
    session_factory = make_session_factory(make_engine(database_url))
    stable = _report(session_factory, workspace_id)
    unstable = replace(
        stable,
        edge_projection_version_after=stable.edge_projection_version_before + 1,
        edge_projection_version_stable=False,
    )
    monkeypatch.setattr(
        graph_commands,
        "relation_density_report",
        lambda _session, _workspace_id: unstable,
    )

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "degree-report",
            "--workspace",
            workspace_slug,
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "edge_projection_version_stable=false" in result.output
    assert "warning" in result.output.lower()


def _context_corpus(
    session,
    corpus_key: str,
    *,
    context_count: int,
) -> WorkspaceGraphCorpus:
    builder = WorkspaceGraphCorpusBuilder(
        session,
        corpus_key=corpus_key,
        title=corpus_key.replace("-", " ").title(),
    )
    for index in range(context_count):
        builder.add_context(
            index,
            slug=f"page-{index}",
            title=f"Page {index}",
        )
    return builder.finish(corpus_key)


def _add_page_link(
    session,
    corpus: WorkspaceGraphCorpus,
    source_page_id: UUID,
    source_version_id: UUID,
    target_page_id: UUID | None,
    *,
    syntax: str = "wikilink",
    occurrences: int = 1,
    target_slug: str | None = None,
) -> None:
    if target_page_id is not None:
        target_slug = corpus.context_slugs[target_page_id]
    assert target_slug is not None
    session.add(
        PageLink(
            workspace_id=corpus.workspace_id,
            source_page_id=source_page_id,
            source_version_id=source_version_id,
            link_syntax=syntax,
            target_slug=target_slug,
            target_page_id=target_page_id,
            occurrence_count=occurrences,
            first_snippet="Density report fixture",
        )
    )


def _add_context_page_citation(
    session,
    source_version_id: UUID,
    cited_version_id: UUID,
) -> None:
    source = session.get(ContextPageVersion, source_version_id)
    assert source is not None
    session.add(
        ContextCitation(
            workspace_id=source.workspace_id,
            target_kind="context_page_version",
            target_id=source_version_id,
            target_location={},
            cited_context_page_version_id=cited_version_id,
            location={},
            citation_type="context_page",
            verification_status="verified",
            created_via="manual_input",
            created_by_actor_type="human",
        )
    )


def _add_legacy_context_page_citation(
    session,
    source_page_id: UUID,
    cited_version_id: UUID,
) -> None:
    source = session.get(ContextPage, source_page_id)
    assert source is not None
    session.add(
        ContextCitation(
            workspace_id=source.workspace_id,
            target_kind="context_page",
            target_id=source_page_id,
            target_location={},
            cited_context_page_version_id=cited_version_id,
            location={},
            citation_type="context_page",
            verification_status="verified",
            created_via="manual_input",
            created_by_actor_type="human",
        )
    )


def _add_claim(
    session,
    corpus: WorkspaceGraphCorpus,
    source_version_id: UUID,
    text: str,
) -> ContextClaim:
    claim = ContextClaim(
        workspace_id=corpus.workspace_id,
        page_version_id=source_version_id,
        claim_text=text,
        location_in_page={"kind": "whole_page"},
        claim_type="summary",
        confidence="unknown",
        status="active",
        is_core=False,
        created_by_actor_type="human",
    )
    session.add(claim)
    session.flush()
    return claim


def _add_claim_context_page_citation(
    session,
    claim_id: UUID,
    cited_version_id: UUID,
) -> None:
    claim = session.get(ContextClaim, claim_id)
    assert claim is not None
    session.add(
        ContextCitation(
            workspace_id=claim.workspace_id,
            target_kind="context_claim",
            target_id=claim_id,
            target_location={},
            cited_context_page_version_id=cited_version_id,
            location={},
            citation_type="context_page",
            verification_status="verified",
            created_via="manual_input",
            created_by_actor_type="human",
        )
    )


def _add_library_citation(
    session,
    source_version_id: UUID,
    *,
    library_version_id: UUID | None = None,
    artifact_id: UUID | None = None,
) -> None:
    source = session.get(ContextPageVersion, source_version_id)
    assert source is not None
    session.add(
        ContextCitation(
            workspace_id=source.workspace_id,
            target_kind="context_page_version",
            target_id=source_version_id,
            target_location={},
            library_item_version_id=library_version_id,
            library_artifact_id=artifact_id,
            location={},
            citation_type=(
                "derived_artifact" if artifact_id is not None else "raw_source"
            ),
            verification_status="verified",
            created_via="manual_input",
            created_by_actor_type="human",
        )
    )


def _add_non_head_context_version(
    session,
    corpus: WorkspaceGraphCorpus,
    page_id: UUID,
) -> ContextPageVersion:
    version = ContextPageVersion(
        workspace_id=corpus.workspace_id,
        page_id=page_id,
        version_no=2,
        body_md="# Stale",
        content_hash=f"stale-{page_id}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="density-test",
        version_status="superseded_snapshot",
        last_saved_at=fixture_time(9_500),
        created_at=fixture_time(9_500),
    )
    session.add(version)
    session.flush()
    return version


def _report(session_factory, workspace_id: UUID):
    with session_factory() as session:
        return relation_density_report(session, workspace_id)


def _cli_database(path: Path) -> tuple[str, UUID, str]:
    database_url = f"sqlite+pysqlite:///{path}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        corpus = _context_corpus(session, "density-cli", context_count=2)
        _add_page_link(
            session,
            corpus,
            corpus.context_ids[0],
            corpus.context_version_ids[0],
            corpus.context_ids[1],
        )
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == corpus.workspace_id)
        )
        assert workspace is not None
        workspace_id = workspace.id
        workspace_slug = workspace.slug
    engine.dispose()
    return database_url, workspace_id, workspace_slug

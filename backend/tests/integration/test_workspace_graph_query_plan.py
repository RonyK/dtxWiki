from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect

from backend.app.db import make_engine, make_session_factory
from backend.models import Base
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.utils.workspace_graph_corpora import (
    add_knowledge_40_relation_branch_fixtures,
    build_knowledge_40,
    corpus_actor,
)
from scripts.bench.graph.bench_relation_sql import _page_link_sql, _row_sha256

pytestmark = pytest.mark.integration


def _normalized(statement: str) -> str:
    return " ".join(statement.casefold().split())


def _assert_indexed_plan(
    plan: list[str],
    *,
    searches: dict[str, tuple[str, ...]],
    left_joins: frozenset[str] = frozenset(),
) -> None:
    assert not any(detail.startswith("SCAN ") for detail in plan), plan
    assert not any("AUTOMATIC" in detail for detail in plan), plan
    assert not any("TEMP B-TREE" in detail for detail in plan), plan

    for alias, required in searches.items():
        # The trailing space makes aliases exact: ``page`` cannot match
        # ``page_version`` and a missing alias cannot pass via another SEARCH.
        matches = [detail for detail in plan if detail.startswith(f"SEARCH {alias} ")]
        assert len(matches) == 1, plan
        detail = matches[0]
        assert all(fragment in detail for fragment in required), detail
        if alias in left_joins:
            assert detail.endswith(" LEFT-JOIN"), detail
        else:
            assert not detail.endswith(" LEFT-JOIN"), detail


def test_relation_reads_use_covering_sqlite_indexes(tmp_path) -> None:
    engine = make_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'graph-query-plan.db').as_posix()}"
    )
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    captured: list[tuple[str, Any]] = []

    try:
        inspector = inspect(engine)
        page_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("page_links")
        }
        citation_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("context_citation")
        }
        assert page_indexes["idx_page_links_graph_head"] == [
            "workspace_id",
            "source_page_id",
            "source_version_id",
            "target_page_id",
            "occurrence_count",
            "created_at",
        ]
        assert citation_indexes["idx_context_citation_graph_head"] == [
            "workspace_id",
            "target_kind",
            "target_id",
            "cited_context_page_version_id",
            "library_item_version_id",
            "library_artifact_id",
            "created_at",
        ]

        with session_factory.begin() as session:
            corpus = build_knowledge_40(session, corpus_key="query-plan")
            relation_branches = add_knowledge_40_relation_branch_fixtures(
                session,
                corpus,
                fixture_key="graph-query-plan",
            )
            # The current-head FK is workspace-scoped but does not prove that
            # the referenced version belongs to this page. Keep a malformed
            # cross-page pointer in the fixture so the claim source query must
            # resolve ownership through ContextPageVersion instead of reverse
            # matching every page that happens to point at the same version.
            cross_page_head = session.get(ContextPage, corpus.context_ids[30])
            assert cross_page_head is not None
            cross_page_head.current_published_version_id = corpus.context_version_ids[
                20
            ]

            # Exercise both claim-source visibility predicates in the captured
            # SQL itself. The stale published snapshot is not the page head,
            # while the draft is deliberately installed as a schema-valid but
            # non-published head. Neither row may reach Python-side document
            # filtering below.
            stale_source_version = ContextPageVersion(
                id=uuid4(),
                workspace_id=corpus.workspace_id,
                page_id=relation_branches.claim_source_page_id,
                version_no=2,
                body_md="# Stale claim source",
                content_hash="query-plan-stale-claim-source",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id=str(uuid4()),
                version_status="superseded_snapshot",
            )
            draft_source_page = session.get(ContextPage, corpus.context_ids[23])
            assert draft_source_page is not None
            draft_source_version = ContextPageVersion(
                id=uuid4(),
                workspace_id=corpus.workspace_id,
                page_id=draft_source_page.id,
                version_no=2,
                body_md="# Draft claim source",
                content_hash="query-plan-draft-claim-source",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id=str(uuid4()),
                version_status="draft",
            )
            session.add_all([stale_source_version, draft_source_version])
            session.flush()
            draft_source_page.current_published_version_id = draft_source_version.id

            stale_claim = ContextClaim(
                workspace_id=corpus.workspace_id,
                page_version_id=stale_source_version.id,
                claim_text="Claim from a stale published snapshot",
                location_in_page={},
            )
            draft_claim = ContextClaim(
                workspace_id=corpus.workspace_id,
                page_version_id=draft_source_version.id,
                claim_text="Claim from a non-published current head",
                location_in_page={},
            )
            session.add_all([stale_claim, draft_claim])
            session.flush()
            session.add_all(
                [
                    ContextCitation(
                        workspace_id=corpus.workspace_id,
                        target_kind="context_claim",
                        target_id=stale_claim.id,
                        target_location={},
                        library_item_version_id=corpus.library_version_ids[2],
                        location={},
                        citation_type="raw_source",
                        verification_status="verified",
                        created_via="manual_input",
                        created_by_actor_type="human",
                    ),
                    ContextCitation(
                        workspace_id=corpus.workspace_id,
                        target_kind="context_claim",
                        target_id=draft_claim.id,
                        target_location={},
                        library_item_version_id=corpus.library_version_ids[3],
                        location={},
                        citation_type="raw_source",
                        verification_status="verified",
                        created_via="manual_input",
                        created_by_actor_type="human",
                    ),
                ]
            )

        with session_factory() as session:
            documents = WorkspaceGraphService._load_documents(
                session,
                corpus.workspace_id,
                corpus_actor(corpus, role="owner"),
            )

            def capture_relation_selects(
                _connection,
                _cursor,
                statement,
                parameters,
                _context,
                executemany,
            ) -> None:
                normalized = _normalized(statement)
                if not executemany and (
                    " from page_links" in normalized
                    or " from context_citation" in normalized
                ):
                    captured.append((statement, parameters))

            event.listen(engine, "before_cursor_execute", capture_relation_selects)
            try:
                relations = WorkspaceGraphService._load_relation_universe(
                    session,
                    corpus.workspace_id,
                    documents,
                )
            finally:
                event.remove(
                    engine,
                    "before_cursor_execute",
                    capture_relation_selects,
                )

        relation_keys = {
            (relation.kind, relation.source, relation.target) for relation in relations
        }
        assert (
            "citation_source",
            ("context", corpus.context_ids[0]),
            ("library", corpus.library_ids[0]),
        ) in relation_keys
        assert relation_branches.expected_relation_keys <= relation_keys

        page_selects = [
            item for item in captured if " from page_links " in _normalized(item[0])
        ]
        citation_selects = [
            item
            for item in captured
            if " from context_citation " in _normalized(item[0])
        ]
        assert len(page_selects) == 1
        assert len(citation_selects) == 2
        claim_select = next(
            item
            for item in citation_selects
            if " join context_claim " in _normalized(item[0])
        )
        version_select = next(
            item
            for item in citation_selects
            if " join context_claim " not in _normalized(item[0])
        )

        with engine.connect() as connection:
            page_statement, page_parameters = page_selects[0]
            assert "page_links INDEXED BY idx_page_links_graph_head" in page_statement
            benchmark_statement = _page_link_sql("pinned")
            assert _normalized(benchmark_statement) == _normalized(page_statement)
            assert page_parameters == (
                corpus.workspace_id.hex,
                corpus.workspace_id.hex,
                "archived",
            )
            production_result = connection.exec_driver_sql(
                page_statement,
                page_parameters,
            )
            production_columns = tuple(production_result.keys())
            production_rows = [tuple(row) for row in production_result.all()]
            benchmark_result = connection.exec_driver_sql(
                benchmark_statement,
                page_parameters,
            )
            benchmark_columns = tuple(benchmark_result.keys())
            benchmark_rows = [tuple(row) for row in benchmark_result.all()]
            expected_columns = (
                "source_page_id",
                "source_version_id",
                "target_page_id",
                "occurrence_count",
                "created_at",
            )
            assert production_columns == benchmark_columns == expected_columns
            assert production_rows
            assert len(production_rows) == len(benchmark_rows)
            assert _row_sha256(production_rows) == _row_sha256(benchmark_rows)
            page_plan = [
                row[3]
                for row in connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {page_statement}",
                    page_parameters,
                ).all()
            ]
            _assert_indexed_plan(
                page_plan,
                searches={
                    "page_links": (
                        "USING COVERING INDEX idx_page_links_graph_head",
                        "(workspace_id=?)",
                    ),
                    "context_page": ("workspace_id=?", "id=?"),
                },
            )

            version_statement, version_parameters = version_select
            assert (
                "context_citation INDEXED BY idx_context_citation_graph_head"
                in version_statement
            )
            version_plan = [
                row[3]
                for row in connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {version_statement}",
                    version_parameters,
                ).all()
            ]
            _assert_indexed_plan(
                version_plan,
                searches={
                    "context_citation": (
                        "USING COVERING INDEX idx_context_citation_graph_head",
                        "(workspace_id=? AND target_kind=? AND target_id=?)",
                    ),
                    "citation_version_source_page": ("workspace_id=?",),
                    "cited_context_version": ("workspace_id=?", "id=?"),
                    "cited_context_page": ("workspace_id=?", "id=?"),
                    "cited_library_artifact": ("workspace_id=?", "id=?"),
                },
                left_joins=frozenset(
                    {
                        "cited_context_version",
                        "cited_context_page",
                        "cited_library_artifact",
                    }
                ),
            )

            claim_statement, claim_parameters = claim_select
            assert (
                "context_citation INDEXED BY idx_context_citation_graph_head"
                in claim_statement
            )
            claim_plan = [
                row[3]
                for row in connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {claim_statement}",
                    claim_parameters,
                ).all()
            ]
            _assert_indexed_plan(
                claim_plan,
                searches={
                    "context_citation": (
                        "USING COVERING INDEX idx_context_citation_graph_head",
                        "(workspace_id=? AND target_kind=?)",
                    ),
                    "context_claim": ("id=?",),
                    "citation_claim_source_version": ("workspace_id=?", "id=?"),
                    "citation_claim_source_page": ("workspace_id=?", "id=?"),
                    "cited_context_version": ("workspace_id=?", "id=?"),
                    "cited_context_page": ("workspace_id=?", "id=?"),
                    "cited_library_artifact": ("workspace_id=?", "id=?"),
                },
                left_joins=frozenset(
                    {
                        "cited_context_version",
                        "cited_context_page",
                        "cited_library_artifact",
                    }
                ),
            )
            claim_rows = list(
                connection.exec_driver_sql(
                    claim_statement,
                    claim_parameters,
                ).mappings()
            )
            assert len(claim_rows) == 1
            assert (
                claim_rows[0]["source_page_id"],
                claim_rows[0]["library_item_version_id"],
            ) == (
                relation_branches.claim_source_page_id.hex,
                corpus.library_version_ids[0].hex,
            )
    finally:
        engine.dispose()

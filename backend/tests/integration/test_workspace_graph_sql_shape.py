from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.utils.postgres import drop_reflected_postgres_metadata
from backend.tests.utils.workspace_graph_corpora import (
    add_knowledge_40_relation_branch_fixtures,
    build_knowledge_40,
    build_synthetic_workspace,
    corpus_actor,
)

pytestmark = pytest.mark.integration


def _normalized(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.casefold()).strip()


def _projected_columns(statement: str, *table_names: str) -> set[str]:
    projection = statement.split(" from ", 1)[0]
    table_pattern = "|".join(re.escape(table) for table in table_names)
    return set(re.findall(rf"\b(?:{table_pattern})\.[a-z0-9_]+\b", projection))


def _parameter_count(parameters: Any) -> int:
    if isinstance(parameters, dict):
        return len(parameters)
    if isinstance(parameters, (list, tuple)):
        return len(parameters)
    return 0


def _capture_selects(engine, operation: Callable[[], None]):
    captured: list[tuple[str, int]] = []

    def capture(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        executemany,
    ) -> None:
        if not executemany and statement.lstrip().casefold().startswith("select"):
            captured.append((_normalized(statement), _parameter_count(parameters)))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        operation()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    return captured


def _postgres_url() -> str | None:
    value = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


def _assert_query_parameter_count_is_constant_at_scale(session_factory) -> None:
    service = WorkspaceGraphService(cursor_secret="sql-shape-scale-secret")
    engine = session_factory.kw["bind"]
    maxima: list[int] = []
    statement_counts: list[int] = []

    for documents in (200, 2_000, 10_000):
        with session_factory.begin() as session:
            corpus = build_synthetic_workspace(
                session,
                documents=documents,
                profile="synthetic",
                seed=260917,
                workspace_index=0,
                restricted_library_pct=0,
                corpus_key=f"sql-shape-{documents}",
            )

        def query(corpus=corpus) -> None:
            with session_factory() as session:
                service.query(
                    session,
                    corpus.workspace_id,
                    request=WorkspaceGraphOverviewRequest(
                        budget={"documents": 10_000, "relations": 100_000}
                    ),
                    actor=corpus_actor(corpus, role="owner"),
                )

        captured = _capture_selects(engine, query)
        assert all(
            "context_page_version.body_md" not in statement
            for statement, _parameter_count in captured
        )
        maxima.append(max(count for _statement, count in captured))
        statement_counts.append(len(captured))

    assert len(set(maxima)) == 1
    assert max(maxima) <= 32
    assert len(set(statement_counts)) == 1


def test_workspace_graph_query_parameter_count_is_constant_at_scale(
    session_factory,
) -> None:
    _assert_query_parameter_count_is_constant_at_scale(session_factory)


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires a dedicated PostgreSQL migration test database",
)
def test_workspace_graph_query_parameter_count_is_constant_on_postgres() -> None:
    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    drop_reflected_postgres_metadata(engine)
    try:
        command.upgrade(config, "head")
        postgres_session_factory = sessionmaker(
            bind=engine,
            expire_on_commit=False,
        )
        _assert_query_parameter_count_is_constant_at_scale(postgres_session_factory)
    finally:
        drop_reflected_postgres_metadata(engine)
        engine.dispose()


def test_workspace_graph_document_queries_select_only_projection_columns(
    session_factory,
) -> None:
    service = WorkspaceGraphService(cursor_secret="sql-shape-columns-secret")
    engine = session_factory.kw["bind"]
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key="sql-shape-columns")
        item = session.get(LibraryItem, corpus.library_ids[0])
        assert item is not None
        item.title = None
        item.current_ready_version_id = corpus.library_version_ids[0]
        expected_library_label = item.uri

    result: dict[str, Any] = {}

    def load_documents() -> None:
        with session_factory() as session:
            result["documents"] = service._load_documents(
                session,
                corpus.workspace_id,
                corpus_actor(corpus, role="owner"),
            )
            service._attach_tree_placements(
                session, corpus.workspace_id, result["documents"]
            )

    loaded_entities: list[type] = []

    def record_load(target, _context) -> None:
        loaded_entities.append(type(target))

    models = (
        ContextPage,
        ContextPageVersion,
        LibraryItem,
        LibraryItemVersion,
        WorkspaceTreeNode,
    )
    for model in models:
        event.listen(model, "load", record_load)
    try:
        statements = [
            statement for statement, _count in _capture_selects(engine, load_documents)
        ]
    finally:
        for model in models:
            event.remove(model, "load", record_load)

    assert len(statements) == 3
    assert loaded_entities == []
    context_sql = next(
        statement
        for statement in statements
        if " from context_page join context_page_version " in statement
    )
    library_sql = next(
        statement
        for statement in statements
        if " from library_item join library_item_version " in statement
    )
    tree_sql = next(
        statement
        for statement in statements
        if " from workspace_tree_node " in statement
    )

    assert "context_page_version.body_md" not in context_sql
    assert _projected_columns(
        context_sql,
        "context_page",
        "context_page_version",
    ) == {
        "context_page.id",
        "context_page.title",
        "context_page_version.id",
        "context_page_version.created_at",
    }
    assert _projected_columns(
        library_sql,
        "library_item",
        "library_item_version",
    ) == {
        "library_item.id",
        "library_item.workspace_id",
        "library_item.lifecycle_status",
        "library_item.trust_level",
        "library_item.current_ready_version_id",
        "library_item.current_version_id",
        "library_item.title",
        "library_item.uri",
        "library_item_version.id",
        "library_item_version.processing_status",
        "library_item_version.updated_at",
        "library_item_version.selected_conversion_attempt_id",
    }
    assert _projected_columns(tree_sql, "workspace_tree_node", "library_artifact") == {
        "workspace_tree_node.id",
        "workspace_tree_node.parent_id",
        "workspace_tree_node.node_kind",
        "workspace_tree_node.title",
        "workspace_tree_node.target_kind",
        "workspace_tree_node.target_id",
        "workspace_tree_node.primary_placement",
        "library_artifact.library_item_version_id",
        "library_artifact.attempt_id",
    }
    assert result["documents"][("library", corpus.library_ids[0])].label == (
        expected_library_label
    )


def test_workspace_graph_uses_ready_library_head_while_current_is_converting(
    session_factory,
) -> None:
    service = WorkspaceGraphService(cursor_secret="sql-shape-canonical-head-secret")
    request = WorkspaceGraphOverviewRequest(lens="links")
    with session_factory.begin() as session:
        corpus = build_knowledge_40(
            session,
            corpus_key="sql-shape-canonical-library-head",
        )
        item_id = corpus.library_ids[0]
        ready_version_id = corpus.library_version_ids[0]
        item = session.get(LibraryItem, item_id)
        ready_version = session.get(LibraryItemVersion, ready_version_id)
        assert item is not None
        assert ready_version is not None
        item.current_ready_version_id = ready_version_id
        item.current_version_id = ready_version_id
        ready_updated_at = ready_version.updated_at

    actor = corpus_actor(corpus, role="owner")
    with session_factory() as session:
        baseline = service.query(
            session,
            corpus.workspace_id,
            request=request,
            actor=actor,
        )

    baseline_node = next(
        node
        for node in baseline.nodes
        if node.grain == "document"
        and node.entity_kind == "library"
        and node.entity_id == item_id
    )
    converting_updated_at = baseline.generated_at + timedelta(days=1)
    converting_version_id = uuid4()
    with session_factory.begin() as session:
        item = session.get(LibraryItem, item_id)
        assert item is not None
        converting_version = LibraryItemVersion(
            id=converting_version_id,
            workspace_id=corpus.workspace_id,
            library_item_id=item_id,
            version_no=2,
            content_hash="canonical-library-converting-v2",
            fetched_at=converting_updated_at,
            raw_storage_ref=(
                f"fixture://sql-shape-canonical-library-head/{item_id}/v2"
            ),
            processing_status="converting",
            previous_version_id=ready_version_id,
            created_at=converting_updated_at,
            updated_at=converting_updated_at,
        )
        session.add(converting_version)
        session.flush()
        item.current_version_id = converting_version_id
        assert item.current_ready_version_id == ready_version_id

    with session_factory() as session:
        documents = service._load_documents(session, corpus.workspace_id, actor)
        actual = service.query(
            session,
            corpus.workspace_id,
            request=request,
            actor=actor,
        )

    library_document = documents[("library", item_id)]
    actual_node = next(
        node
        for node in actual.nodes
        if node.grain == "document"
        and node.entity_kind == "library"
        and node.entity_id == item_id
    )
    assert library_document.head_id == ready_version_id
    assert library_document.head_id != converting_version_id
    assert library_document.updated_at == ready_updated_at
    assert actual_node.model_dump() == baseline_node.model_dump()
    assert actual.graph_revision == baseline.graph_revision
    assert actual.generated_at == baseline.generated_at


def test_workspace_graph_relation_branches_use_bounded_head_joins(
    session_factory,
) -> None:
    service = WorkspaceGraphService(cursor_secret="sql-shape-branches-secret")
    engine = session_factory.kw["bind"]
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key="sql-shape-branches")
        artifact_tree_node_id = uuid4()
        relation_branches = add_knowledge_40_relation_branch_fixtures(
            session,
            corpus,
            fixture_key="graph-sql-shape",
        )
        stale_version_id = uuid4()
        stale_version = ContextPageVersion(
            id=stale_version_id,
            workspace_id=corpus.workspace_id,
            page_id=relation_branches.claim_source_page_id,
            version_no=2,
            body_md="# Stale claim source",
            content_hash="graph-sql-shape-stale-claim-source",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=str(uuid4()),
            version_status="superseded_snapshot",
        )
        session.add(stale_version)
        session.flush()
        stale_claim = ContextClaim(
            workspace_id=corpus.workspace_id,
            page_version_id=stale_version_id,
            claim_text="A claim from a non-current published snapshot",
            location_in_page={},
        )
        session.add(stale_claim)
        session.flush()
        session.add(
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
            )
        )
        session.add(
            WorkspaceTreeNode(
                id=artifact_tree_node_id,
                workspace_id=corpus.workspace_id,
                section="library",
                node_kind="library_artifact_ref",
                parent_id=None,
                sort_key="artifact",
                title="Artifact placement",
                slug="artifact-placement",
                target_kind="library_artifact",
                target_id=relation_branches.artifact_id,
                is_system_node=False,
                can_have_children=False,
                primary_placement=True,
                visibility_status="visible",
                created_by_actor_type="human",
            )
        )

    result: dict[str, Any] = {}

    def query() -> None:
        with session_factory() as session:
            documents = service._load_documents(
                session, corpus.workspace_id, corpus_actor(corpus, role="owner")
            )
            service._attach_tree_placements(session, corpus.workspace_id, documents)
            relations = service._load_relation_universe(
                session, corpus.workspace_id, documents
            )
            result["documents"] = documents
            result["relations"] = relations

    captured = _capture_selects(engine, query)
    statements = [statement for statement, _count in captured]
    page_link_sql = next(
        statement for statement in statements if " from page_links " in statement
    )
    citation_sql = [
        statement for statement in statements if " from context_citation " in statement
    ]
    tree_sql = next(
        statement
        for statement in statements
        if " from workspace_tree_node " in statement
    )

    assert " join context_page " in page_link_sql
    assert (
        "context_page.current_published_version_id = page_links.source_version_id"
        in page_link_sql
    )
    assert "context_page.workspace_id = page_links.workspace_id" in page_link_sql
    assert " order by " not in page_link_sql
    assert len(citation_sql) == 2
    claim_sql = next(
        statement for statement in citation_sql if " join context_claim " in statement
    )
    version_sql = next(
        statement
        for statement in citation_sql
        if " join context_claim " not in statement
    )
    assert " join context_page_version as citation_claim_source_version " in claim_sql
    assert (
        "citation_claim_source_version.id = context_claim.page_version_id" in claim_sql
    )
    assert (
        "citation_claim_source_version.workspace_id = context_claim.workspace_id"
        in claim_sql
    )
    assert (
        "citation_claim_source_page.id = citation_claim_source_version.page_id"
        in claim_sql
    )
    assert (
        "citation_claim_source_page.workspace_id "
        "= citation_claim_source_version.workspace_id"
    ) in claim_sql
    assert (
        "citation_claim_source_page.current_published_version_id "
        "= citation_claim_source_version.id"
    ) in claim_sql
    assert "citation_claim_source_version.version_status in" in claim_sql
    assert (
        "citation_version_source_page.current_published_version_id "
        "= context_citation.target_id"
    ) in version_sql
    assert all(" order by " not in statement for statement in citation_sql)
    assert all(
        " left outer join library_artifact " in statement for statement in citation_sql
    )
    assert " left outer join library_artifact " in tree_sql
    assert all(count <= 32 for _statement, count in captured)
    assert not any(
        "library_artifact.id in" in statement
        or "page_links.source_version_id in" in statement
        or "page_links.source_page_id in" in statement
        or "context_citation.target_id in" in statement
        or "context_claim.page_version_id in" in statement
        or "context_page_version.id in" in statement
        for statement in statements
    )

    page_link_projection = page_link_sql.split(" from page_links ", 1)[0]
    assert "page_links.id" not in page_link_projection
    assert "page_links.target_slug" not in page_link_projection
    assert "page_links.first_snippet" not in page_link_projection
    for statement in citation_sql:
        projection = statement.split(" from context_citation ", 1)[0]
        assert "context_citation.id" not in projection
        assert "context_citation.target_location" not in projection
        assert "context_citation.location" not in projection
        assert "context_citation.snippet" not in projection
        assert "context_citation.citation_type" not in projection
        assert "context_citation.verification_status" not in projection
        assert "context_citation.created_via" not in projection
        assert "context_citation.created_by_actor_type" not in projection

    relations = {
        (relation.kind, relation.source, relation.target)
        for relation in result["relations"]
    }
    assert relation_branches.expected_relation_keys <= relations
    assert (
        "citation_source",
        ("context", relation_branches.claim_source_page_id),
        ("library", corpus.library_ids[2]),
    ) not in relations

    artifact_document = result["documents"][
        ("library", relation_branches.artifact_library_item_id)
    ]
    assert artifact_tree_node_id in {
        placement.tree_node_id for placement in artifact_document.secondary_revision
    }

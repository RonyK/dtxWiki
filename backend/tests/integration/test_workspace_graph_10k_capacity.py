"""Real-database desktop capacity, mobile budgets, and actor-visible twins."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models.context import ContextCitation, ContextPage, PageLink
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.utils.postgres import drop_reflected_postgres_metadata
from backend.tests.utils.workspace_graph_capacity import build_capacity_workspace
from backend.tests.utils.workspace_graph_corpora import (
    WorkspaceGraphCorpusBuilder,
    add_hidden_draft_decoys,
    corpus_actor,
    fixture_time,
)

pytestmark = pytest.mark.integration

_DESKTOP_BUDGET = {"documents": 10_000, "relations": 100_000}
_MOBILE_BUDGET = {"documents": 2_000, "relations": 6_000}


def _assert_complete_bounded_accounting(payload, *, documents, relations, budget):
    assert payload.counts.candidate_documents == documents
    assert payload.counts.candidate_relations == relations
    assert len(payload.nodes) <= budget["documents"]
    assert len(payload.edges) <= budget["relations"]
    assert (
        sum(
            1 if node.grain == "document" else node.member_count
            for node in payload.nodes
        )
        == documents
    )
    assert sum(edge.relation_count for edge in payload.edges) == relations
    assert sum(edge.occurrence_weight for edge in payload.edges) == relations
    node_ids = {node.id for node in payload.nodes}
    assert len(node_ids) == len(payload.nodes)
    assert len({edge.id for edge in payload.edges}) == len(payload.edges)
    assert all(
        edge.source in node_ids and edge.target in node_ids for edge in payload.edges
    )


def _authored_edges(session, corpus):
    """Read persisted sources independently of the Graph service projection."""

    result = {
        (f"document:context:{source}", f"document:context:{target}", "wikilink")
        for source, target in session.execute(
            select(PageLink.source_page_id, PageLink.target_page_id).where(
                PageLink.workspace_id == corpus.workspace_id
            )
        )
    }
    context_by_head = dict(
        zip(corpus.context_version_ids, corpus.context_ids, strict=True)
    )
    library_by_head = dict(
        zip(corpus.library_version_ids, corpus.library_ids, strict=True)
    )
    for source, cited_context, cited_library in session.execute(
        select(
            ContextCitation.target_id,
            ContextCitation.cited_context_page_version_id,
            ContextCitation.library_item_version_id,
        ).where(ContextCitation.workspace_id == corpus.workspace_id)
    ):
        target_kind = "context" if cited_context is not None else "library"
        target_id = (
            context_by_head[cited_context]
            if cited_context is not None
            else library_by_head[cited_library]
        )
        result.add(
            (
                f"document:context:{context_by_head[source]}",
                f"document:{target_kind}:{target_id}",
                "citation_page" if cited_context is not None else "citation_source",
            )
        )
    return result


def _assert_capacity_boundaries(session_factory):
    # Seed once and make small, reversible fixture changes between assertions.
    # This exercises actual SQL/visibility paths without five giant fixtures.
    with session_factory.begin() as session:
        corpus = build_capacity_workspace(session)
        service = WorkspaceGraphService(cursor_secret="real-10k-capacity-secret")
        viewer = corpus_actor(corpus, role="viewer")
        desktop = WorkspaceGraphOverviewRequest(budget=_DESKTOP_BUDGET)

        def read(request=desktop):
            return service.query(
                session, corpus.workspace_id, request=request, actor=viewer
            )

        exact = read()
        _assert_complete_bounded_accounting(
            exact, documents=10_000, relations=100_000, budget=_DESKTOP_BUDGET
        )
        assert exact.representation == "exact"
        assert exact.completeness == "complete"
        assert exact.truncated is False
        assert exact.omission_reasons == []
        assert {node.entity_id for node in exact.nodes} == set(corpus.document_ids)
        assert all(
            edge.direction == "directed" and not edge.aggregate for edge in exact.edges
        )
        assert {
            (edge.source, edge.target, edge.relation_kind) for edge in exact.edges
        } == (_authored_edges(session, corpus))
        exact_wire = exact.model_dump_json()
        exact_revision = exact.graph_revision
        del exact

        # Hidden rows must not enter identity, metrics, folder topology, facets,
        # freshness, cursor state, or the complete public wire representation.
        add_hidden_draft_decoys(session, corpus, count=3)
        builder = WorkspaceGraphCorpusBuilder(
            session,
            corpus_key="capacity-10k-extra",
            title="Capacity additions",
            existing_corpus=corpus,
        )
        _restricted_item, restricted_head = builder.add_library(
            0,
            title="RESTRICTED CAPACITY SENTINEL",
            uri="https://example.test/restricted-capacity",
            restricted=True,
        )
        builder.add_relation(
            0,
            source_context_id=corpus.context_ids[0],
            target_kind="library_item_version",
            target_id=restricted_head,
            edge_kind="citation_source",
        )
        assert read().model_dump_json() == exact_wire
        del exact_wire

        added_page_id = builder.add_context(
            0, slug="capacity-extra-document", title="Capacity document 10001"
        )
        document_capped = read()
        _assert_complete_bounded_accounting(
            document_capped, documents=10_001, relations=100_000, budget=_DESKTOP_BUDGET
        )
        assert document_capped.representation == "aggregated"
        assert document_capped.omission_reasons == ["document_budget"]
        assert document_capped.counts.rendered_documents >= 9_998
        assert document_capped.graph_revision != exact_revision
        added_page = session.get(ContextPage, added_page_id)
        assert added_page is not None
        added_page.lifecycle_status = "archived"
        session.flush()

        existing_targets = set(
            session.scalars(
                select(PageLink.target_page_id).where(
                    PageLink.workspace_id == corpus.workspace_id,
                    PageLink.source_page_id == corpus.context_ids[0],
                )
            )
        )
        target_id = next(
            target
            for target in corpus.context_ids[1:]
            if target not in existing_targets
        )
        session.add(
            PageLink(
                id=uuid4(),
                workspace_id=corpus.workspace_id,
                source_page_id=corpus.context_ids[0],
                source_version_id=corpus.context_version_ids[0],
                target_slug=corpus.context_slugs[target_id],
                target_page_id=target_id,
                occurrence_count=1,
                first_snippet="One authored relation beyond the desktop capacity",
                created_at=fixture_time(200_000),
            )
        )
        session.flush()
        relation_capped = read()
        _assert_complete_bounded_accounting(
            relation_capped, documents=10_000, relations=100_001, budget=_DESKTOP_BUDGET
        )
        assert relation_capped.representation == "aggregated"
        assert relation_capped.omission_reasons == ["relation_budget"]
        assert relation_capped.counts.rendered_documents > 9_000

        mobile = read(WorkspaceGraphOverviewRequest(budget=_MOBILE_BUDGET))
        _assert_complete_bounded_accounting(
            mobile, documents=10_000, relations=100_001, budget=_MOBILE_BUDGET
        )
        assert mobile.representation == "aggregated"
        assert mobile.omission_reasons == ["document_budget", "relation_budget"]
        assert mobile.graph_revision == relation_capped.graph_revision
        assert mobile.generated_at == relation_capped.generated_at
        assert mobile.counts.rendered_documents > 100


def test_real_db_10k_and_100k_boundaries_preserve_identity_privacy_and_mobile_budget(
    session_factory,
):
    _assert_capacity_boundaries(session_factory)


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    not os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL", "").startswith(
        "postgresql"
    ),
    reason="requires a dedicated PostgreSQL migration test database",
)
def test_postgres_10k_and_100k_capacity_boundaries():
    database_url = os.environ["DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL"]
    engine = create_engine(database_url)
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    drop_reflected_postgres_metadata(engine)
    try:
        command.upgrade(config, "head")
        _assert_capacity_boundaries(sessionmaker(bind=engine, expire_on_commit=False))
    finally:
        drop_reflected_postgres_metadata(engine)
        engine.dispose()

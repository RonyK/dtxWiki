"""Combined Map uses only the visible current published authored relation union."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from backend.models.context import (
    ContextCitation,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.utils.workspace_graph_corpora import (
    add_hidden_draft_decoys,
    add_knowledge_40_relation_branch_fixtures,
    build_knowledge_40,
    corpus_actor,
)
from backend.tests.utils.workspace_graph_fixture_identity import (
    fixture_time,
    fixture_uuid,
)
from backend.utils.content_hash import content_hash

pytestmark = pytest.mark.integration


def _query(service, session, corpus, lens):
    return service.query(
        session,
        corpus.workspace_id,
        request=WorkspaceGraphOverviewRequest(
            lens=lens, budget={"documents": 2_000, "relations": 6_000}
        ),
        actor=corpus_actor(corpus, role="viewer"),
    )


def _relations(payload):
    return {
        (edge.relation_kind, edge.source, edge.target, edge.layer)
        for edge in payload.edges
    }


def test_combined_is_the_complete_published_union_and_compare_stays_partial(
    session_factory,
):
    assert WorkspaceGraphOverviewRequest().lens == "combined"
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key="combined-authored-union")
        branches = add_knowledge_40_relation_branch_fixtures(
            session, corpus, fixture_key="combined-authored-union-branches"
        )
        service = WorkspaceGraphService(cursor_secret="combined-union-test-secret")
        combined = _query(service, session, corpus, "combined")
        links = _query(service, session, corpus, "links")
        evidence = _query(service, session, corpus, "evidence")
        compare = _query(service, session, corpus, "compare")

        assert combined.representation == "exact"
        assert combined.completeness == "complete"
        assert combined.truncated is False
        assert combined.omission_reasons == []
        assert combined.warnings == []
        assert combined.counts.candidate_documents == 39
        assert combined.counts.candidate_relations == 50
        assert combined.counts.rendered_relations == 50
        assert _relations(combined) == _relations(links) | _relations(evidence)
        assert _relations(combined) == _relations(compare)
        assert links.counts.candidate_relations == 40
        assert evidence.counts.candidate_relations == 10
        assert {edge.relation_kind for edge in combined.edges} == {
            "wikilink",
            "citation_page",
            "citation_source",
        }
        assert all(
            edge.layer
            == ("knowledge" if edge.relation_kind == "wikilink" else "evidence")
            for edge in combined.edges
        )
        for kind, source, target in branches.expected_relation_keys:
            source_kind, source_id = source
            target_kind, target_id = target
            assert (
                kind,
                f"document:{source_kind}:{source_id}",
                f"document:{target_kind}:{target_id}",
                "evidence",
            ) in _relations(combined)

        assert compare.completeness == "partial"
        assert compare.truncated is True
        assert compare.omission_reasons == ["projection_partial"]
        assert compare.warnings == ["compare_projection_not_available"]
        assert compare.capabilities.compare is False
        assert combined.metric_basis == (
            "visible_published_links_and_citations.pre_quick.v1"
        )
        assert compare.metric_basis == "partial_union_of_authored_relations.v1"

        default_scene = service.query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=corpus_actor(corpus, role="viewer"),
        )
        assert default_scene.model_dump_json() == combined.model_dump_json()
        restricted_id = corpus.restricted_library_ids[0]
        assert str(restricted_id) not in combined.model_dump_json()
        owner = service.query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="combined"),
            actor=corpus_actor(corpus, role="owner"),
        )
        assert owner.counts.candidate_documents == 40
        assert owner.counts.candidate_relations == 51
        assert any(
            node.grain == "document" and node.entity_id == restricted_id
            for node in owner.nodes
        )


def test_combined_wire_ignores_usage_manual_hidden_draft_and_stale_head_noise(
    session_factory,
):
    corpus_key = "combined-nonauthoritative-twin"
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key=corpus_key)
        add_knowledge_40_relation_branch_fixtures(
            session, corpus, fixture_key=f"{corpus_key}-branches"
        )
        service = WorkspaceGraphService(cursor_secret="combined-twin-test-secret")
        before = _query(service, session, corpus, "combined")
        before_wire = before.model_dump_json()
        before_projection_count = session.scalar(
            select(func.count())
            .select_from(EdgeProjection)
            .where(EdgeProjection.workspace_id == corpus.workspace_id)
        )
        before_authored_counts = {
            model: session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.workspace_id == corpus.workspace_id)
            )
            for model in (PageLink, ContextCitation)
        }
        visible_ids = {
            node.entity_id for node in before.nodes if node.grain == "document"
        }
        source_id, target_id = corpus.context_ids[:2]
        assert {source_id, target_id} <= visible_ids
        noise_ids = {
            kind: fixture_uuid(corpus_key, "projection-noise", kind)
            for kind in ("co_access", "manual")
        }
        noise_time = fixture_time(100_001)
        session.add_all(
            EdgeProjection(
                id=noise_id,
                workspace_id=corpus.workspace_id,
                source_kind="context_page",
                source_id=source_id,
                target_kind="context_page",
                target_id=target_id,
                edge_kind=kind,
                provenance="usage" if kind == "co_access" else "curated",
                weight=1_000_000.0,
                occurred_at=noise_time,
                source_ref={"fixture": corpus_key, "nonauthoritative": True},
                created_at=noise_time,
                last_seen_at=noise_time,
            )
            for kind, noise_id in noise_ids.items()
        )

        # A superseded published snapshot has real authored rows and visible
        # endpoints. Neither its timestamp nor matching mutable projections may
        # contribute to the current-head Map.
        stale_source_id = corpus.context_ids[30]
        stale_target_id = corpus.context_ids[10]
        stale_head_id = fixture_uuid(corpus_key, "noncurrent-published-head")
        stale_body = (
            f"# Noncurrent snapshot\n\n[[{corpus.context_slugs[stale_target_id]}]]"
        )
        session.add(
            ContextPageVersion(
                id=stale_head_id,
                workspace_id=corpus.workspace_id,
                page_id=stale_source_id,
                version_no=2,
                body_md=stale_body,
                content_hash=content_hash(stale_body),
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id=str(fixture_uuid(corpus_key, "author")),
                version_status="superseded_snapshot",
                created_at=noise_time,
            )
        )
        session.flush()
        stale_link_id = fixture_uuid(corpus_key, "noncurrent-page-link")
        stale_citation_id = fixture_uuid(corpus_key, "noncurrent-citation")
        session.add_all(
            [
                PageLink(
                    id=stale_link_id,
                    workspace_id=corpus.workspace_id,
                    source_page_id=stale_source_id,
                    source_version_id=stale_head_id,
                    target_slug=corpus.context_slugs[stale_target_id],
                    target_page_id=stale_target_id,
                    occurrence_count=1_000_000,
                    created_at=noise_time,
                ),
                ContextCitation(
                    id=stale_citation_id,
                    workspace_id=corpus.workspace_id,
                    target_kind="context_page_version",
                    target_id=stale_head_id,
                    target_location={},
                    library_item_version_id=corpus.library_version_ids[0],
                    location={},
                    citation_type="raw_source",
                    verification_status="verified",
                    created_via="manual_input",
                    created_by_actor_type="human",
                    created_at=noise_time,
                ),
            ]
        )
        stale_projection_ids = []
        for kind, target_kind, relation_target in (
            ("wikilink", "context_page", stale_target_id),
            ("citation_source", "library_item_version", corpus.library_version_ids[0]),
        ):
            projection_id = fixture_uuid(corpus_key, "noncurrent-projection", kind)
            stale_projection_ids.append(projection_id)
            session.add(
                EdgeProjection(
                    id=projection_id,
                    workspace_id=corpus.workspace_id,
                    source_kind="context_page",
                    source_id=stale_source_id,
                    target_kind=target_kind,
                    target_id=relation_target,
                    edge_kind=kind,
                    provenance="curated",
                    weight=1_000_000.0,
                    occurred_at=noise_time,
                    source_ref={
                        "fixture": corpus_key,
                        "source_version": str(stale_head_id),
                    },
                    created_at=noise_time,
                    last_seen_at=noise_time,
                )
            )
        hidden_ids = add_hidden_draft_decoys(session, corpus, count=7)
        session.flush()

        # Positive DB controls prevent an empty/misclassified noise fixture
        # from making the byte twin pass vacuously.
        noise_rows = session.scalars(
            select(EdgeProjection).where(EdgeProjection.id.in_(noise_ids.values()))
        ).all()
        assert {row.id for row in noise_rows} == set(noise_ids.values())
        assert {row.edge_kind for row in noise_rows} == {"co_access", "manual"}
        assert all(
            row.source_id == source_id
            and row.target_id == target_id
            and row.weight == 1_000_000.0
            for row in noise_rows
        )
        assert (
            session.get(ContextPage, stale_source_id).current_published_version_id
            == (corpus.context_version_ids[30])
        )
        assert session.get(ContextPageVersion, stale_head_id).version_status == (
            "superseded_snapshot"
        )
        assert stale_head_id != corpus.context_version_ids[30]
        assert {stale_source_id, stale_target_id, corpus.library_ids[0]} <= visible_ids
        stale_link = session.get(PageLink, stale_link_id)
        assert stale_link.source_version_id == stale_head_id
        assert stale_link.occurrence_count == 1_000_000
        assert (
            session.get(ContextCitation, stale_citation_id).target_id == stale_head_id
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(EdgeProjection)
                .where(EdgeProjection.id.in_(stale_projection_ids))
            )
            == 2
        )
        hidden_pages = session.scalars(
            select(ContextPage).where(ContextPage.id.in_(hidden_ids))
        ).all()
        assert {page.id for page in hidden_pages} == set(hidden_ids)
        assert all(
            page.lifecycle_status == "draft"
            and page.current_published_version_id is None
            and page.latest_draft_version_id is not None
            for page in hidden_pages
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(PageLink)
                .where(PageLink.target_page_id.in_(hidden_ids))
            )
            == 7
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(EdgeProjection)
                .where(
                    EdgeProjection.target_id.in_(hidden_ids),
                    EdgeProjection.weight >= 999_994,
                )
            )
            == 7
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(EdgeProjection)
                .where(EdgeProjection.workspace_id == corpus.workspace_id)
            )
            == before_projection_count + 11
        )
        for model, increment in ((PageLink, 8), (ContextCitation, 1)):
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == corpus.workspace_id)
                )
                == before_authored_counts[model] + increment
            )

        after = _query(service, session, corpus, "combined")
        assert after.representation == "exact"
        assert after.completeness == "complete"
        assert after.counts.candidate_documents == 39
        assert after.counts.candidate_relations == 50
        assert after.model_dump_json() == before_wire
        for hidden_id in (
            *hidden_ids,
            stale_head_id,
            *noise_ids.values(),
            *stale_projection_ids,
        ):
            assert str(hidden_id) not in after.model_dump_json()
        assert "PRIVACY SENTINEL" not in after.model_dump_json()

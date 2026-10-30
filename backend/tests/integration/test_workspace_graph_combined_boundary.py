"""The representative Combined corpus must not rebuild every merge for cursors."""

from __future__ import annotations

from time import perf_counter

import pytest

import backend.services.workspace_graph_service as graph
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.tests.utils.workspace_graph_corpora import (
    build_synthetic_workspace,
    corpus_actor,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("document_count", [2_000, 2_001])
def test_combined_boundary_cursor_checks_do_not_build_expansion_pages(
    session_factory, monkeypatch, document_count
):
    with session_factory.begin() as session:
        corpus = build_synthetic_workspace(
            session,
            documents=document_count,
            profile="synthetic",
            seed=260917,
            workspace_index=0,
            restricted_library_pct=0,
            corpus_key=f"probe-{document_count}-1",
        )
        service = graph.WorkspaceGraphService(cursor_secret="combined-boundary-test")
        actor = corpus_actor(corpus, role="viewer")
        full_scans = 0
        builds = 0
        original_count = graph._mapped_relation_count
        original_build = service._expansion_candidate

        def counted_relations(*args, **kwargs):
            nonlocal full_scans
            full_scans += 1
            return original_count(*args, **kwargs)

        def counted_build(*args, **kwargs):
            nonlocal builds
            builds += 1
            return original_build(*args, **kwargs)

        monkeypatch.setattr(graph, "_mapped_relation_count", counted_relations)
        monkeypatch.setattr(service, "_expansion_candidate", counted_build)
        started = perf_counter()
        overview = service.query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(
                budget={"documents": 2_000, "relations": 6_000}
            ),
            actor=actor,
        )
        elapsed = perf_counter() - started
        print(
            f"Combined {document_count}: overview={elapsed:.3f}s, "
            f"relations={overview.counts.candidate_relations}, "
            f"full_scans={full_scans}, builds={builds}"
        )

        assert overview.counts.candidate_documents == document_count
        assert len(overview.nodes) <= 2_000
        assert len(overview.edges) <= 6_000
        assert sum(edge.relation_count for edge in overview.edges) == (
            overview.counts.candidate_relations
        )
        assert (
            sum(
                1 if node.grain == "document" else node.member_count
                for node in overview.nodes
            )
            == document_count
        )
        # Cursor discovery must only prove that one member can be revealed;
        # constructing the maximum reveal page belongs to the expansion call.
        assert builds == 0
        assert full_scans <= 16

        if document_count == 2_000:
            assert overview.representation == "exact"
            assert overview.expansion_cursor is None
            return

        assert overview.expansion_cursor is not None
        cursor = service._decode_cursor(overview.expansion_cursor)
        cluster = next(
            node for node in overview.nodes if node.id == cursor["cluster_id"]
        )
        full_scans = 0
        started = perf_counter()
        expanded = service.query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(
                budget={"documents": 2_000, "relations": 6_000},
                expansion={
                    "cluster_id": cluster.id,
                    "cursor": overview.expansion_cursor,
                },
            ),
            actor=actor,
        )
        print(
            f"Combined {document_count}: expansion={perf_counter() - started:.3f}s, "
            f"full_scans={full_scans}, builds={builds}"
        )
        assert builds == 1
        assert full_scans <= 16
        assert len(expanded.nodes) <= 2_000
        assert len(expanded.edges) <= 6_000
        assert sum(edge.relation_count for edge in expanded.edges) == (
            overview.counts.candidate_relations
        )
        assert (
            sum(
                1 if node.grain == "document" else node.member_count
                for node in expanded.nodes
            )
            == document_count
        )
        original_document_ids = {
            node.entity_id for node in overview.nodes if node.grain == "document"
        }
        assert any(
            node.grain == "document" and node.entity_id not in original_document_ids
            for node in expanded.nodes
        )

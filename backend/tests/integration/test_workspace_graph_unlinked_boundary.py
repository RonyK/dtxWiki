"""The representative 2K Combined corpus exposes all unlinked documents it can."""

from __future__ import annotations

import gzip

import pytest

import backend.services.workspace_graph_service as graph
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.read_errors import ReadValidationError
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.utils.workspace_graph_corpora import (
    build_synthetic_workspace,
    corpus_actor,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("focus", [None, "linked", "unlinked"])
def test_combined_2001_keeps_unlinked_exact_and_preserves_focus_and_relation_totals(
    session_factory, focus
):
    with session_factory.begin() as session:
        corpus = build_synthetic_workspace(
            session,
            documents=2_001,
            profile="synthetic",
            seed=260917,
            workspace_index=0,
            restricted_library_pct=0,
            corpus_key="probe-2001-1",
        )
        service = WorkspaceGraphService(cursor_secret="unlinked-boundary-test")
        actor = corpus_actor(corpus, role="viewer")
        documents = service._load_documents(session, corpus.workspace_id, actor)
        relations = service._load_relation_universe(
            session, corpus.workspace_id, documents
        )
        incident = {
            key for relation in relations for key in (relation.source, relation.target)
        }
        unlinked = set(documents) - incident
        assert len(unlinked) == 72
        focus_id = None
        if focus is not None:
            focus_keys = incident if focus == "linked" else unlinked
            focus_id = min(focus_keys, key=lambda key: str(key[1]))[1]
        payload = service.query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(
                budget={"documents": 2_000, "relations": 6_000},
                focus_entity_id=focus_id,
            ),
            actor=actor,
        )
        exact_ids = {
            node.entity_id for node in payload.nodes if node.grain == "document"
        }
        print(
            f"Combined 2001 focus={focus}: nodes={len(payload.nodes)}, "
            f"spare={2000 - len(payload.nodes)}, "
            f"unlinked_exact={len({key[1] for key in unlinked} & exact_ids)}/72"
        )
        assert {key[1] for key in unlinked} <= exact_ids
        assert all(
            node.grain == "document" or node.member_count > 1 for node in payload.nodes
        )
        assert len(payload.nodes) <= 2_000
        assert len(payload.edges) <= 6_000
        assert (
            sum(
                1 if node.grain == "document" else node.member_count
                for node in payload.nodes
            )
            == 2_001
        )
        assert (
            sum(edge.relation_count for edge in payload.edges)
            == payload.counts.candidate_relations
        )
        assert payload.algorithm_version == "visible-component-pack.v3"
        if focus is None:
            wire = payload.model_dump_json().encode("utf-8")
            without_topology = payload.model_dump_json(
                exclude={"folder_topology"}
            ).encode("utf-8")
            print(
                f"FR-08 read-only 2001: raw={len(wire)} bytes, "
                f"without_topology={len(without_topology)}, "
                f"gzip={len(gzip.compress(wire))}, "
                f"without_topology_gzip={len(gzip.compress(without_topology))}"
            )
        if focus_id is not None:
            assert focus_id in exact_ids
            assert payload.focus is not None and payload.focus.status == "document"


def test_v3_grouping_revision_rejects_a_v2_cursor_even_when_cluster_is_unchanged(
    session_factory, monkeypatch
):
    with session_factory.begin() as session:
        corpus = build_synthetic_workspace(
            session,
            documents=2_001,
            profile="synthetic",
            seed=260917,
            workspace_index=0,
            restricted_library_pct=0,
            corpus_key="probe-2001-1",
        )
        service = WorkspaceGraphService(cursor_secret="unlinked-revision-test")
        actor = corpus_actor(corpus, role="viewer")
        request = WorkspaceGraphOverviewRequest(
            budget={"documents": 2_000, "relations": 6_000}
        )
        monkeypatch.setattr(graph, "ALGORITHM_VERSION", "visible-component-pack.v2")
        old = service.query(session, corpus.workspace_id, request=request, actor=actor)
        assert old.expansion_cursor is not None
        old_cursor = service._decode_cursor(old.expansion_cursor)
        monkeypatch.setattr(graph, "ALGORITHM_VERSION", "visible-component-pack.v3")
        current = service.query(
            session, corpus.workspace_id, request=request, actor=actor
        )
        assert current.graph_revision != old.graph_revision
        assert (
            current.schema_version == old.schema_version == "workspace-graph-scene.v1"
        )
        assert [node.model_dump() for node in current.nodes] == [
            node.model_dump() for node in old.nodes
        ]
        assert [edge.model_dump() for edge in current.edges] == [
            edge.model_dump() for edge in old.edges
        ]
        with pytest.raises(
            ReadValidationError,
            match="does not match the current actor, query, or revision",
        ):
            service.query(
                session,
                corpus.workspace_id,
                request=WorkspaceGraphOverviewRequest(
                    budget={"documents": 2_000, "relations": 6_000},
                    expansion={
                        "cluster_id": old_cursor["cluster_id"],
                        "cursor": old.expansion_cursor,
                    },
                ),
                actor=actor,
            )

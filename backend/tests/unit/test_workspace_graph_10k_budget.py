"""Canonical large-scene identity and cursor binding; SQL tests own capacity."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

import backend.services.workspace_graph_service as graph
from backend.policy.auth import ActorContext
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.read_errors import ReadValidationError

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_WORKSPACE = UUID(int=42)
_LARGE_BUDGET = {"documents": 10_000, "relations": 100_000}


def _universe(document_count: int, relation_count: int):
    keys = [("context", UUID(int=index + 1)) for index in range(document_count)]
    documents = {
        key: graph._Document(
            key=key,
            entity_id=key[1],
            entity_kind="context",
            label=f"Document {index}",
            path=f"/context?target={key[1]}",
            head_id=UUID(int=document_count + index + 1),
            updated_at=_NOW,
        )
        for index, key in enumerate(keys)
    }
    relations = [
        graph._Relation(
            source=keys[index % document_count],
            target=keys[
                (index % document_count + index // document_count + 1) % document_count
            ],
            kind="wikilink" if index % 2 == 0 else "citation_page",
            layer="knowledge" if index % 2 == 0 else "evidence",
            occurred_at=_NOW,
            occurrence_weight=index % 3 + 1,
        )
        for index in range(relation_count)
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    return documents, relations


def _actor() -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id="visible-budget-viewer",
        token_kind="session",
        scopes=(),
        workspace_role="viewer",
        role="viewer",
    )


def _render(service, documents, relations, request):
    return service._render(
        documents,
        relations,
        request,
        workspace_id=_WORKSPACE,
        graph_revision=service._graph_revision(documents, relations),
        request_fingerprint=service._request_fingerprint(request),
        actor_fingerprint=service._actor_fingerprint(_actor()),
    )


def _assert_visible_coverage(rendered, documents, relations, budget):
    members = [member for group in rendered.groups for member in group.members]
    assert len(members) == len(set(members)) == len(documents)
    assert set(members) == set(documents)
    assert len(rendered.nodes) <= budget["documents"]
    assert len(rendered.edges) <= budget["relations"]
    assert len({node.id for node in rendered.nodes}) == len(rendered.nodes)
    assert len({edge.id for edge in rendered.edges}) == len(rendered.edges)
    node_ids = {node.id for node in rendered.nodes}
    assert all(
        edge.source in node_ids and edge.target in node_ids for edge in rendered.edges
    )
    assert sum(edge.relation_count for edge in rendered.edges) == len(relations)
    assert sum(edge.occurrence_weight for edge in rendered.edges) == sum(
        relation.occurrence_weight for relation in relations
    )
    assert {(edge.relation_kind, edge.layer) for edge in rendered.edges} == {
        (relation.kind, relation.layer) for relation in relations
    }


# Actual SQL capacity and overflow belong to test_workspace_graph_10k_capacity.
# Keep one large canonical-identity pin here, not a second capacity matrix.
def test_large_budget_preserves_preoptimization_revision_and_authored_kind_identity():
    relation_count = 100_000
    documents, relations = _universe(10_000, relation_count)
    service = graph.WorkspaceGraphService(cursor_secret="relation-budget-test-secret")
    rendered = _render(
        service,
        documents,
        relations,
        WorkspaceGraphOverviewRequest(
            budget=_LARGE_BUDGET,
        ),
    )
    _assert_visible_coverage(rendered, documents, relations, _LARGE_BUDGET)
    assert rendered.representation == "exact"
    assert rendered.omission_reasons == []
    assert len(rendered.nodes) == 10_000
    assert len(rendered.edges) == 100_000
    # Retain the pre-10K-optimization canonical digest so an optimization
    # cannot silently change revision/cursor identity for the same source.
    expected_revision = (
        "wgr1:6cad592c34ce651584869810f08bfce255c80d2d73e31924c5eada93f505c360"
    )
    assert service._graph_revision(documents, relations) == expected_revision
    assert (
        service._graph_revision(
            dict(reversed(list(documents.items()))), list(reversed(relations))
        )
        == expected_revision
    )


@pytest.mark.parametrize(
    "changed_budget",
    [
        {"documents": 9, "relations": 100},
        {"documents": 10, "relations": 99},
    ],
)
def test_expansion_cursor_cannot_be_reused_with_a_changed_budget(changed_budget):
    # Cursor binding is independent of source cardinality. A small aggregate
    # exercises both budget fields without rebuilding ten thousand documents.
    documents, relations = _universe(13, 13)
    service = graph.WorkspaceGraphService(cursor_secret="cursor-budget-test-secret")
    request = WorkspaceGraphOverviewRequest(budget={"documents": 10, "relations": 100})
    rendered = _render(service, documents, relations, request)
    assert rendered.expansion_cursor is not None
    cursor = service._decode_cursor(rendered.expansion_cursor)
    changed = WorkspaceGraphOverviewRequest(
        budget=changed_budget,
        expansion={
            "cluster_id": cursor["cluster_id"],
            "cursor": rendered.expansion_cursor,
        },
    )
    assert service._request_fingerprint(changed) != service._request_fingerprint(
        request
    )
    with pytest.raises(ReadValidationError):
        _render(service, documents, relations, changed)

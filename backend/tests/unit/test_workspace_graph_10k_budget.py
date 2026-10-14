"""Explicit larger Workspace budgets preserve visible identities and cursors."""

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
_MOBILE_BUDGET = {"documents": 2_000, "relations": 6_000}


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


@pytest.mark.parametrize("document_count", [10_000, 10_001])
def test_explicit_document_boundary_renders_exact_or_bounded_aggregate(document_count):
    documents, relations = _universe(document_count, document_count)
    service = graph.WorkspaceGraphService(cursor_secret="large-budget-test-secret")
    request = WorkspaceGraphOverviewRequest(budget=_LARGE_BUDGET)
    rendered = _render(service, documents, relations, request)
    _assert_visible_coverage(rendered, documents, relations, _LARGE_BUDGET)
    if document_count == 10_000:
        assert rendered.representation == "exact"
        assert all(node.grain == "document" for node in rendered.nodes)
        assert rendered.omission_reasons == []
        assert rendered.expansion_cursor is None
        return

    assert rendered.representation == "aggregated"
    assert rendered.omission_reasons == ["document_budget"]
    assert sum(node.grain == "document" for node in rendered.nodes) >= 9_998
    assert rendered.expansion_cursor is not None
    cursor = service._decode_cursor(rendered.expansion_cursor)
    expanded = _render(
        service,
        documents,
        relations,
        WorkspaceGraphOverviewRequest(
            budget=_LARGE_BUDGET,
            expansion={
                "cluster_id": cursor["cluster_id"],
                "cursor": rendered.expansion_cursor,
            },
        ),
    )
    _assert_visible_coverage(expanded, documents, relations, _LARGE_BUDGET)
    exact_before = {
        node.entity_id for node in rendered.nodes if node.grain == "document"
    }
    assert any(
        node.grain == "document" and node.entity_id not in exact_before
        for node in expanded.nodes
    )


def test_existing_mobile_request_still_aggregates_above_6000_relations():
    documents, relations = _universe(2_000, 6_001)
    service = graph.WorkspaceGraphService(cursor_secret="mobile-budget-test-secret")
    rendered = _render(
        service,
        documents,
        relations,
        WorkspaceGraphOverviewRequest(
            budget=_MOBILE_BUDGET,
        ),
    )
    _assert_visible_coverage(rendered, documents, relations, _MOBILE_BUDGET)
    assert rendered.representation == "aggregated"
    assert rendered.omission_reasons == ["relation_budget"]
    assert sum(node.grain == "document" for node in rendered.nodes) > 1_900


@pytest.mark.parametrize("relation_count", [100_000, 100_001])
def test_explicit_relation_boundary_preserves_counts_and_authored_kind_identity(
    relation_count,
):
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
    assert rendered.representation == (
        "exact" if relation_count == 100_000 else "aggregated"
    )
    assert rendered.omission_reasons == (
        [] if relation_count == 100_000 else ["relation_budget"]
    )
    if relation_count == 100_000:
        assert len(rendered.nodes) == 10_000
        assert len(rendered.edges) == 100_000


@pytest.mark.parametrize(
    "changed_budget",
    [
        {"documents": 9_999, "relations": 100_000},
        {"documents": 10_000, "relations": 99_999},
    ],
)
def test_expansion_cursor_cannot_be_reused_with_a_changed_budget(changed_budget):
    documents, relations = _universe(10_001, 10_001)
    service = graph.WorkspaceGraphService(cursor_secret="cursor-budget-test-secret")
    request = WorkspaceGraphOverviewRequest(budget=_LARGE_BUDGET)
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

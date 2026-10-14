"""Relation caps must not hide unrelated documents while exact slots remain."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

import backend.services.workspace_graph_service as graph

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _documents(count):
    return {
        ("context", UUID(int=index)): graph._Document(
            key=("context", UUID(int=index)),
            entity_id=UUID(int=index),
            entity_kind="context",
            label=f"Document {index}",
            path=f"/context?target={UUID(int=index)}",
            head_id=UUID(int=10_000 + index),
            updated_at=_NOW,
        )
        for index in range(1, count + 1)
    }


def _assert_partition(
    groups, documents, relations, *, document_budget, relation_budget
):
    members = [member for group in groups for member in group.members]
    assert len(members) == len(set(members)) == len(documents)
    assert set(members) == set(documents)
    assert len(groups) <= document_budget
    assert graph._mapped_relation_count(groups, relations) <= relation_budget
    assert all(group.is_document or len(group.members) > 1 for group in groups)


@pytest.mark.parametrize("relation_count", [6_001, 6_002])
def test_unlinked_documents_fill_slots_after_connected_prefix_hits_relation_cap(
    relation_count,
):
    documents = _documents(2_001)
    connected = list(documents)[:1_929]
    unlinked = set(documents) - set(connected)
    relations = [
        graph._Relation(
            connected[index % len(connected)],
            connected[
                (index % len(connected) + index // len(connected) + 1) % len(connected)
            ],
            "wikilink",
            "knowledge",
            _NOW,
        )
        for index in range(relation_count)
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    groups = graph.WorkspaceGraphService._aggregate_groups(
        documents,
        relations,
        document_budget=2_000,
        relation_budget=6_000,
        lens="combined",
    )
    exact = {group.members[0] for group in groups if group.is_document}
    print(
        f"relation boundary {relation_count}: groups={len(groups)}, "
        f"unlinked_exact={len(unlinked & exact)}/{len(unlinked)}"
    )
    assert unlinked <= exact
    _assert_partition(
        groups, documents, relations, document_budget=2_000, relation_budget=6_000
    )


def test_one_member_connected_residual_is_an_exact_document_not_a_cluster():
    documents = _documents(4)
    keys = list(documents)
    relations = [
        graph._Relation(keys[0], keys[2], "wikilink", "knowledge", _NOW),
        graph._Relation(keys[1], keys[3], "wikilink", "knowledge", _NOW),
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    groups = graph.WorkspaceGraphService._aggregate_groups(
        documents, relations, document_budget=3, relation_budget=10, lens="combined"
    )
    assert sum(group.is_document for group in groups) == 2
    _assert_partition(
        groups, documents, relations, document_budget=3, relation_budget=10
    )


def test_one_member_expansion_residual_is_an_exact_document():
    documents = _documents(4)
    keys = list(documents)
    cluster = graph._Group(tuple(keys[:2]), cluster_kind="link_cluster")
    groups = [cluster, *(graph._Group((key,)) for key in keys[2:])]
    result = graph.WorkspaceGraphService._expansion_candidate(
        groups,
        cluster,
        documents,
        [],
        offset=1,
        document_budget=4,
        relation_budget=1,
        pinned=None,
    )
    assert result is not None
    expanded, revealed = result
    assert revealed == 1
    assert all(group.is_document for group in expanded)
    _assert_partition(expanded, documents, [], document_budget=4, relation_budget=1)


@pytest.mark.parametrize(
    "document_count,budget", [(1, 1), (2, 1), (3, 2), (4, 3), (10, 10)]
)
def test_unlinked_only_fill_reserves_the_residual_or_reveals_every_document(
    document_count, budget
):
    documents = _documents(document_count)
    groups = graph.WorkspaceGraphService._aggregate_groups(
        documents, [], document_budget=budget, relation_budget=1, lens="combined"
    )
    expected_exact = document_count if document_count <= budget else max(0, budget - 1)
    assert sum(group.is_document for group in groups) == expected_exact
    _assert_partition(groups, documents, [], document_budget=budget, relation_budget=1)


def test_unlinked_fill_remains_independent_of_input_iteration_order():
    documents = _documents(40)
    connected = list(documents)[:20]
    relations = [
        graph._Relation(source, target, "wikilink", "knowledge", _NOW)
        for source in connected
        for target in connected
        if source != target
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    options = {"document_budget": 35, "relation_budget": 200, "lens": "combined"}
    first = graph.WorkspaceGraphService._aggregate_groups(
        documents, relations, **options
    )
    reversed_groups = graph.WorkspaceGraphService._aggregate_groups(
        dict(reversed(list(documents.items()))), list(reversed(relations)), **options
    )
    assert [group.identity for group in first] == [
        group.identity for group in reversed_groups
    ]
    assert set(documents) - set(connected) <= {
        group.members[0] for group in first if group.is_document
    }
    _assert_partition(
        first, documents, relations, document_budget=35, relation_budget=200
    )

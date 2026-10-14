"""Expansion feasibility and incremental packing retain exact relation identities."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from itertools import product
from uuid import UUID

import pytest

import backend.services.workspace_graph_service as graph

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _documents():
    return {
        ("context", UUID(int=index)): graph._Document(
            key=("context", UUID(int=index)),
            entity_id=UUID(int=index),
            entity_kind="context",
            label=f"Document {index}",
            path=f"/context?target={UUID(int=index)}",
            head_id=UUID(int=100 + index),
            updated_at=_NOW,
        )
        for index in range(1, 9)
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_incremental_merge_matches_full_identity_count_at_every_step(reverse):
    documents = _documents()
    keys = list(documents)
    relations = [
        graph._Relation(source, target, kind, layer, _NOW)
        for source, target in product(keys, repeat=2)
        for kind, layer in (
            ("wikilink", "knowledge"),
            ("citation_page", "evidence"),
            # Distinct layers must not collapse even with the same kind.
            ("wikilink", "evidence"),
        )
    ]
    # Repeated occurrences are one mapped identity, including self-relations.
    relations += relations[:20]
    if reverse:
        relations.reverse()
    groups = {index: graph._Group((key,)) for index, key in enumerate(keys)}
    partition = graph._MappedRelationPartition(list(groups.values()), relations)
    assert partition.count == graph._mapped_relation_count(
        list(groups.values()), relations
    )
    for merged_index, (first, second) in enumerate(
        ((0, 1), (2, 3), (4, 5), (6, 7), (8, 9), (10, 11), (12, 13)),
        start=8,
    ):
        before = partition.count
        members = (*groups.pop(first).members, *groups.pop(second).members)
        groups[merged_index] = graph._Group(members, cluster_kind="other_cluster")
        partition.merge(first, second, merged_index)
        assert partition.count == graph._mapped_relation_count(
            list(groups.values()), relations
        )
        assert partition.count <= before
    assert partition.count == 3


@pytest.mark.parametrize("seed", range(8))
def test_one_member_feasibility_agrees_with_actual_builder_for_every_budget(seed):
    documents = _documents()
    keys = list(documents)
    rng = random.Random(seed)
    relations = [
        graph._Relation(
            rng.choice(keys),
            rng.choice(keys),
            kind,
            layer,
            _NOW,
        )
        for index in range(40)
        for kind, layer in (
            (("citation_page", "evidence"),)
            if index % 3 == 0
            else (("wikilink", "knowledge"),)
        )
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    cluster = graph._Group(tuple(keys[:4]), cluster_kind="link_cluster")
    groups = [cluster, *(graph._Group((key,)) for key in keys[4:])]
    service = graph.WorkspaceGraphService(cursor_secret="expansion-floor-test")
    for offset, document_budget, relation_budget, pinned in product(
        range(4), (1, 2, 3, 8), (1, 3, 6, 12, 24, 64), (None, keys[-1])
    ):
        options = {
            "offset": offset,
            "document_budget": document_budget,
            "relation_budget": relation_budget,
            "pinned": pinned,
        }
        feasible = service._can_expand_cluster(groups, cluster, relations, **options)
        candidate = service._expansion_candidate(
            groups, cluster, documents, relations, **options
        )
        assert feasible == (candidate is not None), (seed, options)
        if candidate is not None:
            expanded, revealed = candidate
            exact_keys = {group.members[0] for group in expanded if group.is_document}
            assert set(cluster.members[offset : offset + revealed]) <= exact_keys
            if pinned is not None:
                assert pinned in exact_keys
            assert len(expanded) <= document_budget
            assert graph._mapped_relation_count(expanded, relations) <= relation_budget
            assert {member for group in expanded for member in group.members} == set(
                keys
            )


def test_cursor_feasibility_never_builds_or_hashes_an_expansion(monkeypatch):
    documents = _documents()
    cluster = graph._Group(tuple(documents), cluster_kind="link_cluster")

    def forbidden(*_args, **_kwargs):
        pytest.fail("cursor feasibility constructed or hashed a full expansion page")

    monkeypatch.setattr(graph, "_group_member_digest", forbidden)
    monkeypatch.setattr(graph, "_mapped_relation_count", forbidden)
    monkeypatch.setattr(graph.WorkspaceGraphService, "_expansion_candidate", forbidden)
    options = {"document_budget": 2, "relation_budget": 1, "pinned": None}
    assert graph.WorkspaceGraphService._can_expand_cluster(
        [cluster], cluster, [], offset=0, **options
    )
    assert not graph.WorkspaceGraphService._can_expand_cluster(
        [cluster], cluster, [], offset=8, **options
    )

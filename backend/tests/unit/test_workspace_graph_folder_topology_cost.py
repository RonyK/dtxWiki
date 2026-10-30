"""Folder topology work follows rendered edges, not authored relations.

The topology used to hash a fresh edge id for every authored relation and to
look both endpoints up again. Relations that fold into one rendered edge share
its id, so it is hashed once per rendered edge. The bytes must not change.
"""

from __future__ import annotations

import random

import backend.services.workspace_graph_service as graph
from backend.tests.utils.workspace_graph_reference import (
    KINDS,
    NOW,
    new_document,
    reference_folder_topology,
    seeded_universe,
)

_SERVICE = graph.WorkspaceGraphService(cursor_secret="topology-cost-test")


def _group(members, kind):
    return graph._Group(tuple(members), cluster_kind=kind if len(members) > 1 else None)


def test_topology_matches_the_earlier_topology_on_seeded_scenes():
    folded = 0
    for seed in range(60):
        documents, relations = seeded_universe(seed)
        keys = list(documents)
        random.Random(seed).shuffle(keys)
        a, b = len(keys) // 3, 2 * len(keys) // 3
        scenes = {
            "exact": [graph._Group((key,)) for key in documents],
            # Exact documents beside two clusters, so relations fold into edges.
            "clustered": [
                *(graph._Group((key,)) for key in keys[:a]),
                *([_group(keys[a:b], "link_cluster")] if keys[a:b] else []),
                *([_group(keys[b:], "other_cluster")] if keys[b:] else []),
            ],
        }
        for name, groups in scenes.items():
            rendered = _SERVICE._materialize_groups(
                groups, documents, relations, omission_reasons=["document_budget"]
            )
            # The second pass keeps half the edges, as the irreducible-identity
            # fallback may: counts of a dropped edge must not move elsewhere.
            for kept in ("every edge", "half the edges"):
                expected = reference_folder_topology(documents, relations, rendered)
                actual = graph.WorkspaceGraphService._folder_topology(
                    documents, relations, rendered
                )
                assert actual is not None
                assert actual.model_dump_json() == expected.model_dump_json(), (
                    seed,
                    name,
                    kept,
                )
                folded += any(row.relation_count > 1 for row in expected.edge_slices)
                rendered.edges = rendered.edges[: max(1, len(rendered.edges) // 2)]
    # The seeds must keep folding several authored relations into one slice.
    assert folded >= 50, folded


def test_edge_ids_are_hashed_per_rendered_edge_not_per_relation(monkeypatch):
    rng = random.Random(7)
    documents = {doc.key: doc for doc in map(new_document, range(30))}
    keys = list(documents)
    relations = [
        graph._Relation(rng.choice(keys), rng.choice(keys), *rng.choice(KINDS), NOW)
        for _ in range(300)
    ]
    groups = [_group(keys[start : start + 10], "link_cluster") for start in (0, 10, 20)]
    rendered = _SERVICE._materialize_groups(
        groups, documents, relations, omission_reasons=["document_budget"]
    )
    assert len(relations) > 10 * len(rendered.edges)

    hashed = 0
    original = graph._aggregate_edge_id

    def counting(*args):
        nonlocal hashed
        hashed += 1
        return original(*args)

    monkeypatch.setattr(graph, "_aggregate_edge_id", counting)
    topology = graph.WorkspaceGraphService._folder_topology(
        documents, relations, rendered
    )
    assert topology is not None
    assert hashed <= len(rendered.edges)

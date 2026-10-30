"""The reveal search of ``_aggregate_groups`` after it stopped rebuilding groups.

It looks for the largest ranked prefix of exact documents that still fits both
budgets. It used to rebuild every candidate prefix as groups (rehashing each
residual) and rescan every relation, so each extra probe cost a whole workspace
pass. It now labels the relations once and answers each probe from the labels.
The groups it returns must not change.
"""

from __future__ import annotations

import random

import pytest

import backend.services.workspace_graph_service as graph
from backend.tests.utils.workspace_graph_reference import (
    NOW,
    new_document,
    reference_prefix,
    reference_reveal,
    seeded_universe,
)


def _shape(groups):
    return [(group.cluster_kind, group.members, group.identity) for group in groups]


@pytest.fixture
def probes(monkeypatch):
    """Every ``_RevealProbe`` built by a search: ``(probe, groups, ranked, ...)``."""

    built = []

    class Recording(graph._RevealProbe):
        def __init__(self, groups, ranked, relations, **budgets):
            super().__init__(groups, ranked, relations, **budgets)
            built.append((self, groups, ranked))

    monkeypatch.setattr(graph, "_RevealProbe", Recording)
    return built


def test_search_matches_the_earlier_search_on_seeded_universes(probes):
    bisected = fitting = refused = 0
    for seed in range(60):
        lens = "links"
        documents, relations = seeded_universe(seed)
        rng = random.Random(seed)
        size = len(documents)
        identities = len({(r.source, r.target, r.kind, r.layer) for r in relations})
        for _ in range(4):
            budgets = {
                "document_budget": rng.choice(
                    [1, 2, max(1, size // 2), max(1, size - 1), size]
                ),
                "relation_budget": rng.choice(
                    [0, 1, identities // 2, max(0, identities - 1), identities]
                ),
            }
            actual = graph.WorkspaceGraphService._aggregate_groups(
                documents, relations, lens=lens, **budgets
            )
            probe, groups, ranked = probes[-1]
            expected, searched = reference_reveal(
                documents, relations, groups, ranked, **budgets
            )
            assert _shape(actual) == _shape(expected), (seed, lens, budgets)
            # The search only asks about some prefixes; each answer must match.
            for count in range(len(ranked) + 1):
                earlier = reference_prefix(groups, ranked, relations, count, **budgets)
                assert probe.fits(count) is (earlier is not None), (
                    seed,
                    budgets,
                    count,
                )
                fitting += earlier is not None
                refused += earlier is None
            bisected += searched > 1
    # The seeds must keep reaching a real search, with prefixes on both sides.
    assert min(bisected, fitting, refused) >= 20, (bisected, fitting, refused)


def _universe(links: str):
    documents = {doc.key: doc for doc in map(new_document, range(8))}
    keys = list(documents)
    if links == "self":
        relations = [graph._Relation(k, k, "wikilink", "knowledge", NOW) for k in keys]
    else:
        relations = [
            graph._Relation(a, b, "wikilink", "knowledge", NOW)
            for a in keys
            for b in keys
            if a != b
        ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    return documents, relations


# In a clique of 8, revealing k of the 8 ranked members leaves k + 1 groups
# (k when none is left over) and k*k + k + 1 distinct mapped relations (56 when
# all 8 are revealed; k = 7 leaves one member and no self relation, also 56).
# "self" links every document only to itself: all exact, nothing to rank.
@pytest.mark.parametrize(
    "links,document_budget,relation_budget,exact,residual",
    [
        pytest.param("clique", 100, 1_000, 8, 0, id="everything-fits"),
        pytest.param("clique", 100, 56, 8, 0, id="relations-fit-exactly"),
        pytest.param("clique", 100, 55, 6, 2, id="relations-one-short"),
        pytest.param("clique", 100, 2, 0, 8, id="relations-none-fits"),
        pytest.param("clique", 100, 0, 0, 8, id="relations-zero"),
        pytest.param("clique", 8, 1_000, 8, 0, id="documents-fit-exactly"),
        pytest.param("clique", 7, 1_000, 6, 2, id="documents-one-short"),
        pytest.param("clique", 1, 1_000, 0, 8, id="documents-none-fits"),
        pytest.param("self", 100, 8, 8, 0, id="capacity-zero"),
    ],
)
def test_search_stops_at_the_budget_boundaries(
    links, document_budget, relation_budget, exact, residual
):
    documents, relations = _universe(links)
    groups = graph.WorkspaceGraphService._aggregate_groups(
        documents,
        relations,
        document_budget=document_budget,
        relation_budget=relation_budget,
        lens="links",
    )
    assert (
        sum(group.is_document for group in groups),
        sum(len(group.members) for group in groups if not group.is_document),
    ) == (exact, residual)


class _PassCountingList(list):
    """A list that counts how many times something starts iterating it."""

    passes = 0

    def __iter__(self):
        self.passes += 1
        return super().__iter__()


def test_a_deep_search_costs_no_more_than_a_shallow_one(monkeypatch):
    """Each probe used to rescan the relations and rebuild its candidate groups."""

    documents = {doc.key: doc for doc in map(new_document, range(96))}
    keys = list(documents)
    relations = [
        graph._Relation(keys[i], keys[(i + 1) % 96], "wikilink", "knowledge", NOW)
        for i in range(96)
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    built = 0
    original = graph._Group.__post_init__

    def counting(self):
        nonlocal built
        built += 1
        original(self)

    monkeypatch.setattr(graph._Group, "__post_init__", counting)

    def cost(document_budget):
        nonlocal built
        built = 0
        scanned = _PassCountingList(relations)
        groups = graph.WorkspaceGraphService._aggregate_groups(
            documents,
            scanned,
            document_budget=document_budget,
            relation_budget=10_000,
            lens="links",
        )
        return scanned.passes, built, sum(group.is_document for group in groups)

    shallow = cost(97)  # every member fits: the first probe answers
    deep = cost(48)  # about half fit: a binary search over the prefix
    assert shallow[2] == 96
    assert 0 < deep[2] < 96  # the search really had to look
    assert deep[0] == shallow[0], "relation passes grew with the probes"
    assert deep[1] <= shallow[1], "groups were rebuilt for probes"

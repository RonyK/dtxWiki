from __future__ import annotations

import ast
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from backend.services import graph_rank
from backend.services.graph_rank import (
    DENOMINATOR_KINDS,
    KIND_ORDER,
    PREDECAYED_KINDS,
    RANK_ALIASES,
    RANK_PARAMS,
    RankDenominators,
    RankParam,
    _check_registry,
    candidate_order_key,
    citation_weight,
    effective_weight,
    fair_share_quotas,
    rank_param,
    rank_score,
    round_robin_take,
    tree_weight,
    wikilink_weight,
)
from backend.services.graph_semantics import ACTION_SEMANTICS, EDGE_SEMANTICS

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def test_no_amount_of_co_reading_outranks_one_authored_link():
    """Raw co_access weights run to the tens of thousands; a link is 1.

    Sorting by raw weight let a busy co-read pair bury every authored link.
    """

    assert rank_score("co_access", 40_000, None, NOW) == pytest.approx(0.199875)
    assert rank_score("co_access", 40_000, None, NOW) < rank_score(
        "wikilink", 1.0, None, NOW
    )
    assert rank_score("wikilink", 1.0, None, NOW) == 0.25


def test_rank_table_is_the_0822_table_verbatim():
    assert RANK_PARAMS == {
        "wikilink": RankParam(1.00, 3, None),
        "citation_page": RankParam(0.95, 2, None),
        "citation_source": RankParam(0.90, 2, None),
        "tree": RankParam(0.85, 1, None),
        "version_parent": RankParam(0.55, 1, 90),
        "page_provenance": RankParam(0.50, 1, None),
        "promoted_from": RankParam(0.50, 1, None),
        "library_sync_version": RankParam(0.35, 1, 180),
        "ask_citation": RankParam(0.30, 2, 60),
        "co_access": RankParam(0.20, 25, 30),
    }


def test_every_edge_kind_is_ranked_through_its_alias():
    for kind in EDGE_SEMANTICS:
        assert rank_param(kind) in RANK_PARAMS.values()
    assert rank_param("version_merge") is RANK_PARAMS["version_parent"]


def test_kind_order_lists_every_relation_then_every_action_once():
    relations = [
        "wikilink",
        "citation_page",
        "citation_source",
        "tree",
        "version_parent",
        "version_merge",
        "page_provenance",
        "promoted_from",
        "library_sync_version",
        "ask_citation",
        "co_access",
    ]

    assert list(KIND_ORDER) == [*relations, *ACTION_SEMANTICS]


@pytest.mark.counterexample(reason="rank registry drift must fail at import")
def test_registry_check_rejects_an_unranked_kind():
    def check(**overrides):
        arguments = {
            "edge_semantics": EDGE_SEMANTICS,
            "action_semantics": ACTION_SEMANTICS,
            "kind_order": KIND_ORDER,
            "rank_params": RANK_PARAMS,
            "aliases": RANK_ALIASES,
            "predecayed": PREDECAYED_KINDS,
        }
        arguments.update(overrides)
        _check_registry(**arguments)

    check()  # the real registry passes
    with pytest.raises(RuntimeError, match="unranked"):
        check(
            edge_semantics={**EDGE_SEMANTICS, "invented": object()},
            kind_order=(*KIND_ORDER, "invented"),
        )
    missing = dict(RANK_PARAMS)
    missing.pop("tree")
    with pytest.raises(RuntimeError, match="unranked"):
        check(rank_params=missing)
    with pytest.raises(RuntimeError, match="do not resolve"):
        check(aliases={**RANK_ALIASES, "tree": "wikilink"})
    with pytest.raises(RuntimeError, match="both relations and actions"):
        check(action_semantics={**ACTION_SEMANTICS, "wikilink": object()})
    with pytest.raises(RuntimeError, match="without a half-life"):
        check(predecayed=frozenset({"co_access", "wikilink"}))
    with pytest.raises(RuntimeError, match="exactly once"):
        check(kind_order=KIND_ORDER[:-1])
    with pytest.raises(RuntimeError, match="out of range"):
        check(rank_params={**RANK_PARAMS, "tree": RankParam(1.5, 1, None)})
    with pytest.raises(RuntimeError, match="out of range"):
        check(rank_params={**RANK_PARAMS, "tree": RankParam(0.85, 0, None)})
    with pytest.raises(RuntimeError, match="alias"):
        check(aliases={"version_merge": "missing"})


def test_one_link_from_a_focused_page_outweighs_one_from_a_hub_page():
    focused = wikilink_weight(1, out_degree=3)
    hub = wikilink_weight(1, out_degree=200)

    assert focused > hub
    assert rank_score("wikilink", focused, None, NOW) > rank_score(
        "wikilink", hub, None, NOW
    )


def test_normalisation_helpers():
    assert citation_weight(2, citation_total=2) == pytest.approx(1.0)
    assert tree_weight(0) == 1.0
    assert tree_weight(9) == pytest.approx(1 / math.log2(11))
    assert wikilink_weight(3, out_degree=0) == 3.0
    assert rank_score("wikilink", wikilink_weight(1, 1), None, NOW) == (
        pytest.approx(0.1738, abs=1e-4)
    )
    assert rank_score("tree", tree_weight(0), None, NOW) == pytest.approx(0.425)
    assert rank_score("citation_page", citation_weight(1, 2), None, NOW) == (
        pytest.approx(0.19)
    )


def test_effective_weight_uses_each_family_source_denominator():
    counts = RankDenominators(wikilink_out_degree=6, citation_total=14, tree_children=4)

    assert effective_weight("wikilink", 3, counts) == pytest.approx(3 / 3.0)
    assert effective_weight("citation_page", 2, counts) == pytest.approx(2 / 4.0)
    assert effective_weight("citation_source", 2, counts) == pytest.approx(2 / 4.0)
    # A placement's share of its folder: three siblings beside it.
    assert effective_weight("tree", 1.0, counts) == pytest.approx(1 / math.log2(5))
    for kind in ("co_access", "version_parent", "ask_citation", "promoted_from"):
        assert effective_weight(kind, 7.5, counts) == 7.5
    # Missing counts read as zeros, and a negative raw weight as zero.
    assert effective_weight("wikilink", 3, None) == 3.0
    assert effective_weight("tree", 1.0, None) == 1.0
    assert effective_weight("wikilink", -2, counts) == 0.0


def test_denominator_kinds_are_exactly_the_kinds_effective_weight_divides():
    """Callers fetch denominators only for DENOMINATOR_KINDS; a kind missing
    from it would silently rank on its undivided weight."""

    counts = RankDenominators(wikilink_out_degree=6, citation_total=14, tree_children=4)
    divided = {
        kind
        for kind in RANK_PARAMS
        if effective_weight(kind, 1.0, counts) != effective_weight(kind, 1.0, None)
    }
    assert divided == DENOMINATOR_KINDS


def test_co_access_is_not_decayed_twice():
    """Its stored weight already carries the table's 30-day half-life."""

    assert rank_param("co_access").half_life_days == 30
    assert rank_score("co_access", 30, NOW - timedelta(days=90), NOW) == rank_score(
        "co_access", 30, None, NOW
    )


def test_half_life_halves_and_undated_relations_do_not_decay():
    fresh = rank_score("version_parent", 1, NOW, NOW)

    assert rank_score("version_parent", 1, NOW - timedelta(days=90), NOW) == (
        pytest.approx(fresh / 2)
    )
    assert rank_score("version_merge", 1, NOW - timedelta(days=90), NOW) == (
        pytest.approx(fresh / 2)
    )
    assert rank_score("ask_citation", 1, None, NOW) == rank_score(
        "ask_citation", 1, NOW, NOW
    )
    assert rank_score("library_sync_version", 1, None, NOW) == rank_score(
        "library_sync_version", 1, NOW, NOW
    )
    # A time after "now" is age 0, and a naive time is read as UTC.
    assert rank_score("version_parent", 1, NOW + timedelta(days=5), NOW) == fresh
    naive = (NOW - timedelta(days=90)).replace(tzinfo=None)
    assert rank_score("version_parent", 1, naive, NOW) == pytest.approx(fresh / 2)


@pytest.mark.parametrize("kind", list(EDGE_SEMANTICS))
def test_rank_is_bounded_and_zero_for_a_weight_that_is_not_positive(kind):
    ceiling = rank_param(kind).w
    for weight in (-1.0, 0.0, 1e-9, 1.0, 1e6, 1e18, math.inf, math.nan):
        value = rank_score(kind, weight, NOW, NOW)
        assert 0.0 <= value <= ceiling <= 1.0
        if not weight > 0 or not math.isfinite(weight):
            assert value == 0.0


def test_candidate_order_key():
    older = NOW - timedelta(days=3)
    a, b = UUID(int=1), UUID(int=2)
    keys = [
        candidate_order_key(0.2, None, "context_page", a),
        candidate_order_key(0.5, older, "context_page", b),
        candidate_order_key(0.2, NOW, "context_page", b),
        candidate_order_key(0.2, older, "context_page", a),
        candidate_order_key(0.2, None, "context_page", b),
        candidate_order_key(0.2, None, "agent_page", b),
    ]

    assert sorted(keys) == [
        keys[1],  # higher rank first
        keys[2],  # then dated, newest first
        keys[3],
        keys[5],  # then undated, by neighbour identity
        keys[0],
        keys[4],
    ]


def test_fair_share_quotas():
    assert fair_share_quotas(20, 3) == [7, 7, 6]
    assert fair_share_quotas(9, 4) == [3, 2, 2, 2]
    assert fair_share_quotas(2, 5) == [1, 1, 0, 0, 0]
    assert fair_share_quotas(7, 0) == []
    assert fair_share_quotas(-3, 2) == [0, 0]
    assert sum(fair_share_quotas(49, 6)) == 49


def test_round_robin_take():
    decided = []

    def take(item):
        decided.append(item)
        if item.startswith("dup"):
            return "skipped"
        if item.startswith("big"):
            return "rejected"
        return "admitted"

    queues = [
        ["a1", "a2", "a3"],
        ["dup-b", "big-b", "b1", "b2"],
        ["c1"],
        [],
    ]

    positions = round_robin_take(queues, [2, 1, 1, 3], take)

    # One admission per queue per round, in queue order; skipped and rejected
    # items are consumed without counting toward the quota.
    assert decided == ["a1", "dup-b", "big-b", "b1", "c1", "a2"]
    assert positions == [2, 3, 1, 0]
    with pytest.raises(ValueError):
        round_robin_take([["x"]], [1, 1], take)


def test_action_kinds_have_no_rank_parameters():
    with pytest.raises(KeyError):
        rank_param("edited")


def test_graph_rank_is_pure_arithmetic():
    """The Knowledge Map reuses it on its own counts, without the database."""

    tree = ast.parse(Path(graph_rank.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert imported <= {
        "__future__",
        "math",
        "collections.abc",
        "dataclasses",
        "datetime",
        "typing",
        "uuid",
        "backend.services.graph_semantics",
    }

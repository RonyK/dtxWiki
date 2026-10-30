"""The Local Graph budget: fair shares first, merit for the rest.

Synthetic candidates only; the scenes that motivate each rule live in
test_graph_query_service.py and test_local_graph_mixed_scene.py.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid5

from backend.schemas.graph import (
    GraphBudget,
    GraphEdge,
    GraphNode,
    GraphQueryRequest,
    GraphSeed,
)
from backend.services.graph_query_service import (
    GraphQueryService,
    _anchor_and_role,
    _EdgeCandidate,
    graph_node_id,
)
from backend.services.graph_semantics import NODE_FAMILY_BY_KIND

NAMESPACE = UUID("2c7f55c4-94d6-4b8d-a5a0-6b7b1a1f0c11")


def _node(name: str, *, seed: bool = False, kind: str = "context_page") -> GraphNode:
    entity_id = uuid5(NAMESPACE, name)
    return GraphNode(
        id=graph_node_id(kind, entity_id),
        entity_kind=kind,
        entity_id=entity_id,
        family=NODE_FAMILY_BY_KIND[kind],
        label=name,
        is_seed=seed,
    )


def _candidate(
    source: GraphNode,
    target: GraphNode,
    kind: str,
    *,
    anchor: GraphNode | None,
    role: str,
    rank: float = 0.5,
    priority: int = 0,
) -> _EdgeCandidate:
    edge = GraphEdge(
        id=f"g1e:{source.label}->{target.label}:{kind}",
        source=source.id,
        target=target.id,
        edge_kind=kind,
        label=kind,
        layer="knowledge",
        provenance="curated",
        rank_score=rank,
    )
    return _EdgeCandidate(
        edge=edge,
        nodes=(source, target),
        priority=priority,
        semantic_identity=(edge.source, edge.target, edge.edge_kind, None),
        anchor=anchor.id if anchor is not None else None,
        role=role,
    )


def _apply(seeds, candidates, *, nodes: int, edges: int):
    request = GraphQueryRequest(
        seeds=[GraphSeed(kind="context_page", id=seed.entity_id) for seed in seeds],
        budget=GraphBudget(nodes=nodes, edges=edges),
    )
    return GraphQueryService._apply_budget(
        request=request,
        seed_nodes={seed.id: seed for seed in seeds},
        candidates=candidates,
        extra_nodes={},
    )


def _kinds(edges) -> dict[str, int]:
    counts: dict[str, int] = {}
    for edge in edges:
        counts[edge.edge_kind] = counts.get(edge.edge_kind, 0) + 1
    return counts


def test_each_queue_gets_its_share_before_merit_fills_the_rest():
    seed = _node("seed", seed=True)
    candidates = [
        _candidate(
            _node(f"backlinker-{i}"), seed, "wikilink", anchor=seed, role="in", rank=0.5
        )
        for i in range(30)
    ]
    candidates += [
        _candidate(
            _node(f"folder-{i}", kind="workspace_tree_node"),
            seed,
            "tree",
            anchor=seed,
            role="in",
            rank=0.4,
        )
        for i in range(3)
    ]
    candidates += [
        _candidate(
            seed,
            _node(f"cited-{i}"),
            "citation_page",
            anchor=seed,
            role="out",
            rank=0.2,
        )
        for i in range(2)
    ]

    _nodes, edges, truncated = _apply([seed], candidates, nodes=50, edges=20)

    # Quotas [7, 7, 6] over wikilink-in, citation_page-out, tree-in: the two
    # small queues are taken whole and their unused slots go to the links.
    assert _kinds(edges) == {"wikilink": 15, "tree": 3, "citation_page": 2}
    assert truncated is True


def test_duplicates_do_not_count_toward_a_queue_quota():
    first, second = _node("first", seed=True), _node("second", seed=True)
    shared = _candidate(first, second, "wikilink", anchor=first, role="out", rank=0.9)
    mirror = _candidate(first, second, "wikilink", anchor=second, role="in", rank=0.9)
    # The first seed also has strong backlinks that merit would take first.
    strong = [
        _candidate(
            _node(f"strong-{i}"), first, "wikilink", anchor=first, role="in", rank=0.8
        )
        for i in range(4)
    ]
    weak = [
        _candidate(
            _node(f"weak-{i}"), second, "wikilink", anchor=second, role="in", rank=0.1
        )
        for i in range(4)
    ]

    _nodes, edges, _truncated = _apply(
        [first, second], [shared, mirror, *strong, *weak], nodes=50, edges=3
    )

    # Groups share 3 slots as [2, 1]. The second seed's copy of the shared
    # link is skipped without spending its one slot, which still buys that
    # seed one of its own neighbours ahead of the first seed's stronger ones.
    assert [edge.id for edge in edges].count(shared.edge.id) == 1
    weak_sources = {candidate.edge.source for candidate in weak}
    assert sum(edge.source in weak_sources for edge in edges) == 1
    assert len(edges) == 3


def test_each_seed_gets_an_equal_share_whatever_its_queue_count():
    busy, quiet = _node("busy", seed=True), _node("quiet", seed=True)
    candidates = []
    for kind, role in (
        ("wikilink", "out"),
        ("wikilink", "in"),
        ("citation_page", "out"),
        ("tree", "in"),
    ):
        for i in range(10):
            neighbour = _node(
                f"busy-{kind}-{role}-{i}",
                kind="workspace_tree_node" if kind == "tree" else "context_page",
            )
            source, target = (busy, neighbour) if role == "out" else (neighbour, busy)
            candidates.append(
                _candidate(source, target, kind, anchor=busy, role=role, rank=0.5)
            )
    candidates += [
        _candidate(
            _node(f"quiet-in-{i}"), quiet, "wikilink", anchor=quiet, role="in", rank=0.1
        )
        for i in range(10)
    ]

    _nodes, edges, _truncated = _apply([busy, quiet], candidates, nodes=11, edges=150)

    # 9 node slots as [5, 4]: the quiet seed's one queue gets its seed's
    # share, not one pick per round beside the busy seed's four queues.
    assert sum(edge.target == quiet.id for edge in edges) == 4
    assert len(edges) == 9


def test_a_candidate_that_does_not_fit_is_consumed_and_selection_continues():
    seed = _node("seed", seed=True)
    present = _node("present")
    # One node fits beside the seed, so the fair share is one slot. After it,
    # a new neighbour does not fit, but a second relation to the node already
    # shown costs no node and is still taken.
    candidates = [
        _candidate(seed, present, "wikilink", anchor=seed, role="out", rank=0.9),
        _candidate(
            seed, _node("too-many"), "wikilink", anchor=seed, role="out", rank=0.8
        ),
        _candidate(seed, present, "citation_page", anchor=seed, role="out", rank=0.7),
        _candidate(
            _node("folder", kind="workspace_tree_node"),
            seed,
            "tree",
            anchor=seed,
            role="in",
            rank=0.1,
        ),
    ]

    _nodes, edges, truncated = _apply([seed], candidates, nodes=2, edges=10)

    assert {(edge.edge_kind, edge.target) for edge in edges} == {
        ("wikilink", present.id),
        ("citation_page", present.id),
    }
    assert truncated is True


def _dated_candidate(source, target, kind, *, anchor, role, day):
    candidate = _candidate(source, target, kind, anchor=anchor, role=role, rank=0.0)
    edge = candidate.edge.model_copy(
        update={"occurred_at": datetime(2026, 9, day, tzinfo=UTC)}
    )
    return replace(candidate, edge=edge)


def _session_scene(seed):
    """Five Ask turns included by the seed, t1 newest, each from its own
    session; the session of the oldest turn started last."""

    turns = [_node(f"t{i}", kind="ask_turn") for i in range(1, 6)]
    included = [
        _dated_candidate(turn, seed, "included", anchor=seed, role="in", day=20 - i)
        for i, turn in enumerate(turns)
    ]
    sessions = [
        _dated_candidate(
            _node(f"s{i + 1}", kind="ask_session"),
            turn,
            "searched",
            anchor=None,
            role="context",
            day=10 + i,
        )
        for i, turn in enumerate(turns)
    ]
    return turns, included + sessions


def _connected_to(seed, edges):
    reached, frontier = {seed.id}, [seed.id]
    while frontier:
        node = frontier.pop()
        for edge in edges:
            for here, there in ((edge.source, edge.target), (edge.target, edge.source)):
                if here == node and there not in reached:
                    reached.add(there)
                    frontier.append(there)
    return reached


def test_unanchored_relations_come_after_every_seed():
    seed = _node("seed", seed=True)
    turns, candidates = _session_scene(seed)
    candidates += [
        _candidate(
            seed, _node(f"linked-{i}"), "wikilink", anchor=seed, role="out", rank=0.1
        )
        for i in range(4)
    ]

    _nodes, edges, _truncated = _apply([seed], candidates, nodes=50, edges=7)

    # 7 slots as [4, 3]. The seed's links and its two newest turns come
    # first; the unanchored share then takes the sessions of those turns.
    turn_ids = {edge.source for edge in edges if edge.edge_kind == "included"}
    assert turn_ids == {turns[0].id, turns[1].id}
    assert {edge.target for edge in edges if edge.edge_kind == "searched"} == turn_ids
    assert sum(edge.edge_kind == "wikilink" for edge in edges) == 3


def test_session_relations_cannot_take_an_anchored_share_or_float_free():
    seed = _node("seed", seed=True)
    turns, candidates = _session_scene(seed)

    nodes, edges, _truncated = _apply([seed], candidates, nodes=4, edges=150)

    # 3 node slots as [2, 1]: the two newest turns, then one session that
    # hangs off them, never the oldest turn pulled in by its session.
    assert {edge.source for edge in edges if edge.edge_kind == "included"} == {
        turns[0].id,
        turns[1].id,
    }
    assert _connected_to(seed, edges) == {node.id for node in nodes}

    nodes, edges, _truncated = _apply([seed], candidates, nodes=50, edges=4)

    assert _connected_to(seed, edges) == {node.id for node in nodes}
    assert sum(edge.edge_kind == "included" for edge in edges) == 2


def test_fair_share_uses_every_slot_it_hands_out():
    seeds = [_node(f"seed-{i}", seed=True) for i in range(3)]
    candidates = [
        _candidate(
            seed,
            _node(f"{seed.label}-link-{j}"),
            "wikilink",
            anchor=seed,
            role="out",
            rank=0.5,
        )
        for seed in seeds
        for j in range(10)
    ]

    for edges_budget in range(1, 13):
        _nodes, edges, _truncated = _apply(
            seeds, candidates, nodes=250, edges=edges_budget
        )
        assert len(edges) == edges_budget
        per_seed = sorted(
            sum(edge.source == seed.id for edge in edges) for seed in seeds
        )
        assert per_seed[-1] - per_seed[0] <= 1


def test_every_tier_keeps_strict_priority():
    seed = _node("seed", seed=True)
    current = [
        _candidate(
            seed, _node(f"current-{i}"), "wikilink", anchor=seed, role="out", rank=0.01
        )
        for i in range(3)
    ]
    observed = [
        _candidate(
            seed,
            _node(f"observed-{i}"),
            "wikilink",
            anchor=seed,
            role="out",
            rank=0.99,
            priority=1,
        )
        for i in range(3)
    ]

    _nodes, edges, _truncated = _apply([seed], observed + current, nodes=50, edges=3)

    assert {edge.target for edge in edges} == {c.edge.target for c in current}


def test_slots_left_over_by_a_small_queue_go_to_the_highest_rank():
    """Edges bind at 6: shares [2, 2, 2] over wikilink, citation and tree.
    The one tree placement leaves a slot, and merit gives it to the
    stronger citations, not to the next link in round-robin order."""

    seed = _node("seed", seed=True)
    candidates = [
        _candidate(
            seed, _node(f"weak-{i}"), "wikilink", anchor=seed, role="out", rank=0.1
        )
        for i in range(10)
    ]
    candidates += [
        _candidate(
            seed,
            _node(f"strong-{i}"),
            "citation_page",
            anchor=seed,
            role="out",
            rank=0.9,
        )
        for i in range(10)
    ]
    candidates.append(
        _candidate(
            _node("folder", kind="workspace_tree_node"),
            seed,
            "tree",
            anchor=seed,
            role="in",
            rank=0.5,
        )
    )

    _nodes, edges, _truncated = _apply([seed], candidates, nodes=50, edges=6)

    assert _kinds(edges) == {"wikilink": 2, "citation_page": 3, "tree": 1}


def test_a_relation_between_seeds_is_anchored_on_the_first_seed_requested():
    first, second = _node("first", seed=True), _node("second", seed=True)
    seed_nodes = {second.id: second, first.id: first}
    edge = _candidate(first, second, "wikilink", anchor=first, role="out").edge

    # Request order is the dict order: the second seed was asked for first.
    assert _anchor_and_role(edge, seed_nodes) == (second.id, "in")
    assert _anchor_and_role(edge, {first.id: first, second.id: second}) == (
        first.id,
        "out",
    )
    undirected = edge.model_copy(update={"direction": "undirected"})
    assert _anchor_and_role(undirected, seed_nodes) == (second.id, "undirected")
    assert _anchor_and_role(edge, {}) == (None, "context")

"""F09' schema and induced relation-class quota contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from backend.schemas.graph import (
    GraphBudget,
    GraphEdge,
    GraphNode,
    GraphQueryRequest,
    GraphSeed,
)
from backend.services.graph_query_service import (
    _EdgeCandidate,
    _select_induced_edges,
    graph_node_id,
)
from backend.services.graph_service import GraphService

NAMESPACE = UUID("06709ff5-102b-4722-bd18-f9484825ad24")


def _node(name: str, *, seed: bool = False, folder: bool = False) -> GraphNode:
    entity_id = uuid5(NAMESPACE, name)
    kind = "workspace_tree_node" if folder else "context_page"
    return GraphNode(
        id=graph_node_id(kind, entity_id),
        entity_kind=kind,
        entity_id=entity_id,
        family="structural" if folder else "document",
        label=name,
        is_seed=seed,
    )


def _candidate(source: GraphNode, target: GraphNode, kind: str, index: int):
    edge = GraphEdge(
        id=f"g1e:{source.label}:{target.label}:{kind}:{index}",
        source=source.id,
        target=target.id,
        edge_kind=kind,
        label=kind,
        layer="knowledge",
        provenance="curated",
        rank_score=0.9 - index * 0.001,
    )
    return _EdgeCandidate(
        edge=edge,
        nodes=(source, target),
        priority=0,
        semantic_identity=(edge.source, edge.target, edge.edge_kind, None),
        anchor=None,
        role="context",
    )


def test_default_depth_is_two_and_seed_count_cannot_exceed_node_budget():
    seeds = [
        GraphSeed(kind="context_page", id=uuid5(NAMESPACE, str(i))) for i in range(2)
    ]
    assert GraphQueryRequest(seeds=seeds).depth == 2
    assert GraphQueryRequest(seeds=seeds, depth=1).depth == 1
    with pytest.raises(
        ValidationError, match="node budget must accommodate every seed"
    ):
        GraphQueryRequest(seeds=seeds, budget=GraphBudget(nodes=1, edges=1))


def test_induced_relation_budget_reserves_every_available_class():
    seed = _node("seed", seed=True)
    h1 = [_node(f"h1-{i}") for i in range(10)]
    h2 = [_node(f"h2-{i}") for i in range(10)]
    folder = _node("folder", folder=True)
    nodes = {node.id: node for node in [seed, *h1, *h2, folder]}
    hops = {seed.id: 0, folder.id: 0}
    hops.update({node.id: 1 for node in h1})
    hops.update({node.id: 2 for node in h2})
    candidates = []
    for index in range(10):
        candidates.append(_candidate(seed, h1[index], "wikilink", index))
        candidates.append(
            _candidate(h1[index], h1[(index + 1) % 10], "wikilink", index)
        )
        candidates.append(
            _candidate(h1[index], h2[(index + 1) % 10], "wikilink", index)
        )
        candidates.append(
            _candidate(h2[index], h2[(index + 1) % 10], "wikilink", index)
        )
    for index, node in enumerate(h1):
        candidates.append(_candidate(folder, node, "tree", index))

    selected, omitted = _select_induced_edges(candidates, hops, nodes, 20, 2)

    ids = {edge.id for edge in selected}
    assert len(selected) == 20
    assert sum(":tree:" in edge_id for edge_id in ids) >= 2
    assert sum(edge_id.startswith("g1e:seed:") for edge_id in ids) >= 6
    assert (
        sum(edge_id.startswith("g1e:h1-") and ":h1-" in edge_id for edge_id in ids) >= 5
    )
    assert (
        sum(edge_id.startswith("g1e:h1-") and ":h2-" in edge_id for edge_id in ids) >= 4
    )
    assert (
        sum(edge_id.startswith("g1e:h2-") and ":h2-" in edge_id for edge_id in ids) >= 3
    )
    assert set(omitted) == {"folder", "seed", "h1", "h12", "h2"}


def test_second_frontier_partition_keeps_eight_rows_for_129_anchors(monkeypatch):
    caps: list[int] = []

    def empty_partition(*_args, **kwargs):
        caps.append(kwargs["cap"])
        return []

    monkeypatch.setattr(
        GraphService, "_ranked_partition_rows", staticmethod(empty_partition)
    )
    service = GraphService()
    session = object()
    workspace_id = uuid5(NAMESPACE, "workspace")
    service.ranked_projection_edges(
        session,
        workspace_id,
        seeds=tuple(
            ("context_page", uuid5(NAMESPACE, f"anchor-{index}"))
            for index in range(129)
        ),
        actor=None,
        edge_kinds=("wikilink",),
        neighbor_kinds=("context_page",),
        occurred_from=None,
        occurred_to=None,
        direction="both",
        edge_budget=750,
        now=datetime(2026, 9, 28, tzinfo=UTC),
        hydration_context=service.hydration_context(session, workspace_id, None),
        min_partition_cap=8,
    )
    assert caps == [8, 8]

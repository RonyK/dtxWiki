from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from math import exp2
from threading import RLock
from uuid import UUID

from sqlalchemy import case, func, select, tuple_
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.graph import KnowledgeNodeStat
from backend.models.review import ReviewDecision, ReviewItem
from backend.services.graph_cache import edge_version

NodeRef = tuple[str, UUID]
PROVENANCE_PRIORITY = {"curated": 3, "derived": 2, "usage": 1}
PPR_NODE_BUDGET = 500
_CACHE_LIMIT = 32
_subgraph_cache: OrderedDict[
    tuple[UUID, tuple[NodeRef, ...], int, int], tuple[tuple[object, ...], ...]
] = OrderedDict()
_subgraph_cache_lock = RLock()


@dataclass(frozen=True, slots=True)
class PprResult:
    kind: str
    entity_id: UUID
    score: float
    trust_prior: float


def subgraph_ppr(
    session: Session,
    workspace_id: UUID,
    seeds: list[tuple[str, UUID, float]],
    *,
    top_k: int = 20,
    node_budget: int = PPR_NODE_BUDGET,
) -> list[PprResult]:
    seed_weights: defaultdict[NodeRef, float] = defaultdict(float)
    for kind, entity_id, weight in seeds:
        seed_weights[(kind, entity_id)] += max(float(weight), 0.0)
    if not seed_weights:
        return []
    seed_refs = tuple(sorted(seed_weights, key=lambda item: (item[0], str(item[1]))))
    bounded_budget = max(len(seed_refs), min(node_budget, PPR_NODE_BUDGET))
    cache_key = (
        workspace_id,
        seed_refs,
        bounded_budget,
        edge_version(session, workspace_id),
    )
    with _subgraph_cache_lock:
        edges = _subgraph_cache.get(cache_key)
        if edges is not None:
            _subgraph_cache.move_to_end(cache_key)
    if edges is None:
        edges = _load_subgraph(
            session,
            workspace_id,
            set(seed_refs),
            node_budget=bounded_budget,
        )
        with _subgraph_cache_lock:
            _subgraph_cache[cache_key] = edges
            _subgraph_cache.move_to_end(cache_key)
            while len(_subgraph_cache) > _CACHE_LIMIT:
                _subgraph_cache.popitem(last=False)

    adjacency: dict[NodeRef, dict[NodeRef, float]] = defaultdict(dict)
    nodes = set(seed_refs)
    for source_kind, source_id, target_kind, target_id, weight in edges:
        source = (str(source_kind), source_id)
        target = (str(target_kind), target_id)
        nodes.update((source, target))
        bounded_weight = max(float(weight), 0.0)
        adjacency[source][target] = adjacency[source].get(target, 0.0) + bounded_weight
        adjacency[target][source] = adjacency[target].get(source, 0.0) + bounded_weight
    total_seed = sum(seed_weights.values()) or float(len(seed_weights))
    personalization = {node: seed_weights.get(node, 0.0) / total_seed for node in nodes}
    ranks = dict(personalization)
    damping = 0.5
    for _ in range(12):
        dangling = sum(
            ranks.get(node, 0.0) for node in nodes if not adjacency.get(node)
        )
        updated = {
            node: (1.0 - damping) * personalization[node]
            + damping * dangling * personalization[node]
            for node in nodes
        }
        for source, targets in adjacency.items():
            total_weight = sum(targets.values())
            if total_weight <= 0:
                continue
            for target, weight in targets.items():
                updated[target] += (
                    damping * ranks.get(source, 0.0) * weight / total_weight
                )
        if max(abs(updated[node] - ranks.get(node, 0.0)) for node in nodes) < 1e-6:
            ranks = updated
            break
        ranks = updated
    page_priors = context_page_trust_priors(
        session,
        workspace_id,
        {node_id for kind, node_id in nodes if kind == "context_page"},
    )
    trust = defaultdict(float)
    trust.update(
        {("context_page", page_id): prior for page_id, prior in page_priors.items()}
    )
    return [
        PprResult(kind=node[0], entity_id=node[1], score=score, trust_prior=trust[node])
        for node, score in sorted(
            ranks.items(),
            key=lambda item: (-item[1], -trust[item[0]], item[0][0], str(item[0][1])),
        )[:top_k]
    ]


def context_page_trust_priors(
    session: Session | None,
    workspace_id: UUID,
    page_ids: set[UUID],
    *,
    now: datetime | None = None,
) -> dict[UUID, float]:
    if session is None or not page_ids:
        return {}
    now = now or datetime.now(UTC)
    rows = session.execute(
        select(
            ContextPage.id,
            ContextPageVersion.change_set_id,
            ContextPageVersion.created_by_actor_kind,
            ContextPageVersion.created_at,
            KnowledgeNodeStat.in_degree,
        )
        .join(
            ContextPageVersion,
            ContextPage.current_published_version_id == ContextPageVersion.id,
        )
        .outerjoin(
            KnowledgeNodeStat,
            (KnowledgeNodeStat.workspace_id == ContextPage.workspace_id)
            & (KnowledgeNodeStat.kind == "context_page")
            & (KnowledgeNodeStat.entity_id == ContextPage.id),
        )
        .where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.id.in_(page_ids),
        )
    ).all()
    if not rows:
        return {}
    max_in_degree = (
        session.scalar(
            select(func.max(KnowledgeNodeStat.in_degree)).where(
                KnowledgeNodeStat.workspace_id == workspace_id,
                KnowledgeNodeStat.kind == "context_page",
            )
        )
        or 0
    )
    change_set_ids = {row.change_set_id for row in rows if row.change_set_id}
    review_ratios: dict[UUID, float] = {}
    if change_set_ids:
        decision_rows = session.execute(
            select(
                ReviewItem.change_set_id,
                func.count(ReviewDecision.id),
                func.sum(case((ReviewDecision.decision_type == "approve", 1), else_=0)),
            )
            .join(ReviewDecision, ReviewDecision.review_item_id == ReviewItem.id)
            .where(ReviewItem.change_set_id.in_(change_set_ids))
            .group_by(ReviewItem.change_set_id)
        ).all()
        review_ratios = {
            change_set_id: float(approved or 0) / total
            for change_set_id, total, approved in decision_rows
            if total
        }
    priors: dict[UUID, float] = {}
    for row in rows:
        review = review_ratios.get(row.change_set_id, 0.0)
        citation = (row.in_degree or 0) / max_in_degree if max_in_degree else 0.0
        human = 1.0 if row.created_by_actor_kind == "human" else 0.0
        age_days = max(0.0, (now - row.created_at).total_seconds() / 86400.0)
        freshness = exp2(-age_days / 90.0)
        priors[row.id] = 0.4 * review + 0.3 * citation + 0.2 * human + 0.1 * freshness
    return priors


def _load_subgraph(
    session: Session,
    workspace_id: UUID,
    seeds: set[NodeRef],
    *,
    node_budget: int,
) -> tuple[tuple[object, ...], ...]:
    nodes = set(seeds)
    frontier = set(seeds)
    selected: list[tuple[object, ...]] = []
    seen_edges: set[UUID] = set()
    for _ in range(2):
        if not frontier or len(nodes) >= node_budget:
            break
        rows = _frontier_edges(
            session,
            workspace_id,
            frontier,
            limit=node_budget,
        )
        rows.sort(
            key=lambda edge: (
                -PROVENANCE_PRIORITY.get(edge.provenance, 0),
                -edge.weight,
                str(edge.id),
            )
        )
        next_frontier: set[NodeRef] = set()
        for edge in rows:
            if edge.id in seen_edges:
                continue
            source = (edge.source_kind, edge.source_id)
            target = (edge.target_kind, edge.target_id)
            new_nodes = {source, target} - nodes
            if len(nodes) + len(new_nodes) > node_budget:
                continue
            seen_edges.add(edge.id)
            nodes.update(new_nodes)
            next_frontier.update(new_nodes)
            selected.append(
                (
                    edge.source_kind,
                    edge.source_id,
                    edge.target_kind,
                    edge.target_id,
                    edge.weight,
                )
            )
        frontier = next_frontier
    return tuple(selected)


def _frontier_edges(
    session: Session,
    workspace_id: UUID,
    frontier: set[NodeRef],
    *,
    limit: int,
) -> list[Row]:
    """Use both indexes without letting an outbound hub starve incoming edges."""

    if limit <= 0:
        return []
    selected: dict[UUID, Row] = {}
    for provenance in ("curated", "derived", "usage"):
        remaining = limit - len(selected)
        if remaining <= 0:
            break
        directional_rows = [
            session.execute(
                select(
                    EdgeProjection.id,
                    EdgeProjection.source_kind,
                    EdgeProjection.source_id,
                    EdgeProjection.target_kind,
                    EdgeProjection.target_id,
                    EdgeProjection.provenance,
                    EdgeProjection.weight,
                )
                .where(
                    EdgeProjection.workspace_id == workspace_id,
                    EdgeProjection.provenance == provenance,
                    tuple_(kind_column, id_column).in_(frontier),
                )
                .order_by(
                    EdgeProjection.weight.desc(),
                    EdgeProjection.id.desc(),
                )
                .limit(remaining)
            ).all()
            for kind_column, id_column in (
                (EdgeProjection.source_kind, EdgeProjection.source_id),
                (EdgeProjection.target_kind, EdgeProjection.target_id),
            )
        ]
        if remaining == 1:
            candidates = {edge.id: edge for batch in directional_rows for edge in batch}
            for edge in sorted(candidates.values(), key=_edge_priority_key)[:1]:
                selected.setdefault(edge.id, edge)
            continue

        # Count the reservation by edges this direction actually contributes: an
        # intra-frontier edge tops BOTH batches, so a positional slice would let
        # it spend the incoming quota while adding no incoming edge at all.
        reserved_per_direction = max(1, remaining // 2)
        for batch in directional_rows:
            added = 0
            for edge in batch:
                if added >= reserved_per_direction:
                    break
                if edge.id in selected:
                    continue
                selected[edge.id] = edge
                added += 1
        candidates = {edge.id: edge for batch in directional_rows for edge in batch}
        for edge in sorted(candidates.values(), key=_edge_priority_key):
            if len(selected) >= limit:
                break
            selected.setdefault(edge.id, edge)
    return list(selected.values())


def _edge_priority_key(edge: Row) -> tuple[int, float, str]:
    return (
        -PROVENANCE_PRIORITY.get(edge.provenance, 0),
        -edge.weight,
        str(edge.id),
    )

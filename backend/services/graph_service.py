"""GraphService — knowledge-edge read primitives (design §19.5, G1).

Reads the ``edge_projection`` read model (the physical ``knowledge_edges``).
Every returned neighbor passes actor visibility (§3.3) through a per-kind
HYDRATORS registry: a kind without a hydrator is never returned, so adding an
edge kind cannot silently leak entities. PPR/subgraph/co-access are G3/N6.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.context import ContextPage, EdgeProjection
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import (
    actor_can_read_drafts,
    actor_can_read_sensitive_agent_pages,
)
from backend.schemas.agent import SENSITIVE_AGENT_PAGE_CATEGORIES
from backend.schemas.read import GraphNeighborEntry, GraphNeighborsPayload
from backend.services.actor_visible_read import (
    VISIBLE_AGENT_SENSITIVITIES,
    context_page_visible,
    library_item_visible,
)
from backend.services.entity_ref import (
    EntityKind,
    context_concept_path,
    library_concept_path,
)
from backend.services.library_canonical import get_canonical_library_version
from backend.services.read_errors import ReadNotFoundError
from backend.services.tree_projection import TreeProjectionService


@dataclass(frozen=True)
class NeighborEdge:
    kind: str
    id: UUID
    title: str
    path: str | None
    edge_kind: str
    weight: float
    direction: str  # "out" | "in"


@dataclass(frozen=True)
class _Hydrated:
    title: str
    path: str | None


class _HydrationContext:
    """Per-``neighbors()``-call hydration state shared across every batch and
    direction. Carries the session/workspace/actor and lazily builds the tree
    projection ONCE — a fresh load per tree-hydrator call would rebuild the
    whole-workspace projection up to ~8x per request.
    """

    def __init__(
        self,
        session: Session,
        workspace_id: UUID,
        actor: ActorContext | None,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.actor = actor
        self._tree_projection = None

    def tree_projection(self):
        if self._tree_projection is None:
            self._tree_projection = TreeProjectionService().load(
                self.session, self.workspace_id, self.actor
            )
        return self._tree_projection


def _hydrate_context_pages(
    ctx: _HydrationContext, ids: list[UUID]
) -> dict[UUID, _Hydrated]:
    result: dict[UUID, _Hydrated] = {}
    if not ids:
        return result
    for page in ctx.session.scalars(
        select(ContextPage)
        .where(ContextPage.id.in_(ids))
        .where(ContextPage.workspace_id == ctx.workspace_id)
    ).all():
        if not context_page_visible(page, ctx.actor):
            continue
        result[page.id] = _Hydrated(
            title=page.title, path=context_concept_path(page.slug)
        )
    return result


def _hydrate_library_items(
    ctx: _HydrationContext, ids: list[UUID]
) -> dict[UUID, _Hydrated]:
    result: dict[UUID, _Hydrated] = {}
    if not ids:
        return result
    for item in _visible_library_items(ctx, set(ids)).values():
        result[item.id] = _Hydrated(
            title=item.title or item.uri, path=library_concept_path(item.id)
        )
    return result


def _hydrate_agent_pages(
    ctx: _HydrationContext, ids: list[UUID]
) -> dict[UUID, _Hydrated]:
    if not ids:
        return {}
    statement = select(AgentPage).where(
        AgentPage.id.in_(ids),
        AgentPage.workspace_id == ctx.workspace_id,
        AgentPage.lifecycle_status != "archived",
    )
    if not actor_can_read_sensitive_agent_pages(ctx.actor):
        statement = statement.where(
            AgentPage.category.notin_(SENSITIVE_AGENT_PAGE_CATEGORIES),
            AgentPage.sensitivity.in_(VISIBLE_AGENT_SENSITIVITIES),
        )
    can_draft = actor_can_read_drafts(ctx.actor)
    result: dict[UUID, _Hydrated] = {}
    for page in ctx.session.scalars(statement).all():
        has_visible_head = (can_draft and page.latest_draft_version_id is not None) or (
            page.lifecycle_status == "active"
            and page.current_active_version_id is not None
        )
        if has_visible_head:
            result[page.id] = _Hydrated(title=page.title, path=None)
    return result


def _visible_library_items(
    ctx: _HydrationContext,
    item_ids: set[UUID],
) -> dict[UUID, LibraryItem]:
    if not item_ids:
        return {}
    visible: dict[UUID, LibraryItem] = {}
    for item in ctx.session.scalars(
        select(LibraryItem)
        .where(LibraryItem.id.in_(item_ids))
        .where(LibraryItem.workspace_id == ctx.workspace_id)
    ).all():
        if not library_item_visible(item, ctx.actor):
            continue
        visible[item.id] = item
    return visible


def _hydrate_library_artifacts(
    ctx: _HydrationContext, ids: list[UUID]
) -> dict[UUID, _Hydrated]:
    """Artifacts hydrate as their parent Library item (title/path) — the
    §5.5 citations block names the SOURCE, gated by the item's trust and
    lifecycle exactly like a direct item neighbor."""
    result: dict[UUID, _Hydrated] = {}
    if not ids:
        return result
    rows = ctx.session.execute(
        select(
            LibraryArtifact,
            LibraryItemVersion.library_item_id,
            LibraryItemVersion,
        )
        .join(
            LibraryItemVersion,
            LibraryArtifact.library_item_version_id == LibraryItemVersion.id,
        )
        .where(LibraryArtifact.id.in_(ids))
    ).all()
    items = _visible_library_items(
        ctx, {item_id for _artifact, item_id, _version in rows}
    )
    canonical_versions = _canonical_library_versions(ctx, items.values())
    for artifact, item_id, version in rows:
        item = items.get(item_id)
        canonical = canonical_versions.get(item_id)
        if (
            canonical is None
            or version.id != canonical.id
            or not _artifact_belongs_to_selected_attempt(artifact, canonical)
        ):
            continue
        result[artifact.id] = _Hydrated(
            title=item.title or item.uri, path=library_concept_path(item.id)
        )
    return result


def _hydrate_library_item_versions(
    ctx: _HydrationContext, ids: list[UUID]
) -> dict[UUID, _Hydrated]:
    result: dict[UUID, _Hydrated] = {}
    if not ids:
        return result
    rows = ctx.session.execute(
        select(LibraryItemVersion, LibraryItemVersion.library_item_id).where(
            LibraryItemVersion.id.in_(ids)
        )
    ).all()
    items = _visible_library_items(ctx, {item_id for _version, item_id in rows})
    canonical_versions = _canonical_library_versions(ctx, items.values())
    for version, item_id in rows:
        item = items.get(item_id)
        canonical = canonical_versions.get(item_id)
        if canonical is None or version.id != canonical.id:
            continue
        result[version.id] = _Hydrated(
            title=item.title or item.uri, path=library_concept_path(item.id)
        )
    return result


def _canonical_library_versions(
    ctx: _HydrationContext,
    items: Iterable[LibraryItem],
) -> dict[UUID, LibraryItemVersion]:
    """Keep canonical resolution on a bounded-query path for graph batches."""

    item_rows = tuple(items)
    version_ids = {
        item.current_ready_version_id or item.current_version_id
        for item in item_rows
        if item.current_ready_version_id is not None
        or item.current_version_id is not None
    }
    if not version_ids:
        return {}
    # Retain strong references while the canonical helper resolves from the
    # identity map; SQLAlchemy's identity map may otherwise weak-reference rows.
    preloaded = {
        version.id: version
        for version in ctx.session.scalars(
            select(LibraryItemVersion).where(LibraryItemVersion.id.in_(version_ids))
        ).all()
    }
    canonical: dict[UUID, LibraryItemVersion] = {}
    for item in item_rows:
        candidate_id = item.current_ready_version_id or item.current_version_id
        if candidate_id not in preloaded:
            continue
        version = get_canonical_library_version(ctx.session, item)
        if version is not None:
            canonical[item.id] = version
    return canonical


def _artifact_belongs_to_selected_attempt(
    artifact: LibraryArtifact,
    version: LibraryItemVersion,
) -> bool:
    # A-1: LibraryArtifact.attempt_id is NOT NULL now (the legacy
    # nullable-attempt shape is retired) -- an artifact can never lack an
    # attempt, so "no attempt selected yet" means no artifact can match.
    selected_attempt_id = version.selected_conversion_attempt_id
    if selected_attempt_id is None:
        return False
    return artifact.attempt_id == selected_attempt_id


def _hydrate_tree_nodes(
    ctx: _HydrationContext, ids: list[UUID]
) -> dict[UUID, _Hydrated]:
    result: dict[UUID, _Hydrated] = {}
    if not ids:
        return result
    projection = ctx.tree_projection()
    for node_id in ids:
        node = projection.nodes.get(node_id)
        if node is None or not projection.is_visible(node_id):
            continue
        result[node.id] = _Hydrated(title=node.title, path=None)
    return result


# EntityKind -> batched (title/path resolver AND visibility predicate).
# Kinds without a hydrator are structurally unreturnable (§19.5.4-b).
HYDRATORS: dict[
    str,
    Callable[[_HydrationContext, list[UUID]], dict[UUID, _Hydrated]],
] = {
    EntityKind.CONTEXT_PAGE.value: _hydrate_context_pages,
    EntityKind.AGENT_PAGE.value: _hydrate_agent_pages,
    EntityKind.LIBRARY_ITEM.value: _hydrate_library_items,
    EntityKind.LIBRARY_ARTIFACT.value: _hydrate_library_artifacts,
    EntityKind.LIBRARY_ITEM_VERSION.value: _hydrate_library_item_versions,
    EntityKind.WORKSPACE_TREE_NODE.value: _hydrate_tree_nodes,
}


class GraphService:
    def ppr(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        seeds: list[tuple[str, UUID, float]],
        actor: ActorContext | None,
        top_k: int = 20,
    ) -> list[NeighborEdge]:
        from backend.services.graph_ppr import subgraph_ppr

        scores = subgraph_ppr(
            session,
            workspace_id,
            seeds,
            top_k=max(top_k * 4, top_k),
        )
        ctx = _HydrationContext(session, workspace_id, actor)
        by_kind: dict[str, list[UUID]] = {}
        for score in scores:
            if score.kind in HYDRATORS:
                by_kind.setdefault(score.kind, []).append(score.entity_id)
        hydrated: dict[tuple[str, UUID], _Hydrated] = {}
        for kind, ids in by_kind.items():
            for entity_id, info in HYDRATORS[kind](ctx, ids).items():
                hydrated[(kind, entity_id)] = info
        results: list[NeighborEdge] = []
        for score in scores:
            info = hydrated.get((score.kind, score.entity_id))
            if info is None:
                continue
            results.append(
                NeighborEdge(
                    kind=score.kind,
                    id=score.entity_id,
                    title=info.title,
                    path=info.path,
                    edge_kind="associative",
                    weight=score.score,
                    direction="out",
                )
            )
            if len(results) >= top_k:
                break
        return results

    def context_page_neighbors_panel(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        page_id: UUID,
        actor: ActorContext | None,
        include_mermaid: bool = False,
    ) -> GraphNeighborsPayload:
        """Return the bounded Context inspector graph projection.

        The seed is visibility-checked before looking up edges. This makes an
        unknown, cross-workspace, archived, or draft-only page uniformly
        unreadable instead of allowing its graph neighborhood to reveal it.
        """
        ctx = _HydrationContext(session, workspace_id, actor)
        seed = _hydrate_context_pages(ctx, [page_id]).get(page_id)
        if seed is None:
            raise ReadNotFoundError("context page not found")

        backlinks = self.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=page_id,
            actor=actor,
            edge_kinds=("wikilink",),
            direction="in",
            limit=10,
        )
        outbound = self.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=page_id,
            actor=actor,
            edge_kinds=("wikilink",),
            direction="out",
            limit=10,
        )
        citations = self.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=page_id,
            actor=actor,
            edge_kinds=("citation_page", "citation_source"),
            direction="out",
            limit=5,
        )
        co_access = self.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=page_id,
            actor=actor,
            edge_kinds=("co_access",),
            direction="both",
            limit=10,
        )
        payload = GraphNeighborsPayload(
            backlinks=[_neighbor_entry(edge) for edge in backlinks],
            outbound=[_neighbor_entry(edge) for edge in outbound],
            citations=[_neighbor_entry(edge) for edge in citations],
            co_access=[_neighbor_entry(edge) for edge in co_access],
        )
        if include_mermaid:
            payload.mermaid = self.render_context_page_mermaid(seed.title, payload)
        return payload

    @staticmethod
    def render_context_page_mermaid(
        seed_title: str,
        payload: GraphNeighborsPayload,
    ) -> str:
        lines = ["graph LR", f'  seed["{_mermaid_label(seed_title)}"]']
        entries = [
            *payload.backlinks,
            *payload.outbound,
            *payload.citations,
            *payload.co_access,
        ]
        seen: set[tuple[str, UUID, str, str]] = set()
        for index, entry in enumerate(entries):
            key = (entry.kind, entry.id, entry.edge_kind, entry.direction)
            if key in seen:
                continue
            seen.add(key)
            node_id = f"n{index}"
            lines.append(f'  {node_id}["{_mermaid_label(entry.title)}"]')
            label = _mermaid_label(entry.edge_kind)
            if entry.edge_kind == "co_access":
                lines.append(f"  seed ---|{label}| {node_id}")
            elif entry.direction == "in":
                lines.append(f"  {node_id} -.->|{label}| seed")
            else:
                lines.append(f"  seed -->|{label}| {node_id}")
            style = "usage" if entry.edge_kind == "co_access" else "linked"
            lines.append(f"  class {node_id} {style}")
        lines.extend(
            (
                "  classDef linked fill:#e8f1f5,stroke:#3f6878,color:#102a33",
                "  classDef usage fill:#f4ecd8,stroke:#8a6b21,color:#382b0d",
                "  classDef seed fill:#dcefe4,stroke:#34704b,color:#163522",
                "  class seed seed",
            )
        )
        return "\n".join(lines)

    def context_page_graph_context(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        page_id: UUID,
        actor: ActorContext | None,
        limit: int = 5,
    ) -> list[GraphNeighborEntry]:
        """Small graph context for an Ask evidence item.

        At most three inbound links, three outbound links, and two citations
        are considered. Entity-level deduplication then enforces the public
        response budget, so one document cannot occupy multiple slots merely
        because it has several edge types or directions.
        """
        contexts = self.context_pages_graph_context(
            session,
            workspace_id,
            page_ids=[page_id],
            actor=actor,
            limit=limit,
        )
        if page_id not in contexts:
            raise ReadNotFoundError("context page not found")
        return contexts[page_id]

    def context_pages_graph_context(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        page_ids: list[UUID],
        actor: ActorContext | None,
        limit: int = 5,
    ) -> dict[UUID, list[GraphNeighborEntry]]:
        """Batch Ask graph context for every visible seed in one bounded pass.

        Two window queries cap inbound/outbound candidates per seed before
        hydration. Hydrators then run once per entity kind, so a 50-turn Ask
        session does not turn into hundreds of edge and visibility queries.
        """
        unique_ids = list(dict.fromkeys(page_ids))
        if not unique_ids:
            return {}
        ctx = _HydrationContext(session, workspace_id, actor)
        visible = _hydrate_context_pages(ctx, unique_ids)
        visible_ids = list(visible)
        if not visible_ids:
            return {}

        inbound = _bounded_context_edge_rows(
            session,
            workspace_id,
            visible_ids,
            direction="in",
            edge_kinds=("wikilink",),
        )
        outbound = _bounded_context_edge_rows(
            session,
            workspace_id,
            visible_ids,
            direction="out",
            edge_kinds=("wikilink", "citation_page", "citation_source"),
        )
        rows = [*inbound, *outbound]
        by_kind: dict[str, list[UUID]] = {}
        for _seed_id, kind, entity_id, _edge_kind, _weight, _direction in rows:
            if kind in HYDRATORS:
                by_kind.setdefault(kind, []).append(entity_id)
        hydrated: dict[tuple[str, UUID], _Hydrated] = {}
        for kind, ids in by_kind.items():
            for entity_id, info in HYDRATORS[kind](ctx, ids).items():
                hydrated[(kind, entity_id)] = info

        groups: dict[UUID, dict[str, list[NeighborEdge]]] = {
            page_id: {"in": [], "out": [], "citations": []} for page_id in visible_ids
        }
        for seed_id, kind, entity_id, edge_kind, weight, direction in rows:
            info = hydrated.get((kind, entity_id))
            if info is None:
                continue
            edge = NeighborEdge(
                kind=kind,
                id=entity_id,
                title=info.title,
                path=info.path,
                edge_kind=edge_kind,
                weight=weight,
                direction=direction,
            )
            bucket = (
                "citations"
                if edge_kind in {"citation_page", "citation_source"}
                else direction
            )
            groups[seed_id][bucket].append(edge)

        result: dict[UUID, list[GraphNeighborEntry]] = {}
        for page_id, page_groups in groups.items():
            entries: list[GraphNeighborEntry] = []
            seen: set[tuple[str, UUID]] = set()
            ordered_groups = (
                page_groups["in"][:3],
                page_groups["out"][:3],
                page_groups["citations"][:2],
            )
            for edges in ordered_groups:
                for edge in edges:
                    key = (edge.kind, edge.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append(_neighbor_entry(edge))
                    if len(entries) >= limit:
                        break
                if len(entries) >= limit:
                    break
            result[page_id] = entries
        return result

    def neighbors(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        kind: str,
        entity_id: UUID,
        actor: ActorContext | None,
        edge_kinds: tuple[str, ...] | None = None,
        direction: str = "both",
        limit: int = 50,
    ) -> list[NeighborEdge]:
        """1-hop neighbors from the edge projection, weight-descending.

        Hub damping (Sprint-3 scope) = weight ordering + the hard limit;
        true damping is G3.
        """
        # Visibility is resolved after edge lookup. A single ordered, hard-
        # capped query per direction bounds a hostile high-degree hub (a
        # deliberate completeness-vs-DoS trade: visible neighbors ranked below
        # scan_budget higher-weight hidden edges are dropped) without OFFSET
        # re-sorts or snapshot skew. One hydration context is shared across
        # both directions so the tree projection loads at most once.
        scan_budget = min(512, max(128, limit * 8))
        ctx = _HydrationContext(session, workspace_id, actor)
        candidates: list[tuple[str, UUID, str, float, str, _Hydrated]] = []

        def collect_visible(statement, edge_dir: str):
            edges = session.scalars(
                statement.order_by(
                    EdgeProjection.weight.desc(), EdgeProjection.id.asc()
                ).limit(scan_budget)
            ).all()
            raw = [
                (
                    edge.target_kind if edge_dir == "out" else edge.source_kind,
                    edge.target_id if edge_dir == "out" else edge.source_id,
                    edge.edge_kind,
                    edge.weight,
                    edge_dir,
                )
                for edge in edges
            ]
            by_kind: dict[str, list[UUID]] = {}
            for neighbor_kind, neighbor_id, _, _, _ in raw:
                if neighbor_kind in HYDRATORS:
                    by_kind.setdefault(neighbor_kind, []).append(neighbor_id)
            hydrated: dict[tuple[str, UUID], _Hydrated] = {}
            for neighbor_kind, ids in by_kind.items():
                for neighbor_id, info in HYDRATORS[neighbor_kind](ctx, ids).items():
                    hydrated[(neighbor_kind, neighbor_id)] = info
            visible: list[tuple[str, UUID, str, float, str, _Hydrated]] = []
            for candidate in raw:
                info = hydrated.get((candidate[0], candidate[1]))
                if info is not None:
                    visible.append((*candidate, info))
            return visible

        if direction in ("out", "both"):
            statement = (
                select(EdgeProjection)
                .where(EdgeProjection.workspace_id == workspace_id)
                .where(EdgeProjection.source_kind == kind)
                .where(EdgeProjection.source_id == entity_id)
            )
            if edge_kinds:
                statement = statement.where(EdgeProjection.edge_kind.in_(edge_kinds))
            candidates.extend(collect_visible(statement, "out"))
        if direction in ("in", "both"):
            statement = (
                select(EdgeProjection)
                .where(EdgeProjection.workspace_id == workspace_id)
                .where(EdgeProjection.target_kind == kind)
                .where(EdgeProjection.target_id == entity_id)
            )
            if edge_kinds:
                statement = statement.where(EdgeProjection.edge_kind.in_(edge_kinds))
            candidates.extend(collect_visible(statement, "in"))

        results: list[NeighborEdge] = []
        seen: set[tuple[str, UUID, str]] = set()
        for neighbor_kind, neighbor_id, edge_kind, weight, edge_dir, info in sorted(
            candidates, key=lambda item: -item[3]
        ):
            dedupe_key = (neighbor_kind, neighbor_id, edge_kind)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            results.append(
                NeighborEdge(
                    kind=neighbor_kind,
                    id=neighbor_id,
                    title=info.title,
                    path=info.path,
                    edge_kind=edge_kind,
                    weight=weight,
                    direction=edge_dir,
                )
            )
            if len(results) >= limit:
                break
        return results

    def siblings(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        page_id: UUID,
        actor: ActorContext | None,
        limit: int = 10,
    ) -> list[NeighborEdge]:
        """Same-folder siblings via a 2-step walk over tree placement edges."""
        parents = self.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=page_id,
            actor=actor,
            edge_kinds=("tree",),
            direction="in",
            limit=1,
        )
        if not parents:
            return []
        folder = parents[0]
        siblings = self.neighbors(
            session,
            workspace_id,
            kind=EntityKind.WORKSPACE_TREE_NODE.value,
            entity_id=folder.id,
            actor=actor,
            edge_kinds=("tree",),
            direction="out",
            limit=limit + 1,
        )
        return [
            sibling
            for sibling in siblings
            if not (
                sibling.kind == EntityKind.CONTEXT_PAGE.value and sibling.id == page_id
            )
        ][:limit]


def _neighbor_entry(edge: NeighborEdge) -> GraphNeighborEntry:
    return GraphNeighborEntry(
        kind=edge.kind,
        id=edge.id,
        title=edge.title,
        path=edge.path,
        edge_kind=edge.edge_kind,
        direction=edge.direction,
    )


def _mermaid_label(value: str) -> str:
    return (value.replace("\r", " ").replace("\n", " ").replace('"', "#quot;"))[:160]


def _bounded_context_edge_rows(
    session: Session,
    workspace_id: UUID,
    page_ids: list[UUID],
    *,
    direction: str,
    edge_kinds: tuple[str, ...],
    scan_budget: int = 64,
) -> list[tuple[UUID, str, UUID, str, float, str]]:
    if direction == "out":
        seed_column = EdgeProjection.source_id
        neighbor_kind = EdgeProjection.target_kind
        neighbor_id = EdgeProjection.target_id
        kind_predicate = EdgeProjection.source_kind == EntityKind.CONTEXT_PAGE.value
    else:
        seed_column = EdgeProjection.target_id
        neighbor_kind = EdgeProjection.source_kind
        neighbor_id = EdgeProjection.source_id
        kind_predicate = EdgeProjection.target_kind == EntityKind.CONTEXT_PAGE.value
    bucket = case(
        (
            EdgeProjection.edge_kind.in_(("citation_page", "citation_source")),
            "citations",
        ),
        else_=direction,
    )
    ranked = (
        select(
            seed_column.label("seed_id"),
            neighbor_kind.label("neighbor_kind"),
            neighbor_id.label("neighbor_id"),
            EdgeProjection.edge_kind.label("edge_kind"),
            EdgeProjection.weight.label("weight"),
            func.row_number()
            .over(
                partition_by=(seed_column, bucket),
                order_by=(EdgeProjection.weight.desc(), EdgeProjection.id.asc()),
            )
            .label("edge_rank"),
        )
        .where(EdgeProjection.workspace_id == workspace_id)
        .where(kind_predicate)
        .where(seed_column.in_(page_ids))
        .where(EdgeProjection.edge_kind.in_(edge_kinds))
        .subquery()
    )
    rows = session.execute(
        select(
            ranked.c.seed_id,
            ranked.c.neighbor_kind,
            ranked.c.neighbor_id,
            ranked.c.edge_kind,
            ranked.c.weight,
        )
        .where(ranked.c.edge_rank <= scan_budget)
        .order_by(ranked.c.seed_id, ranked.c.edge_rank)
    ).all()
    return [(*row, direction) for row in rows]

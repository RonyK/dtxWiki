"""GraphService — knowledge-edge read primitives (design §19.5, G1).

Reads the ``edge_projection`` read model (the physical ``knowledge_edges``).
Every returned neighbor passes actor visibility (§3.3) through a per-kind
HYDRATORS registry: a kind without a hydrator is never returned, so adding an
edge kind cannot silently leak entities. PPR/subgraph/co-access are G3/N6.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Float,
    Integer,
    and_,
    bindparam,
    case,
    cast,
    func,
    literal,
    literal_column,
    or_,
    select,
    union_all,
)
from sqlalchemy.orm import Session, aliased

from backend.models.agent import AgentPage
from backend.models.context import ContextPage, EdgeProjection
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import (
    actor_can_read_drafts,
    actor_can_read_restricted_library,
    actor_can_read_sensitive_agent_pages,
)
from backend.repositories.context_visibility import visible_context_version_expression
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
from backend.services.graph_rank import (
    DENOMINATOR_KINDS,
    ROLE_ORDER,
    RankDenominators,
    candidate_order_key,
    effective_weight,
    kind_order_key,
    rank_score,
)
from backend.services.graph_semantics import EDGE_SEMANTICS
from backend.services.library_canonical import (
    LEGACY_READY_PROCESSING_STATUSES,
    get_canonical_library_version,
)
from backend.services.read_errors import ReadNotFoundError
from backend.services.tree_projection import TreeProjectionService

CONTEXT_GRAPH_RAW_CANDIDATES_PER_SEED = 1_024
VISIBLE_PROJECTION_RAW_SCAN_LIMIT = 1_024
VISIBLE_PROJECTION_PAGE_LIMIT = 8
GRAPH_TREE_HYDRATION_NODE_LIMIT = 1_024
GRAPH_TREE_HYDRATION_ANCESTOR_DEPTH_LIMIT = 64
GRAPH_TREE_HYDRATION_BATCH_SIZE = 256
# Rows the Local Graph reads per (seed, stored direction, relation kind),
# shared by the request's seeds. Past it a partition is a raw-weight sample
# and the scene says so (bounded_sample).
RANKED_PARTITION_ROW_BUDGET = 1_024
# Sources per rank-denominator statement; under the bound-parameter limits
# of SQLite (32,766) and pg8000 (32,767).
RANK_DENOMINATOR_BATCH_SIZE = 1_000
# Relations counted per source before a denominator saturates. Past it
# log2(2 + count) moves little, and a large folder or index page would
# otherwise cost a visibility check per relation on every request.
RANK_DENOMINATOR_ROW_CAP = 256
# Capped sources per statement: each is one LIMITed subquery.
RANK_DENOMINATOR_CAPPED_BATCH_SIZE = 100
# Grouped source chunks per denominator statement (each binds its ids twice).
_GROUPED_COUNTS_PER_STATEMENT = 4
# Built grouped-denominator statements, reused across requests: building
# one (two visibility predicates) costs several times what running it does.
# Keyed by workspace, the reader's visibility signature and chunk counts;
# the source ids are bound at execution. Oldest entries go first.
_DENOMINATOR_STATEMENTS: dict[tuple, list[tuple[Any, tuple[str, ...]]]] = {}
_DENOMINATOR_STATEMENT_LIMIT = 256
# The same for the ranked partition statements, by orientation and window.
_PARTITION_STATEMENTS: dict[tuple, Any] = {}
_TREE_CHAIN_PREDICATES: dict[tuple, Any] = {}
# The kinds whose rank divides by a count taken over the source page.
_PAGE_DENOMINATOR_KINDS = ("wikilink", "citation_page", "citation_source")


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
class VisibleProjectionEdge:
    """One current projection edge whose neighbor passed live actor ACLs.

    The database row UUID is intentionally absent: projection rebuilds delete
    and recreate rows, so it is not a stable transport identity.
    """

    source_kind: str
    source_id: UUID
    target_kind: str
    target_id: UUID
    edge_kind: str
    provenance: str
    weight: float
    occurred_at: datetime | None
    source_ref: dict[str, Any]
    direction: str
    neighbor_kind: str
    neighbor_id: UUID
    neighbor_title: str
    neighbor_path: str | None
    # co_access evidence; None for every other kind.
    support_session_count: int | None = None
    first_observed_at: datetime | None = None
    # Actor-visible relation rank (graph_rank); 0.0 on the raw-weight path.
    rank_score: float = 0.0


class VisibleProjectionEdges(list[VisibleProjectionEdge]):
    """List-compatible result carrying non-sensitive scan-bound metadata."""

    def __init__(
        self,
        values: list[VisibleProjectionEdge] | None = None,
        *,
        scan_capped: bool = False,
        raw_scanned: int = 0,
        pages_scanned: int = 0,
    ) -> None:
        super().__init__(values or [])
        self.scan_capped = scan_capped
        self.raw_scanned = raw_scanned
        self.pages_scanned = pages_scanned


def _hydration_window(edge_budget: int) -> int:
    """Rows a ranked queue hydrates before the budget could need more."""

    return edge_budget + 1


class NeighborEdges(list[NeighborEdge]):
    """Legacy list result with the projection scan's boundedness attached."""

    def __init__(
        self,
        values: list[NeighborEdge] | None = None,
        *,
        scan_capped: bool = False,
        result_capped: bool = False,
    ) -> None:
        super().__init__(values or [])
        self.scan_capped = scan_capped
        self.result_capped = result_capped


@dataclass(frozen=True)
class _Hydrated:
    title: str
    path: str | None


class GraphHydrationContext:
    """Request-scoped actor-visible entity hydration state.

    Tree endpoint visibility depends on an ancestor chain. Loading the full
    canonical tree here would let a bounded edge or observation scan perform
    unbounded workspace work, so requested tree ids and their ancestors are
    fetched by primary key under one total lookup and depth budget. Results,
    including misses, are cached across refill pages.
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
        self._tree_nodes: dict[UUID, WorkspaceTreeNode] = {}
        self._tree_missing: set[UUID] = set()
        self._tree_direct_visible: set[UUID] = set()
        self._tree_ids_looked_up = 0
        self._source_cap_events = 0

    @property
    def source_capped(self) -> bool:
        return self._source_cap_events > 0

    @property
    def source_cap_events(self) -> int:
        """Monotonic marker allowing each caller to attribute a new cap."""

        return self._source_cap_events

    def _mark_source_capped(self) -> None:
        self._source_cap_events += 1

    def _load_tree_ids(self, ids: list[UUID]) -> None:
        unknown = list(
            dict.fromkeys(
                node_id
                for node_id in ids
                if node_id not in self._tree_nodes and node_id not in self._tree_missing
            )
        )
        if not unknown:
            return

        remaining = GRAPH_TREE_HYDRATION_NODE_LIMIT - self._tree_ids_looked_up
        if remaining <= 0:
            self._mark_source_capped()
            return
        selected = unknown[:remaining]
        if len(selected) < len(unknown):
            self._mark_source_capped()

        service = TreeProjectionService()
        for offset in range(0, len(selected), GRAPH_TREE_HYDRATION_BATCH_SIZE):
            batch = selected[offset : offset + GRAPH_TREE_HYDRATION_BATCH_SIZE]
            rows = list(
                self.session.scalars(
                    select(WorkspaceTreeNode).where(
                        WorkspaceTreeNode.workspace_id == self.workspace_id,
                        WorkspaceTreeNode.visibility_status == "visible",
                        WorkspaceTreeNode.id.in_(batch),
                    )
                ).all()
            )
            self._tree_ids_looked_up += len(batch)
            found = {node.id for node in rows}
            self._tree_missing.update(set(batch) - found)
            self._tree_nodes.update({node.id: node for node in rows})
            self._tree_direct_visible.update(
                service._visible_ids(self.session, rows, self.actor)
            )

    def visible_tree_nodes(self, ids: list[UUID]) -> dict[UUID, WorkspaceTreeNode]:
        requested = list(dict.fromkeys(ids))
        self._load_tree_ids(requested)

        frontier = [
            node.parent_id
            for node_id in requested
            if (node := self._tree_nodes.get(node_id)) is not None
            and node.parent_id is not None
        ]
        for _depth in range(GRAPH_TREE_HYDRATION_ANCESTOR_DEPTH_LIMIT):
            frontier = list(dict.fromkeys(frontier))
            if not frontier:
                break
            self._load_tree_ids(frontier)
            frontier = [
                node.parent_id
                for node_id in frontier
                if (node := self._tree_nodes.get(node_id)) is not None
                and node.parent_id is not None
            ]
        else:
            if frontier:
                self._mark_source_capped()

        def chain_visible(node_id: UUID, seen: set[UUID]) -> bool:
            if node_id in seen or node_id not in self._tree_direct_visible:
                return False
            node = self._tree_nodes.get(node_id)
            if node is None:
                return False
            if node.parent_id is None:
                return True
            return chain_visible(node.parent_id, {*seen, node_id})

        return {
            node_id: node
            for node_id in requested
            if (node := self._tree_nodes.get(node_id)) is not None
            and chain_visible(node_id, set())
        }


def _hydrate_context_pages(
    ctx: GraphHydrationContext, ids: list[UUID]
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
    ctx: GraphHydrationContext, ids: list[UUID]
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
    ctx: GraphHydrationContext, ids: list[UUID]
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
    ctx: GraphHydrationContext,
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
    ctx: GraphHydrationContext, ids: list[UUID]
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
    ctx: GraphHydrationContext, ids: list[UUID]
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
    ctx: GraphHydrationContext,
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


def _canonical_library_version_condition():
    """SQL mirror of get_canonical_library_version.

    The ready pointer names the canonical version when it is set; only a
    Library item that has never been projected falls back to its legacy
    current pointer, and only once that version finished processing.
    """

    return or_(
        LibraryItem.current_ready_version_id == LibraryItemVersion.id,
        and_(
            LibraryItem.current_ready_version_id.is_(None),
            LibraryItem.current_version_id == LibraryItemVersion.id,
            LibraryItemVersion.processing_status.in_(
                sorted(LEGACY_READY_PROCESSING_STATUSES)
            ),
        ),
    )


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
    ctx: GraphHydrationContext, ids: list[UUID]
) -> dict[UUID, _Hydrated]:
    result: dict[UUID, _Hydrated] = {}
    if not ids:
        return result
    for node in ctx.visible_tree_nodes(ids).values():
        result[node.id] = _Hydrated(title=node.title, path=None)
    return result


# EntityKind -> batched (title/path resolver AND visibility predicate).
# Kinds without a hydrator are structurally unreturnable (§19.5.4-b).
HYDRATORS: dict[
    str,
    Callable[[GraphHydrationContext, list[UUID]], dict[UUID, _Hydrated]],
] = {
    EntityKind.CONTEXT_PAGE.value: _hydrate_context_pages,
    EntityKind.AGENT_PAGE.value: _hydrate_agent_pages,
    EntityKind.LIBRARY_ITEM.value: _hydrate_library_items,
    EntityKind.LIBRARY_ARTIFACT.value: _hydrate_library_artifacts,
    EntityKind.LIBRARY_ITEM_VERSION.value: _hydrate_library_item_versions,
    EntityKind.WORKSPACE_TREE_NODE.value: _hydrate_tree_nodes,
}


class GraphService:
    @staticmethod
    def hydration_context(
        session: Session,
        workspace_id: UUID,
        actor: ActorContext | None,
    ) -> GraphHydrationContext:
        return GraphHydrationContext(session, workspace_id, actor)

    @staticmethod
    def _resolve_hydration_context(
        session: Session,
        workspace_id: UUID,
        actor: ActorContext | None,
        hydration_context: GraphHydrationContext | None,
    ) -> GraphHydrationContext:
        if hydration_context is None:
            return GraphHydrationContext(session, workspace_id, actor)
        if (
            hydration_context.session is not session
            or hydration_context.workspace_id != workspace_id
            or hydration_context.actor != actor
        ):
            raise ValueError("hydration context does not match graph read scope")
        return hydration_context

    def hydrate_entities(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        entities: list[tuple[str, UUID]],
        actor: ActorContext | None,
        hydration_context: GraphHydrationContext | None = None,
    ) -> dict[tuple[str, UUID], _Hydrated]:
        """Resolve labels and live visibility for a bounded entity set.

        Kinds without an explicit hydrator fail closed. Historical callers use
        this same seam so a formerly-visible endpoint is removed immediately
        when its current ACL changes.
        """

        ctx = self._resolve_hydration_context(
            session, workspace_id, actor, hydration_context
        )
        by_kind: dict[str, list[UUID]] = {}
        for kind, entity_id in entities:
            if kind in HYDRATORS:
                by_kind.setdefault(kind, []).append(entity_id)
        hydrated: dict[tuple[str, UUID], _Hydrated] = {}
        for kind, ids in by_kind.items():
            for entity_id, info in HYDRATORS[kind](
                ctx, list(dict.fromkeys(ids))
            ).items():
                hydrated[(kind, entity_id)] = info
        return hydrated

    def visible_projection_edges(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        kind: str,
        entity_id: UUID,
        actor: ActorContext | None,
        edge_kinds: tuple[str, ...] | None = None,
        exclude_edge_kinds: tuple[str, ...] = (),
        neighbor_kinds: tuple[str, ...] | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        direction: str = "both",
        limit: int = 50,
        hydration_context: GraphHydrationContext | None = None,
    ) -> VisibleProjectionEdges:
        """Return actor-visible current edges under a finite raw-scan budget.

        Actor-equivalent SQL predicates remove supported hidden entity
        families before ordering and limiting. Families whose visibility is
        projection-derived are still checked by their canonical hydrator, but
        their keyset refill is bounded across both directions. ``scan_capped``
        tells callers when that fallback sample was insufficient.
        """

        if limit <= 0:
            return VisibleProjectionEdges()
        if neighbor_kinds is not None and not neighbor_kinds:
            return VisibleProjectionEdges()
        if direction not in {"in", "out", "both"}:
            raise ValueError("direction must be one of: in, out, both")

        excluded_kinds = set(exclude_edge_kinds)

        ctx = self._resolve_hydration_context(
            session, workspace_id, actor, hydration_context
        )
        source_cap_events_before = ctx.source_cap_events
        page_size = min(512, max(128, limit * 8))
        candidates: list[VisibleProjectionEdge] = []
        raw_scanned = 0
        pages_scanned = 0
        scan_capped = False

        def collect_visible(statement, edge_dir: str) -> list[VisibleProjectionEdge]:
            nonlocal pages_scanned, raw_scanned, scan_capped
            neighbor_kind_column = (
                EdgeProjection.target_kind
                if edge_dir == "out"
                else EdgeProjection.source_kind
            )
            neighbor_id_column = (
                EdgeProjection.target_id
                if edge_dir == "out"
                else EdgeProjection.source_id
            )
            visible: list[VisibleProjectionEdge] = []
            last_key: tuple[float, str, UUID, str] | None = None

            while len(visible) < limit:
                remaining_raw = VISIBLE_PROJECTION_RAW_SCAN_LIMIT - raw_scanned
                remaining_pages = VISIBLE_PROJECTION_PAGE_LIMIT - pages_scanned
                if remaining_raw <= 0 or remaining_pages <= 0:
                    scan_capped = True
                    break
                fetch_size = min(page_size, remaining_raw)
                page_statement = statement
                if last_key is not None:
                    last_weight, last_kind, last_id, last_edge_kind = last_key
                    page_statement = page_statement.where(
                        or_(
                            EdgeProjection.weight < last_weight,
                            and_(
                                EdgeProjection.weight == last_weight,
                                neighbor_kind_column > last_kind,
                            ),
                            and_(
                                EdgeProjection.weight == last_weight,
                                neighbor_kind_column == last_kind,
                                neighbor_id_column > last_id,
                            ),
                            and_(
                                EdgeProjection.weight == last_weight,
                                neighbor_kind_column == last_kind,
                                neighbor_id_column == last_id,
                                EdgeProjection.edge_kind > last_edge_kind,
                            ),
                        )
                    )
                edges = session.scalars(
                    page_statement.order_by(
                        EdgeProjection.weight.desc(),
                        neighbor_kind_column.asc(),
                        neighbor_id_column.asc(),
                        EdgeProjection.edge_kind.asc(),
                    ).limit(fetch_size)
                ).all()
                pages_scanned += 1
                raw_scanned += len(edges)
                if not edges:
                    break

                entities = [
                    (
                        edge.target_kind if edge_dir == "out" else edge.source_kind,
                        edge.target_id if edge_dir == "out" else edge.source_id,
                    )
                    for edge in edges
                ]
                by_kind: dict[str, list[UUID]] = {}
                for neighbor_kind, neighbor_id in entities:
                    if neighbor_kind in HYDRATORS:
                        by_kind.setdefault(neighbor_kind, []).append(neighbor_id)
                hydrated: dict[tuple[str, UUID], _Hydrated] = {}
                for neighbor_kind, ids in by_kind.items():
                    for neighbor_id, info in HYDRATORS[neighbor_kind](
                        ctx, list(dict.fromkeys(ids))
                    ).items():
                        hydrated[(neighbor_kind, neighbor_id)] = info

                for edge, (neighbor_kind, neighbor_id) in zip(
                    edges, entities, strict=True
                ):
                    info = hydrated.get((neighbor_kind, neighbor_id))
                    if info is None:
                        continue
                    visible.append(
                        VisibleProjectionEdge(
                            source_kind=edge.source_kind,
                            source_id=edge.source_id,
                            target_kind=edge.target_kind,
                            target_id=edge.target_id,
                            edge_kind=edge.edge_kind,
                            provenance=edge.provenance,
                            weight=edge.weight,
                            occurred_at=edge.occurred_at,
                            source_ref=dict(edge.source_ref or {}),
                            direction=edge_dir,
                            neighbor_kind=neighbor_kind,
                            neighbor_id=neighbor_id,
                            neighbor_title=info.title,
                            neighbor_path=info.path,
                            support_session_count=edge.support_session_count,
                            first_observed_at=edge.first_observed_at,
                        )
                    )
                    if len(visible) >= limit:
                        break

                last = edges[-1]
                last_key = (
                    last.weight,
                    last.target_kind if edge_dir == "out" else last.source_kind,
                    last.target_id if edge_dir == "out" else last.source_id,
                    last.edge_kind,
                )
                if len(edges) < fetch_size:
                    break
                if len(visible) < limit and (
                    raw_scanned >= VISIBLE_PROJECTION_RAW_SCAN_LIMIT
                    or pages_scanned >= VISIBLE_PROJECTION_PAGE_LIMIT
                ):
                    scan_capped = True
                    break
            return visible

        if direction in {"out", "both"}:
            statement = select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.source_kind == kind,
                EdgeProjection.source_id == entity_id,
            )
            if edge_kinds:
                statement = statement.where(EdgeProjection.edge_kind.in_(edge_kinds))
            if excluded_kinds:
                statement = statement.where(
                    EdgeProjection.edge_kind.notin_(excluded_kinds)
                )
            if neighbor_kinds is not None:
                statement = statement.where(
                    EdgeProjection.target_kind.in_(neighbor_kinds)
                )
            statement = statement.where(
                EdgeProjection.target_kind.in_(tuple(HYDRATORS)),
                graph_neighbor_visibility_predicate(
                    EdgeProjection.target_kind,
                    EdgeProjection.target_id,
                    workspace_id=workspace_id,
                    actor=actor,
                    allow_tree_fallback=True,
                ),
            )
            if occurred_from is not None:
                statement = statement.where(EdgeProjection.occurred_at >= occurred_from)
            if occurred_to is not None:
                statement = statement.where(EdgeProjection.occurred_at <= occurred_to)
            candidates.extend(collect_visible(statement, "out"))
        if direction in {"in", "both"}:
            statement = select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.target_kind == kind,
                EdgeProjection.target_id == entity_id,
            )
            if edge_kinds:
                statement = statement.where(EdgeProjection.edge_kind.in_(edge_kinds))
            if excluded_kinds:
                statement = statement.where(
                    EdgeProjection.edge_kind.notin_(excluded_kinds)
                )
            if neighbor_kinds is not None:
                statement = statement.where(
                    EdgeProjection.source_kind.in_(neighbor_kinds)
                )
            statement = statement.where(
                EdgeProjection.source_kind.in_(tuple(HYDRATORS)),
                graph_neighbor_visibility_predicate(
                    EdgeProjection.source_kind,
                    EdgeProjection.source_id,
                    workspace_id=workspace_id,
                    actor=actor,
                    allow_tree_fallback=True,
                ),
            )
            if occurred_from is not None:
                statement = statement.where(EdgeProjection.occurred_at >= occurred_from)
            if occurred_to is not None:
                statement = statement.where(EdgeProjection.occurred_at <= occurred_to)
            candidates.extend(collect_visible(statement, "in"))

        ordered = sorted(
            candidates,
            key=lambda edge: (
                -edge.weight,
                edge.source_kind,
                str(edge.source_id),
                edge.target_kind,
                str(edge.target_id),
                edge.edge_kind,
                0 if edge.direction == "out" else 1,
            ),
        )
        unique: list[VisibleProjectionEdge] = []
        seen: set[tuple[str, UUID, str, UUID, str]] = set()
        for edge in ordered:
            identity = (
                edge.source_kind,
                edge.source_id,
                edge.target_kind,
                edge.target_id,
                edge.edge_kind,
            )
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(edge)
            if len(unique) >= limit:
                break
        return VisibleProjectionEdges(
            unique,
            scan_capped=(
                scan_capped or ctx.source_cap_events > source_cap_events_before
            ),
            raw_scanned=raw_scanned,
            pages_scanned=pages_scanned,
        )

    def relation_rank_denominators(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        actor: ActorContext | None,
        sources: tuple[tuple[str, UUID], ...],
    ) -> dict[tuple[str, UUID], RankDenominators]:
        """Count what each source spreads itself over, among what ``actor`` sees.

        For a page: its distinct visible wikilink targets and the citation
        instances it writes to visible targets. For a folder: its visible
        children, folders included. The visibility predicate admits exactly
        what the hydrators admit for every document family, so a relation the
        reader cannot see never enters a count. Counts ignore every view
        filter, so a filter cannot change the rank of an edge it leaves
        visible. A source with no counted row reads as zeros.

        Every count saturates at ``RANK_DENOMINATOR_ROW_CAP``: an Inbox folder
        or an index page can hold tens of thousands of relations, and each one
        costs a visibility check. A source whose stored relations fit under
        the cap is counted exactly in one grouped statement; a larger one is
        read only until the cap's worth of visible rows, so its count is
        ``min(visible, cap)`` either way and still ignores hidden rows. The
        stored total only picks the cheaper statement.
        """

        pages = sorted(
            {source_id for kind, source_id in sources if kind == "context_page"},
            key=str,
        )
        folders = sorted(
            {
                source_id
                for kind, source_id in sources
                if kind == EntityKind.WORKSPACE_TREE_NODE.value
            },
            key=str,
        )
        result: dict[tuple[str, UUID], RankDenominators] = {}
        tree_kind = EntityKind.WORKSPACE_TREE_NODE.value
        cap = RANK_DENOMINATOR_ROW_CAP
        batch = RANK_DENOMINATOR_BATCH_SIZE
        parameters = {
            f"{family}_ids_{index}": ids[offset : offset + batch]
            for family, ids in (("page", pages), ("folder", folders))
            for index, offset in enumerate(range(0, len(ids), batch))
        }
        for statement, names in self._grouped_count_statements(
            workspace_id,
            actor,
            page_chunks=-(-len(pages) // batch),
            folder_chunks=-(-len(folders) // batch),
        ):
            for source_kind, source_id, links, citations, children in session.execute(
                statement,
                {"row_cap": cap, **{name: parameters[name] for name in names}},
            ).all():
                result[(source_kind, source_id)] = RankDenominators(
                    wikilink_out_degree=int(links or 0),
                    citation_total=min(float(citations or 0.0), cap),
                    tree_children=int(children or 0),
                )
        if all(
            (kind, source_id) in result
            for kind, ids in (("context_page", pages), (tree_kind, folders))
            for source_id in ids
        ):
            return result
        visible_target, visible_child = _denominator_visibility(workspace_id, actor)
        families = (
            ("context_page", pages, _PAGE_DENOMINATOR_KINDS, visible_target),
            (tree_kind, folders, ("tree",), visible_child),
        )
        # A source the grouped count left out has more stored relations than
        # the cap, or none the reader can see; only the first are read again,
        # up to the cap's worth of visible rows.
        for kind, ids, edge_kinds, visible in families:
            missing = [
                source_id for source_id in ids if (kind, source_id) not in result
            ]
            if not missing:
                continue
            stored = self._stored_relation_counts(
                session,
                workspace_id,
                source_kind=kind,
                source_ids=missing,
                edge_kinds=edge_kinds,
            )
            large = [
                source_id for source_id in missing if stored.get(source_id, 0) > cap
            ]
            counted_families = (
                (("tree", ("tree",), False),)
                if kind == tree_kind
                else (
                    ("wikilink", ("wikilink",), False),
                    ("citation", ("citation_page", "citation_source"), True),
                )
            )
            for offset in range(0, len(large), RANK_DENOMINATOR_CAPPED_BATCH_SIZE):
                chunk = large[offset : offset + RANK_DENOMINATOR_CAPPED_BATCH_SIZE]
                counted: dict[UUID, dict[str, float]] = defaultdict(dict)
                for source_id, family, value in session.execute(
                    union_all(
                        *(
                            self._capped_count(
                                workspace_id,
                                source_kind=kind,
                                source_id=source_id,
                                edge_kinds=family_kinds,
                                visible=visible,
                                family=family,
                                summed=summed,
                            )
                            for source_id in chunk
                            for family, family_kinds, summed in counted_families
                        )
                    ).execution_options(graph_rank_phase="rank_denominators")
                ).all():
                    counted[source_id][family] = float(value or 0.0)
                for source_id in chunk:
                    values = counted[source_id]
                    result[(kind, source_id)] = RankDenominators(
                        wikilink_out_degree=int(values.get("wikilink", 0)),
                        citation_total=min(values.get("citation", 0.0), cap),
                        tree_children=int(values.get("tree", 0)),
                    )
        return result

    @staticmethod
    def _grouped_count_statements(
        workspace_id: UUID,
        actor: ActorContext | None,
        *,
        page_chunks: int,
        folder_chunks: int,
    ) -> list[tuple[Any, tuple[str, ...]]]:
        """The grouped denominator statements for this reader, built once."""

        key = (
            workspace_id,
            graph_visibility_signature(actor),
            page_chunks,
            folder_chunks,
        )
        cached = _DENOMINATOR_STATEMENTS.get(key)
        if cached is not None:
            return cached
        visible_target, visible_child = _denominator_visibility(workspace_id, actor)
        arms = [
            (
                GraphService._grouped_count(
                    workspace_id,
                    source_kind=kind,
                    source_ids=bindparam(
                        f"{family}_ids_{index}",
                        expanding=True,
                        type_=EdgeProjection.source_id.type,
                    ),
                    edge_kinds=edge_kinds,
                    visible=visible,
                ),
                f"{family}_ids_{index}",
            )
            for family, kind, chunks, edge_kinds, visible in (
                (
                    "page",
                    "context_page",
                    page_chunks,
                    _PAGE_DENOMINATOR_KINDS,
                    visible_target,
                ),
                (
                    "folder",
                    EntityKind.WORKSPACE_TREE_NODE.value,
                    folder_chunks,
                    ("tree",),
                    visible_child,
                ),
            )
            for index in range(chunks)
        ]
        statements = [
            (
                union_all(
                    *(
                        arm
                        for arm, _name in arms[
                            offset : offset + _GROUPED_COUNTS_PER_STATEMENT
                        ]
                    )
                ).execution_options(graph_rank_phase="rank_denominators"),
                tuple(
                    name
                    for _arm, name in arms[
                        offset : offset + _GROUPED_COUNTS_PER_STATEMENT
                    ]
                ),
            )
            for offset in range(0, len(arms), _GROUPED_COUNTS_PER_STATEMENT)
        ]
        if len(_DENOMINATOR_STATEMENTS) >= _DENOMINATOR_STATEMENT_LIMIT:
            _DENOMINATOR_STATEMENTS.pop(next(iter(_DENOMINATOR_STATEMENTS)), None)
        _DENOMINATOR_STATEMENTS[key] = statements
        return statements

    @staticmethod
    def _grouped_count(
        workspace_id: UUID,
        *,
        source_kind: str,
        source_ids: Any,
        edge_kinds: tuple[str, ...],
        visible: Any,
    ):
        """Visible relations of each source whose stored ones fit the cap.

        The inner, index-only count keeps a large source out of the grouped
        read, where it would cost a visibility check per stored relation.
        """

        stored = aliased(EdgeProjection)
        fits = (
            select(stored.source_id)
            .where(
                stored.workspace_id == workspace_id,
                stored.source_kind == source_kind,
                stored.source_id.in_(source_ids),
                stored.edge_kind.in_(edge_kinds),
            )
            .group_by(stored.source_id)
            # Bound at execution, so a reused statement follows the cap.
            .having(func.count() <= bindparam("row_cap", type_=Integer))
        )
        return (
            select(
                EdgeProjection.source_kind,
                EdgeProjection.source_id,
                func.count(
                    case((EdgeProjection.edge_kind == "wikilink", literal_column("1")))
                ).label("links"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                EdgeProjection.edge_kind.in_(
                                    ("citation_page", "citation_source")
                                ),
                                EdgeProjection.weight,
                            )
                        )
                    ),
                    cast(literal_column("0.0"), Float),
                ).label("citations"),
                func.count(
                    case((EdgeProjection.edge_kind == "tree", literal_column("1")))
                ).label("children"),
            )
            .where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.source_kind == source_kind,
                EdgeProjection.source_id.in_(fits),
                EdgeProjection.edge_kind.in_(edge_kinds),
                visible,
            )
            .group_by(EdgeProjection.source_kind, EdgeProjection.source_id)
        )

    @staticmethod
    def _stored_relation_counts(
        session: Session,
        workspace_id: UUID,
        *,
        source_kind: str,
        source_ids: list[UUID],
        edge_kinds: tuple[str, ...],
    ) -> dict[UUID, int]:
        """Stored relations per source, hidden or not, from the index alone.

        Only picks the statement that counts a source; never a count itself.
        """

        counts: dict[UUID, int] = {}
        for offset in range(0, len(source_ids), RANK_DENOMINATOR_BATCH_SIZE):
            chunk = source_ids[offset : offset + RANK_DENOMINATOR_BATCH_SIZE]
            counts.update(
                session.execute(
                    select(EdgeProjection.source_id, func.count())
                    .where(
                        EdgeProjection.workspace_id == workspace_id,
                        EdgeProjection.source_kind == source_kind,
                        EdgeProjection.source_id.in_(chunk),
                        EdgeProjection.edge_kind.in_(edge_kinds),
                    )
                    .group_by(EdgeProjection.source_id)
                    .execution_options(graph_rank_phase="rank_denominators")
                )
                .tuples()
                .all()
            )
        return counts

    @staticmethod
    def _capped_count(
        workspace_id: UUID,
        *,
        source_kind: str,
        source_id: UUID,
        edge_kinds: tuple[str, ...],
        visible: Any,
        family: str,
        summed: bool,
    ):
        """One source's visible relations, read only up to the row cap.

        ``summed`` adds the rows' weights (citation instances) instead of
        counting them, over the same capped rows in a fixed order.
        """

        rows = (
            select(EdgeProjection.weight.label("weight"))
            .where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.source_kind == source_kind,
                EdgeProjection.source_id == source_id,
                EdgeProjection.edge_kind.in_(edge_kinds),
                visible,
            )
            .order_by(EdgeProjection.target_kind, EdgeProjection.target_id)
            .limit(RANK_DENOMINATOR_ROW_CAP)
            .subquery()
        )
        value = (
            func.coalesce(func.sum(rows.c.weight), literal_column("0.0"))
            if summed
            else func.count()
        )
        return select(
            literal(source_id, EdgeProjection.source_id.type).label("source_id"),
            literal(family).label("family"),
            value.label("value"),
        ).select_from(rows)

    def ranked_projection_edges(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        seeds: tuple[tuple[str, UUID], ...],
        actor: ActorContext | None,
        edge_kinds: tuple[str, ...],
        neighbor_kinds: tuple[str, ...],
        occurred_from: datetime | None,
        occurred_to: datetime | None,
        direction: str,
        edge_budget: int,
        now: datetime,
        hydration_context: GraphHydrationContext,
        min_partition_cap: int = 1,
    ) -> VisibleProjectionEdges:
        """Every relation kind around the seeds, each ranked for this reader.

        The Local Graph's collector. Unlike ``visible_projection_edges`` it
        does not sort by raw weight, whose unit differs per kind: it reads
        each (seed, stored direction, kind) partition on its own, up to
        ``max(min_partition_cap, RANKED_PARTITION_ROW_BUDGET // len(seeds))``
        rows, so a flood of one kind cannot push another out, and ranks every
        row with graph_rank from actor-visible denominators. Undirected kinds
        are read from both
        stored orientations whatever ``direction`` says: a pair is stored
        once, in UUID order.

        ``min_partition_cap`` is an optional keyword defaulting to 1, which
        preserves the original collector's allocation. The depth-two frontier
        uses 8 so a large first-hop frontier can still discover neighbors;
        increasing this floor does not relax actor or workspace visibility.

        Only the first ``edge_budget + 1`` rows of each (seed, kind, role)
        queue in rank order are hydrated, plus rows whose neighbour is a seed;
        a queue whose window loses rows to hydration is hydrated in full. A
        budget that walks each queue in this order (the Local Graph's does)
        can admit no node-costing edge past that window, so the scene is the
        one full hydration would give. Nothing is truncated here;
        ``scan_capped`` reports a partition read past its budget or the
        request-wide tree lookup cap.
        """

        if direction not in {"in", "out", "both"}:
            raise ValueError("direction must be one of: in, out, both")
        seed_order = list(dict.fromkeys(seeds))
        hydratable = tuple(sorted(set(HYDRATORS) & set(neighbor_kinds)))
        if not seed_order or not edge_kinds or not hydratable:
            return VisibleProjectionEdges()
        ctx = self._resolve_hydration_context(
            session, workspace_id, actor, hydration_context
        )
        source_cap_events_before = ctx.source_cap_events
        cap = max(min_partition_cap, RANKED_PARTITION_ROW_BUDGET // len(seed_order))
        undirected = [kind for kind in edge_kinds if not EDGE_SEMANTICS[kind].directed]
        directed = [kind for kind in edge_kinds if EDGE_SEMANTICS[kind].directed]
        orientations = {
            "out": [*(directed if direction in {"out", "both"} else ()), *undirected],
            "in": [*(directed if direction in {"in", "both"} else ()), *undirected],
        }

        rows: list[tuple[str, tuple[str, UUID], Any]] = []
        overflowed: set[tuple[tuple[str, UUID], str, str]] = set()
        raw_scanned = 0
        statements = 0
        seed_kinds = list(dict.fromkeys(kind for kind, _seed_id in seed_order))
        for orientation, kinds in orientations.items():
            if not kinds:
                continue
            for seed_kind in seed_kinds:
                seed_ids = [
                    seed_id for kind, seed_id in seed_order if kind == seed_kind
                ]
                fetched = self._ranked_partition_rows(
                    session,
                    workspace_id,
                    actor=actor,
                    orientation=orientation,
                    seed_kind=seed_kind,
                    seed_ids=seed_ids,
                    edge_kinds=tuple(sorted(kinds)),
                    neighbor_kinds=hydratable,
                    occurred_from=occurred_from,
                    occurred_to=occurred_to,
                    cap=cap,
                )
                statements += 1
                raw_scanned += len(fetched)
                for row in fetched:
                    anchor = (
                        (row.source_kind, row.source_id)
                        if orientation == "out"
                        else (row.target_kind, row.target_id)
                    )
                    if row.rn > cap:
                        overflowed.add((anchor, orientation, row.edge_kind))
                        continue
                    rows.append((orientation, anchor, row))

        # One copy per anchor and identity; a self-loop keeps its out copy.
        unique: dict[tuple, tuple[str, tuple[str, UUID], Any]] = {}
        for orientation, anchor, row in rows:
            identity = (
                anchor,
                row.source_kind,
                row.source_id,
                row.target_kind,
                row.target_id,
                row.edge_kind,
            )
            if identity not in unique:
                unique[identity] = (orientation, anchor, row)

        denominators = self.relation_rank_denominators(
            session,
            workspace_id,
            actor=actor,
            sources=tuple(
                sorted(
                    {
                        (row.source_kind, row.source_id)
                        for _orientation, _anchor, row in unique.values()
                        if row.edge_kind in DENOMINATOR_KINDS
                    },
                    key=lambda ref: (ref[0], str(ref[1])),
                )
            ),
        )

        seed_set = set(seed_order)
        queues: dict[tuple, list[tuple[tuple, str, tuple[str, UUID], Any]]] = (
            defaultdict(list)
        )
        for orientation, anchor, row in unique.values():
            neighbor = (
                (row.target_kind, row.target_id)
                if orientation == "out"
                else (row.source_kind, row.source_id)
            )
            rank = rank_score(
                row.edge_kind,
                effective_weight(
                    row.edge_kind,
                    float(row.weight),
                    denominators.get((row.source_kind, row.source_id)),
                ),
                row.occurred_at,
                now,
            )
            role = (
                "undirected"
                if not EDGE_SEMANTICS[row.edge_kind].directed
                else orientation
            )
            order = candidate_order_key(rank, row.occurred_at, *neighbor)
            queues[(anchor, row.edge_kind, role)].append(
                (order, orientation, neighbor, (rank, row))
            )
        for queue in queues.values():
            queue.sort(key=lambda item: item[0])

        window = _hydration_window(edge_budget)
        hydrated: dict[tuple[str, UUID], _Hydrated] = {}
        attempted: set[tuple[str, UUID]] = set()

        def hydrate(neighbors: Iterable[tuple[str, UUID]]) -> None:
            by_kind: dict[str, list[UUID]] = defaultdict(list)
            for neighbor in neighbors:
                if neighbor in attempted:
                    continue
                attempted.add(neighbor)
                by_kind[neighbor[0]].append(neighbor[1])
            for kind, ids in by_kind.items():
                for entity_id, info in HYDRATORS[kind](ctx, ids).items():
                    hydrated[(kind, entity_id)] = info

        first_round: list[tuple[str, UUID]] = []
        for queue in queues.values():
            taken = 0
            for _order, _orientation, neighbor, _ranked in queue:
                if neighbor in seed_set:
                    first_round.append(neighbor)
                elif taken < window:
                    first_round.append(neighbor)
                    taken += 1
        hydrate(first_round)
        # The budget walks a queue in order, so what counts is the hydrated
        # prefix: rows past the first unattempted one do not fill the window,
        # even when another queue hydrated their neighbour.
        second_round: list[tuple[str, UUID]] = []
        for queue in queues.values():
            visible_prefix = 0
            for _order, _orientation, neighbor, _ranked in queue:
                if neighbor in seed_set:
                    continue
                if neighbor not in attempted:
                    second_round.extend(
                        pending
                        for _o, _r, pending, _k in queue
                        if pending not in attempted
                    )
                    break
                if neighbor in hydrated:
                    visible_prefix += 1
                    if visible_prefix >= window:
                        break
        hydrate(second_round)

        seed_position = {seed: index for index, seed in enumerate(seed_order)}
        ordered_queues = sorted(
            queues.items(),
            key=lambda item: (
                seed_position[item[0][0]],
                kind_order_key(item[0][1]),
                ROLE_ORDER.index(item[0][2]),
            ),
        )
        result: list[VisibleProjectionEdge] = []
        for _key, queue in ordered_queues:
            for _order, orientation, neighbor, (rank, row) in queue:
                info = hydrated.get(neighbor)
                if info is None:
                    continue
                result.append(
                    VisibleProjectionEdge(
                        source_kind=row.source_kind,
                        source_id=row.source_id,
                        target_kind=row.target_kind,
                        target_id=row.target_id,
                        edge_kind=row.edge_kind,
                        provenance=row.provenance,
                        weight=float(row.weight),
                        occurred_at=row.occurred_at,
                        source_ref=dict(row.source_ref or {}),
                        direction=orientation,
                        neighbor_kind=neighbor[0],
                        neighbor_id=neighbor[1],
                        neighbor_title=info.title,
                        neighbor_path=info.path,
                        support_session_count=row.support_session_count,
                        first_observed_at=row.first_observed_at,
                        rank_score=rank,
                    )
                )
        return VisibleProjectionEdges(
            result,
            scan_capped=(
                bool(overflowed) or ctx.source_cap_events > source_cap_events_before
            ),
            raw_scanned=raw_scanned,
            pages_scanned=statements,
        )

    @staticmethod
    def _ranked_partition_rows(
        session: Session,
        workspace_id: UUID,
        *,
        actor: ActorContext | None,
        orientation: str,
        seed_kind: str,
        seed_ids: list[UUID],
        edge_kinds: tuple[str, ...],
        neighbor_kinds: tuple[str, ...],
        occurred_from: datetime | None,
        occurred_to: datetime | None,
        cap: int,
    ) -> list[Any]:
        """One stored orientation for every seed, ``cap + 1`` rows per partition.

        The extra row only proves the partition overflowed. The raw-weight
        order with identity tie-breaks decides nothing but which rows an
        overflowing partition keeps, and survives rebuilds that recreate row
        ids.
        """

        if orientation == "out":
            anchor_kind, anchor_id = (
                EdgeProjection.source_kind,
                EdgeProjection.source_id,
            )
            neighbor_kind, neighbor_id = (
                EdgeProjection.target_kind,
                EdgeProjection.target_id,
            )
        else:
            anchor_kind, anchor_id = (
                EdgeProjection.target_kind,
                EdgeProjection.target_id,
            )
            neighbor_kind, neighbor_id = (
                EdgeProjection.source_kind,
                EdgeProjection.source_id,
            )
        position = (
            func.row_number()
            .over(
                partition_by=(anchor_id, EdgeProjection.edge_kind),
                order_by=(
                    EdgeProjection.weight.desc(),
                    neighbor_kind.asc(),
                    neighbor_id.asc(),
                ),
            )
            .label("rn")
        )
        # Built once per reader and shape and reused: the visibility
        # predicate costs more to build than the statement costs to run.
        key = (
            workspace_id,
            graph_visibility_signature(actor),
            orientation,
            occurred_from is not None,
            occurred_to is not None,
        )
        query = _PARTITION_STATEMENTS.get(key)
        if query is None:
            statement = select(
                EdgeProjection.source_kind,
                EdgeProjection.source_id,
                EdgeProjection.target_kind,
                EdgeProjection.target_id,
                EdgeProjection.edge_kind,
                EdgeProjection.provenance,
                EdgeProjection.weight,
                EdgeProjection.occurred_at,
                EdgeProjection.source_ref,
                EdgeProjection.support_session_count,
                EdgeProjection.first_observed_at,
                position,
            ).where(
                EdgeProjection.workspace_id == workspace_id,
                anchor_kind == bindparam("seed_kind"),
                anchor_id.in_(
                    bindparam(
                        "seed_ids", expanding=True, type_=EdgeProjection.source_id.type
                    )
                ),
                EdgeProjection.edge_kind.in_(bindparam("edge_kinds", expanding=True)),
                neighbor_kind.in_(bindparam("neighbor_kinds", expanding=True)),
                graph_neighbor_visibility_predicate(
                    neighbor_kind,
                    neighbor_id,
                    workspace_id=workspace_id,
                    actor=actor,
                    allow_tree_fallback=True,
                ),
            )
            if occurred_from is not None:
                statement = statement.where(
                    EdgeProjection.occurred_at
                    >= bindparam("occurred_from", type_=EdgeProjection.occurred_at.type)
                )
            if occurred_to is not None:
                statement = statement.where(
                    EdgeProjection.occurred_at
                    <= bindparam("occurred_to", type_=EdgeProjection.occurred_at.type)
                )
            ranked = statement.subquery()
            anchor_column = (
                ranked.c.source_id if orientation == "out" else ranked.c.target_id
            )
            query = (
                select(ranked)
                .where(ranked.c.rn <= bindparam("row_limit", type_=Integer))
                .order_by(anchor_column, ranked.c.edge_kind, ranked.c.rn)
                .execution_options(graph_rank_phase="ranked_candidates")
            )
            if len(_PARTITION_STATEMENTS) >= _DENOMINATOR_STATEMENT_LIMIT:
                _PARTITION_STATEMENTS.pop(next(iter(_PARTITION_STATEMENTS)), None)
            _PARTITION_STATEMENTS[key] = query
        parameters: dict[str, Any] = {
            "seed_kind": seed_kind,
            "seed_ids": list(seed_ids),
            "edge_kinds": list(edge_kinds),
            "neighbor_kinds": list(neighbor_kinds),
            "row_limit": cap + 1,
        }
        if occurred_from is not None:
            parameters["occurred_from"] = occurred_from
        if occurred_to is not None:
            parameters["occurred_to"] = occurred_to
        return list(session.execute(query, parameters).all())

    def ppr(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        seeds: list[tuple[str, UUID, float]],
        actor: ActorContext | None,
        top_k: int = 20,
    ) -> list[NeighborEdge]:
        """Rank only the topology this actor can read.

        The legacy implementation ranked an actor-independent cached subgraph
        and hydrated only the winners. Hidden bridges therefore changed the
        order and score of visible results. This bounded two-hop walk applies
        live hydration before admitting each edge, then computes PageRank over
        that actor-visible graph. Usage-derived co-access is omitted because
        an associative search request has no explicit privacy opt-in.
        """

        if top_k <= 0:
            return []
        seed_weights: defaultdict[tuple[str, UUID], float] = defaultdict(float)
        for seed_kind, seed_id, weight in seeds:
            seed_weights[(seed_kind, seed_id)] += max(float(weight), 0.0)
        if not seed_weights:
            return []

        hydrated_seeds = self.hydrate_entities(
            session,
            workspace_id,
            entities=list(seed_weights),
            actor=actor,
        )
        seed_weights = defaultdict(
            float,
            {
                ref: weight
                for ref, weight in seed_weights.items()
                if ref in hydrated_seeds
            },
        )
        if not seed_weights:
            return []

        node_budget = max(len(seed_weights), min(500, max(top_k * 8, 64)))
        nodes = set(seed_weights)
        frontier = set(seed_weights)
        selected: dict[tuple[str, UUID, str, UUID, str], VisibleProjectionEdge] = {}
        provenance_priority = {"curated": 3, "derived": 2, "usage": 1}

        for _depth in range(2):
            if not frontier or len(nodes) >= node_budget:
                break
            edge_candidates: dict[
                tuple[str, UUID, str, UUID, str], VisibleProjectionEdge
            ] = {}
            for frontier_kind, frontier_id in sorted(
                frontier, key=lambda ref: (ref[0], str(ref[1]))
            ):
                for edge in self.visible_projection_edges(
                    session,
                    workspace_id,
                    kind=frontier_kind,
                    entity_id=frontier_id,
                    actor=actor,
                    exclude_edge_kinds=("co_access",),
                    direction="both",
                    limit=node_budget + 1,
                ):
                    identity = (
                        edge.source_kind,
                        edge.source_id,
                        edge.target_kind,
                        edge.target_id,
                        edge.edge_kind,
                    )
                    edge_candidates.setdefault(identity, edge)

            next_frontier: set[tuple[str, UUID]] = set()
            for identity, edge in sorted(
                edge_candidates.items(),
                key=lambda item: (
                    -provenance_priority.get(item[1].provenance, 0),
                    -item[1].weight,
                    item[0][0],
                    str(item[0][1]),
                    item[0][2],
                    str(item[0][3]),
                    item[0][4],
                ),
            ):
                if identity in selected:
                    continue
                source = (edge.source_kind, edge.source_id)
                target = (edge.target_kind, edge.target_id)
                new_nodes = {source, target} - nodes
                if len(nodes) + len(new_nodes) > node_budget:
                    continue
                selected[identity] = edge
                nodes.update(new_nodes)
                next_frontier.update(new_nodes)
            frontier = next_frontier

        adjacency: dict[tuple[str, UUID], dict[tuple[str, UUID], float]] = defaultdict(
            dict
        )
        for edge in selected.values():
            source = (edge.source_kind, edge.source_id)
            target = (edge.target_kind, edge.target_id)
            weight = max(float(edge.weight), 0.0)
            adjacency[source][target] = adjacency[source].get(target, 0.0) + weight
            adjacency[target][source] = adjacency[target].get(source, 0.0) + weight

        total_seed_weight = sum(seed_weights.values()) or float(len(seed_weights))
        personalization = {
            node: seed_weights.get(node, 0.0) / total_seed_weight for node in nodes
        }
        ranks = dict(personalization)
        damping = 0.5
        for _iteration in range(12):
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

        # Visible degree is safe request-local tie information; global
        # KnowledgeNodeStat.in_degree is actor-independent and must not rank UI
        # or associative results.
        max_degree = max((len(adjacency.get(node, {})) for node in nodes), default=0)
        visible_prior = {
            node: (len(adjacency.get(node, {})) / max_degree if max_degree else 0.0)
            for node in nodes
        }
        hydrated = self.hydrate_entities(
            session,
            workspace_id,
            entities=list(nodes),
            actor=actor,
        )
        results: list[NeighborEdge] = []
        for ref, score in sorted(
            ranks.items(),
            key=lambda item: (
                -item[1],
                -visible_prior[item[0]],
                item[0][0],
                str(item[0][1]),
            ),
        ):
            info = hydrated.get(ref)
            if info is None:
                continue
            results.append(
                NeighborEdge(
                    kind=ref[0],
                    id=ref[1],
                    title=info.title,
                    path=info.path,
                    edge_kind="associative",
                    weight=score,
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
        ctx = GraphHydrationContext(session, workspace_id, actor)
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
            hydration_context=ctx,
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
            hydration_context=ctx,
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
            hydration_context=ctx,
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
            hydration_context=ctx,
        )
        payload = GraphNeighborsPayload(
            backlinks=[_neighbor_entry(edge) for edge in backlinks],
            outbound=[_neighbor_entry(edge) for edge in outbound],
            citations=[_neighbor_entry(edge) for edge in citations],
            co_access=[_neighbor_entry(edge) for edge in co_access],
            truncated=any(
                group.scan_capped or group.result_capped
                for group in (backlinks, outbound, citations, co_access)
            ),
        )
        if payload.truncated:
            payload.warnings.append("graph_projection_source_bounded")
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

        Two window queries first apply actor-equivalent SQL visibility, then
        take a deterministic, hard-bounded candidate sample per seed before
        canonical hydration. Hidden rows therefore cannot consume the 1,024
        actor-visible slots, while a high-degree seed is never fully
        materialized. Hydrators run once per entity kind, so a 50-turn Ask
        session does not turn into hundreds of queries.
        """
        unique_ids = list(dict.fromkeys(page_ids))
        if not unique_ids:
            return {}
        ctx = GraphHydrationContext(session, workspace_id, actor)
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
            actor=actor,
        )
        outbound = _bounded_context_edge_rows(
            session,
            workspace_id,
            visible_ids,
            direction="out",
            edge_kinds=("wikilink", "citation_page", "citation_source"),
            actor=actor,
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
        hydration_context: GraphHydrationContext | None = None,
    ) -> NeighborEdges:
        """1-hop neighbors from the edge projection, weight-descending.

        Hub damping (Sprint-3 scope) = weight ordering + the hard limit;
        true damping is G3.
        """
        if limit <= 0:
            return NeighborEdges()
        candidates = self.visible_projection_edges(
            session,
            workspace_id,
            kind=kind,
            entity_id=entity_id,
            actor=actor,
            edge_kinds=edge_kinds,
            direction=direction,
            limit=limit + 1,
            hydration_context=hydration_context,
        )
        results: list[NeighborEdge] = []
        seen: set[tuple[str, UUID, str]] = set()
        for edge in candidates:
            dedupe_key = (edge.neighbor_kind, edge.neighbor_id, edge.edge_kind)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            results.append(
                NeighborEdge(
                    kind=edge.neighbor_kind,
                    id=edge.neighbor_id,
                    title=edge.neighbor_title,
                    path=edge.neighbor_path,
                    edge_kind=edge.edge_kind,
                    weight=edge.weight,
                    direction=edge.direction,
                )
            )
            if len(results) > limit:
                break
        return NeighborEdges(
            results[:limit],
            scan_capped=candidates.scan_capped,
            result_capped=len(results) > limit,
        )

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
    actor: ActorContext | None,
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
    visibility_predicate = graph_neighbor_visibility_predicate(
        neighbor_kind,
        neighbor_id,
        workspace_id=workspace_id,
        actor=actor,
    )
    # This is deliberately a bounded actor-visible sample rather than a claim
    # of complete traversal. Hidden candidates are excluded before row_number,
    # so they cannot starve visible links, and the 1,024 slots retain a hard
    # SQL/materialization bound for genuinely high-degree visible seeds.
    candidate_rank = (
        func.row_number()
        .over(
            partition_by=seed_column,
            order_by=(
                EdgeProjection.weight.desc(),
                neighbor_kind.asc(),
                neighbor_id.asc(),
                EdgeProjection.edge_kind.asc(),
            ),
        )
        .label("candidate_rank")
    )
    ranked = (
        select(
            seed_column.label("seed_id"),
            neighbor_kind.label("neighbor_kind"),
            neighbor_id.label("neighbor_id"),
            EdgeProjection.edge_kind.label("edge_kind"),
            EdgeProjection.weight.label("weight"),
            candidate_rank,
        )
        .where(EdgeProjection.workspace_id == workspace_id)
        .where(kind_predicate)
        .where(seed_column.in_(page_ids))
        .where(EdgeProjection.edge_kind.in_(edge_kinds))
        .where(visibility_predicate)
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
        .where(ranked.c.candidate_rank <= CONTEXT_GRAPH_RAW_CANDIDATES_PER_SEED)
        .order_by(
            ranked.c.seed_id.asc(),
            ranked.c.candidate_rank.asc(),
        )
    ).all()
    return [(*row, direction) for row in rows]


def graph_visibility_signature(actor: ActorContext | None) -> tuple:
    """Everything graph_neighbor_visibility_predicate reads from ``actor``.

    Two readers with the same signature get the same predicate, which is what
    lets a built statement be reused for both. A reader who can see drafts is
    their own signature: other editors' in-progress rows are hidden from them.
    Internal ``actor=None`` tree reads skip target ACLs, so they must not share
    a statement with authenticated viewers even when their other flags match.
    test_graph_visibility_signature pins that this covers every actor field
    the predicate uses.
    """

    drafts = actor_can_read_drafts(actor)
    identity = (
        (getattr(actor, "actor_kind", None), getattr(actor, "actor_id", None))
        if drafts
        else None
    )
    return (
        actor is None,
        drafts,
        identity,
        actor_can_read_restricted_library(actor),
        actor_can_read_sensitive_agent_pages(actor),
    )


def _denominator_visibility(workspace_id: UUID, actor: ActorContext | None):
    """The relation-target and folder-child predicates the counts use."""

    visible_target = graph_neighbor_visibility_predicate(
        EdgeProjection.target_kind,
        EdgeProjection.target_id,
        workspace_id=workspace_id,
        actor=actor,
    )
    # A child folder is visible when its own row is: tree rows exist only
    # for visible nodes, folders carry no entity ACL of their own, and a
    # child's ancestor chain is its parent's, which the reader can see
    # because only hydrated tree relations bring a parent here.
    visible_child = or_(
        visible_target,
        and_(
            EdgeProjection.target_kind == EntityKind.WORKSPACE_TREE_NODE.value,
            select(WorkspaceTreeNode.id)
            .where(
                WorkspaceTreeNode.id == EdgeProjection.target_id,
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.visibility_status == "visible",
            )
            .exists(),
        ),
    )
    return visible_target, visible_child


def graph_neighbor_visibility_predicate(
    neighbor_kind,
    neighbor_id,
    *,
    workspace_id: UUID,
    actor: ActorContext | None,
    allow_tree_fallback: bool = False,
):
    """Build actor-equivalent SQL ACLs for graph endpoint families.

    For context_page, agent_page, library_item, library_item_version and
    library_artifact this admits exactly what HYDRATORS admit, so counts
    taken in SQL (the rank denominators) cannot include a relation the
    reader cannot see; test_graph_visibility_predicate_parity pins it.
    Hydrators still run after the query. Tree rows must pass the same target
    and ancestor visibility test *before* a ranked or observation raw limit;
    otherwise hidden placements can consume the limit and change a viewer's
    scene or completeness.
    """

    context_visible = (
        select(ContextPage.id)
        .where(
            ContextPage.id == neighbor_id,
            ContextPage.workspace_id == workspace_id,
            ContextPage.lifecycle_status != "archived",
            visible_context_version_expression(actor).is_not(None),
        )
        .exists()
    )

    # Keep these literals aligned with okf.EXCLUDED_LIBRARY_LIFECYCLE_STATUSES;
    # the post-query canonical hydrator remains authoritative if policy grows.
    library_conditions = [
        LibraryItem.workspace_id == workspace_id,
        LibraryItem.lifecycle_status.notin_(("archived", "hidden")),
    ]
    if not actor_can_read_restricted_library(actor):
        library_conditions.append(LibraryItem.trust_level != "restricted")

    item_visible = (
        select(LibraryItem.id)
        .where(
            LibraryItem.id == neighbor_id,
            *library_conditions,
        )
        .exists()
    )
    item_version_visible = (
        select(LibraryItemVersion.id)
        .join(
            LibraryItem,
            LibraryItemVersion.library_item_id == LibraryItem.id,
        )
        .where(
            LibraryItemVersion.id == neighbor_id,
            _canonical_library_version_condition(),
            *library_conditions,
        )
        .exists()
    )
    artifact_visible = (
        select(LibraryArtifact.id)
        .join(
            LibraryItemVersion,
            LibraryArtifact.library_item_version_id == LibraryItemVersion.id,
        )
        .join(
            LibraryItem,
            LibraryItemVersion.library_item_id == LibraryItem.id,
        )
        .where(
            LibraryArtifact.id == neighbor_id,
            _canonical_library_version_condition(),
            # Only the selected conversion attempt's artifacts are the
            # version's content; with no attempt selected there are none.
            LibraryItemVersion.selected_conversion_attempt_id.is_not(None),
            LibraryArtifact.attempt_id
            == LibraryItemVersion.selected_conversion_attempt_id,
            *library_conditions,
        )
        .exists()
    )
    agent_conditions = [
        AgentPage.id == neighbor_id,
        AgentPage.workspace_id == workspace_id,
        AgentPage.lifecycle_status != "archived",
    ]
    if not actor_can_read_sensitive_agent_pages(actor):
        agent_conditions.extend(
            (
                AgentPage.category.notin_(SENSITIVE_AGENT_PAGE_CATEGORIES),
                AgentPage.sensitivity.in_(VISIBLE_AGENT_SENSITIVITIES),
            )
        )
    agent_heads = [
        and_(
            AgentPage.lifecycle_status == "active",
            AgentPage.current_active_version_id.is_not(None),
        )
    ]
    if actor_can_read_drafts(actor):
        agent_heads.append(AgentPage.latest_draft_version_id.is_not(None))
    agent_visible = (
        select(AgentPage.id)
        .where(
            *agent_conditions,
            or_(*agent_heads),
        )
        .exists()
    )

    predicates = [
        and_(
            neighbor_kind == EntityKind.CONTEXT_PAGE.value,
            context_visible,
        ),
        and_(
            neighbor_kind == EntityKind.LIBRARY_ITEM.value,
            item_visible,
        ),
        and_(
            neighbor_kind == EntityKind.LIBRARY_ITEM_VERSION.value,
            item_version_visible,
        ),
        and_(
            neighbor_kind == EntityKind.LIBRARY_ARTIFACT.value,
            artifact_visible,
        ),
        and_(
            neighbor_kind == EntityKind.AGENT_PAGE.value,
            agent_visible,
        ),
    ]
    if allow_tree_fallback:
        predicates.append(
            and_(
                neighbor_kind == EntityKind.WORKSPACE_TREE_NODE.value,
                _tree_chain_visibility_predicate(
                    neighbor_id, workspace_id=workspace_id, actor=actor
                ),
            )
        )
    return or_(*predicates)


def _tree_chain_visibility_predicate(
    neighbor_id,
    *,
    workspace_id: UUID,
    actor: ActorContext | None,
):
    """Correlated, depth-bounded tree closure matching visible_tree_nodes.

    A nested recursive CTE is evaluated for the candidate row, not the whole
    workspace. All ancestors must be visible and the chain must reach a root
    within the same 64-hop bound used by the hydrator. This is intentionally
    in SQL so invisible rows cannot spend a LIMIT applied outside the query.
    """

    endpoint = (
        "source"
        if neighbor_id is EdgeProjection.source_id
        else "target"
        if neighbor_id is EdgeProjection.target_id
        else None
    )
    cache_key = (
        (workspace_id, graph_visibility_signature(actor), endpoint)
        if endpoint is not None
        else None
    )
    if cache_key is not None and cache_key in _TREE_CHAIN_PREDICATES:
        return _TREE_CHAIN_PREDICATES[cache_key]

    tree_node = aliased(WorkspaceTreeNode)
    tree_parent = aliased(WorkspaceTreeNode)
    chain = (
        select(
            tree_node.id.label("id"),
            tree_node.parent_id.label("parent_id"),
            tree_node.workspace_id.label("workspace_id"),
            tree_node.visibility_status.label("visibility_status"),
            tree_node.target_kind.label("target_kind"),
            tree_node.target_id.label("target_id"),
            literal(0).label("depth"),
        )
        .where(tree_node.id == neighbor_id, tree_node.workspace_id == workspace_id)
        .correlate_except(tree_node)
        .cte(recursive=True, nesting=True)
    )
    chain = chain.union_all(
        select(
            tree_parent.id,
            tree_parent.parent_id,
            tree_parent.workspace_id,
            tree_parent.visibility_status,
            tree_parent.target_kind,
            tree_parent.target_id,
            chain.c.depth + 1,
        )
        .join(chain, tree_parent.id == chain.c.parent_id)
        .where(chain.c.depth < GRAPH_TREE_HYDRATION_ANCESTOR_DEPTH_LIMIT)
    )
    invalid = [
        chain.c.workspace_id != workspace_id,
        chain.c.visibility_status != "visible",
    ]
    if actor is not None:
        invalid.append(
            and_(
                chain.c.target_id.is_not(None),
                ~graph_neighbor_visibility_predicate(
                    chain.c.target_kind,
                    chain.c.target_id,
                    workspace_id=workspace_id,
                    actor=actor,
                ),
            )
        )
    # Aggregate over the chain once. Separate EXISTS clauses would execute
    # the recursive walk twice for every candidate relation.
    predicate = (
        select(
            and_(
                func.max(case((chain.c.parent_id.is_(None), 1), else_=0)) == 1,
                func.max(case((or_(*invalid), 1), else_=0)) == 0,
            )
        )
        .select_from(chain)
        .scalar_subquery()
    )
    if cache_key is not None:
        if len(_TREE_CHAIN_PREDICATES) >= _DENOMINATOR_STATEMENT_LIMIT:
            _TREE_CHAIN_PREDICATES.pop(next(iter(_TREE_CHAIN_PREDICATES)), None)
        _TREE_CHAIN_PREDICATES[cache_key] = predicate
    return predicate

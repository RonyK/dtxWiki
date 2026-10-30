"""Actor-visible, renderer-neutral Graph View scene construction."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import (
    Boolean,
    Integer,
    String,
    Uuid,
    and_,
    case,
    column,
    func,
    literal,
    or_,
    select,
    tuple_,
    values,
)
from sqlalchemy.orm import Session, aliased

from backend.models.ask import AskSession, AskTurn, AskTurnCitation
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.graph import GraphHistoryCoverage, GraphObservation
from backend.models.mcp_chat import McpChatSession, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import _role_allows, actor_can_read_drafts
from backend.repositories.workspace_scoped import select_projection_in_workspace
from backend.schemas.graph import (
    GraphBudgetResult,
    GraphCapabilities,
    GraphEdge,
    GraphEvidenceRef,
    GraphFacets,
    GraphHistoryMetadata,
    GraphNode,
    GraphOmission,
    GraphQueryPayload,
    GraphQueryRequest,
    GraphTimelineEvent,
)
from backend.services.actor_visible_read import (
    PUBLISHED_CONTEXT_VERSION_STATUSES,
    actor_can_read_audit_records,
    context_version_is_visible,
)
from backend.services.graph_observation_service import GraphObservationService
from backend.services.graph_rank import (
    DENOMINATOR_KINDS,
    ROLE_ORDER,
    Outcome,
    candidate_order_key,
    effective_weight,
    fair_share_quotas,
    kind_order_key,
    rank_score,
    round_robin_take,
)
from backend.services.graph_semantics import (
    ACTION_SEMANTICS,
    EDGE_SEMANTICS,
    NODE_FAMILY_BY_KIND,
    action_semantic,
    edge_semantic,
)
from backend.services.graph_service import (
    GraphHydrationContext,
    GraphService,
    VisibleProjectionEdge,
    graph_neighbor_visibility_predicate,
)
from backend.services.read_errors import ReadNotFoundError, ReadValidationError

GRAPH_TRANSPORT_NAMESPACE = UUID("9fa0a65c-22fc-5e2f-bc73-0bcd967fd57a")
GRAPH_OBSERVATION_RAW_SCAN_LIMIT = 1_024
GRAPH_OBSERVATION_PAGE_LIMIT = 8
_ObservationIdentity = tuple[str, UUID, str, UUID, str]

_PRESET_LAYERS = {
    "linked-knowledge": {"knowledge"},
    "evidence": {"evidence"},
    "activity": {"activity", "action"},
    "all": {"knowledge", "evidence", "activity", "action"},
}

_CHANGE_ACTIONS = {
    "create": "created",
    "create_draft": "created",
    "update": "edited",
    "update_draft": "edited",
    "metadata_patch": "edited",
    "publish": "published",
    "restore": "restored",
    "supersede": "superseded",
    "promote": "promoted",
}

_MCP_ACTIONS = {
    "created": "created",
    "edited": "edited",
    "cited": "cited",
    "referenced": "referenced",
}


@dataclass(frozen=True)
class _EdgeCandidate:
    edge: GraphEdge
    nodes: tuple[GraphNode, GraphNode]
    priority: int
    semantic_identity: tuple[str, str, str, str | None]
    # The seed GraphNode.id whose share the edge spends, or None for a
    # relation with no seed endpoint; role is out, in, undirected or context.
    anchor: str | None
    role: str


@dataclass(frozen=True)
class _TimelineDraft:
    identity: str
    operation: str
    action_kind: str
    label: str
    occurred_at: datetime
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    evidence: GraphEvidenceRef | None
    node_snapshots: tuple[GraphNode, ...] = ()
    edge_snapshots: tuple[GraphEdge, ...] = ()
    source_sequence: int = 0


@dataclass
class _ObservationScanState:
    """One bounded evidence catalog shared by every current-scene frontier."""

    visible_limit: int
    visible: list[GraphObservation] = field(default_factory=list)
    scanned_ids: set[UUID] = field(default_factory=set)
    raw_scanned: int = 0
    pages_scanned: int = 0
    capped: bool = False

    def ordered(self) -> list[GraphObservation]:
        return sorted(
            self.visible,
            key=lambda row: (row.occurred_at, row.workspace_sequence),
        )


class GraphQueryService:
    def __init__(
        self,
        *,
        graph_service: GraphService | None = None,
        observation_service: GraphObservationService | None = None,
    ) -> None:
        self.graph_service = graph_service or GraphService()
        self.observations = observation_service or GraphObservationService()

    def query(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        actor: ActorContext | None,
        usage_signals_enabled: bool,
    ) -> GraphQueryPayload:
        """Build one actor-visible scene around the request's seeds.

        ``usage_signals_enabled`` is the owner's switch and has no default.
        Off, no co_access edge enters the scene even while projection rows
        remain from before the switch, and a request that would have shown
        them says so with ``usage_relation_signals_disabled``.
        """

        generated_at = utc_now()
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        hydration_context = self.graph_service.hydration_context(
            session, workspace_id, actor
        )

        seed_refs = [(seed.kind, seed.id) for seed in request.seeds]
        if any(kind != "context_page" for kind, _seed_id in seed_refs):
            raise ReadValidationError("local graph seeds must be context_page entities")
        if len(seed_refs) != len(set(seed_refs)):
            raise ReadValidationError("graph seeds must be unique")
        if len(seed_refs) > request.budget.nodes:
            raise ReadValidationError("node budget must accommodate every seed")
        if request.time.mode == "as_of" and (
            request.time.from_ is not None or request.time.to is not None
        ):
            raise ReadValidationError("as_of cannot be combined with a time window")
        if not set(request.node_kinds).issubset(NODE_FAMILY_BY_KIND):
            raise ReadValidationError("unsupported graph node kind filter")
        if not set(request.edge_kinds).issubset(EDGE_SEMANTICS):
            raise ReadValidationError("unsupported graph edge kind filter")
        if not set(request.action_kinds).issubset(ACTION_SEMANTICS):
            raise ReadValidationError("unsupported graph action kind filter")

        hydrated_seeds = self.graph_service.hydrate_entities(
            session,
            workspace_id,
            entities=seed_refs,
            actor=actor,
            hydration_context=hydration_context,
        )
        if any(seed_ref not in hydrated_seeds for seed_ref in seed_refs):
            raise ReadNotFoundError("context page not found")

        pages = {
            page.id: page
            for page in session.scalars(
                select(ContextPage).where(
                    ContextPage.workspace_id == workspace_id,
                    ContextPage.id.in_([seed.id for seed in request.seeds]),
                )
            ).all()
        }
        seed_nodes: dict[str, GraphNode] = {}
        for seed in request.seeds:
            page = pages.get(seed.id)
            hydrated = hydrated_seeds[(seed.kind, seed.id)]
            if page is None:
                raise ReadNotFoundError("context page not found")
            node = GraphNode(
                id=graph_node_id(seed.kind, seed.id),
                entity_kind=seed.kind,
                entity_id=seed.id,
                family="document",
                label=page.title[:200],
                status=page.lifecycle_status,
                path=hydrated.path,
                is_seed=True,
                occurred_at=page.created_at,
            )
            seed_nodes[node.id] = node

        active_layers = set(request.layers) & _PRESET_LAYERS[request.preset]
        warnings: list[str] = []
        # co_access carried a k-anonymity gate that never fit this product. A
        # workspace is a team, not the public internet: activity is already
        # recorded and shared inside it, and at the team sizes dtxWiki targets
        # an anonymity threshold is unreachable, so the gate only guaranteed
        # the relation stayed empty. What actually has to hold is access
        # control, and that is enforced elsewhere and for every edge kind
        # alike: hydration drops any neighbour the actor cannot see, so an
        # edge can only reach a reader who can already read both endpoints.

        coverages = self._coverages(session, workspace_id, request)
        # Edge observations alone cannot reconstruct historical Context title,
        # lifecycle status, or path. Returning current node metadata around an
        # old edge set would overstate a true historical scene, even when the
        # edge stream itself is marked complete. Keep the interval validator as
        # a future seam, but fail closed until node snapshots are covered too.
        as_of_ready = False
        if request.time.mode == "as_of" and not as_of_ready:
            return self._unsupported_as_of_payload(
                request=request,
                workspace=workspace,
                generated_at=generated_at,
                seed_nodes=seed_nodes,
                warnings=warnings,
                coverages=coverages,
            )

        candidates: list[_EdgeCandidate] = []
        extra_nodes: dict[str, GraphNode] = {}
        timeline_drafts: list[_TimelineDraft] = []
        source_capped = False

        observation_scan = _ObservationScanState(
            visible_limit=min(1_000, request.budget.nodes + request.budget.edges)
        )

        observations, observations_capped = self._observations_for_seeds(
            session,
            workspace_id,
            request,
            actor=actor,
            through=(request.time.as_of if request.time.mode == "as_of" else None),
            hydration_context=hydration_context,
            usage_signals_enabled=usage_signals_enabled,
            scan=observation_scan,
        )
        source_capped = source_capped or observations_capped
        if observations_capped:
            warnings.append("graph_history_source_bounded")
        if request.time.mode == "as_of" and observations_capped:
            return self._unsupported_as_of_payload(
                request=request,
                workspace=workspace,
                generated_at=generated_at,
                seed_nodes=seed_nodes,
                warnings=warnings,
                coverages=coverages,
            )

        if request.time.mode != "as_of":
            projection_candidates, capped = self._projection_candidates(
                session,
                workspace_id,
                request=request,
                actor=actor,
                seed_nodes=seed_nodes,
                active_layers=active_layers,
                hydration_context=hydration_context,
                usage_signals_enabled=usage_signals_enabled,
                now=generated_at,
            )
            candidates.extend(projection_candidates)
            source_capped = source_capped or capped
            if capped and "graph_projection_source_bounded" not in warnings:
                warnings.append("graph_projection_source_bounded")

        observation_candidates, observation_nodes = self._observation_scene(
            session,
            workspace_id,
            request=request,
            actor=actor,
            seed_nodes=seed_nodes,
            active_layers=active_layers,
            observations=observations,
            at=(request.time.as_of or generated_at),
            hydration_context=hydration_context,
        )
        candidates.extend(
            self._ranked_observations(
                session,
                workspace_id,
                actor=actor,
                observed=observation_candidates,
                projected=candidates,
                now=generated_at,
            )
        )
        extra_nodes.update(observation_nodes)

        if request.time.mode in {"appearance", "action"}:
            (
                domain_candidates,
                domain_nodes,
                domain_timeline,
                domain_capped,
            ) = self._domain_actions(
                session,
                workspace_id,
                request=request,
                actor=actor,
                seed_nodes=seed_nodes,
                active_layers=active_layers,
            )
            candidates.extend(domain_candidates)
            extra_nodes.update(domain_nodes)
            timeline_drafts.extend(domain_timeline)
            source_capped = source_capped or domain_capped
            if domain_capped and "graph_history_source_bounded" not in warnings:
                warnings.append("graph_history_source_bounded")
            observation_timeline = self._observation_timeline(
                session,
                workspace_id,
                request=request,
                actor=actor,
                seed_nodes=seed_nodes,
                active_layers=active_layers,
                observations=observations,
                hydration_context=hydration_context,
            )
            timeline_drafts.extend(observation_timeline)

        if not usage_signals_enabled:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.edge.edge_kind != "co_access"
            ]
            if _requests_co_access(request, active_layers):
                warnings.append("usage_relation_signals_disabled")

        omissions: list[GraphOmission] = []
        if request.time.mode == "current":
            (
                nodes,
                edges,
                budget_capped,
                local_capped,
                omissions,
            ) = self._current_projection_scene(
                session,
                workspace_id,
                request=request,
                actor=actor,
                seed_nodes=seed_nodes,
                candidates=candidates,
                active_layers=active_layers,
                hydration_context=hydration_context,
                usage_signals_enabled=usage_signals_enabled,
                now=generated_at,
                observation_scan=observation_scan,
            )
            source_capped = source_capped or local_capped
            if local_capped and "graph_projection_source_bounded" not in warnings:
                warnings.append("graph_projection_source_bounded")
            observations = observation_scan.ordered()
            source_capped = source_capped or observation_scan.capped
            if (
                observation_scan.capped
                and "graph_history_source_bounded" not in warnings
            ):
                warnings.append("graph_history_source_bounded")
        else:
            nodes, edges, budget_capped = self._apply_budget(
                request=request,
                seed_nodes=seed_nodes,
                candidates=candidates,
                extra_nodes=extra_nodes,
            )
        timeline, timeline_capped = self._finalize_timeline(
            timeline_drafts,
            nodes,
            edges,
            node_limit=request.budget.nodes,
            edge_limit=request.budget.edges,
        )
        # Policy omissions (for example the reserved folder slice) make the
        # scene partial without necessarily exhausting the requested budget.
        scene_capped = budget_capped or bool(omissions)
        truncated = scene_capped or source_capped or timeline_capped
        if timeline_capped:
            warnings.append("graph_timeline_catalog_bounded")

        if budget_capped or timeline_capped:
            warnings.append("graph_budget_applied")
        history = self._history_metadata(
            request=request,
            observations=observations,
            timeline=timeline,
            coverages=coverages,
        )
        if timeline_capped:
            history.warnings.append("graph_timeline_catalog_bounded")
        completeness = (
            "bounded_sample"
            if source_capped
            else "capped"
            if scene_capped or timeline_capped
            else "complete"
        )
        return GraphQueryPayload(
            query=request,
            projection_revision=workspace.edge_projection_version,
            generated_at=generated_at,
            nodes=nodes,
            edges=edges,
            timeline=timeline,
            mermaid=None,
            facets=_facets(nodes, edges, timeline),
            budget=GraphBudgetResult(
                requested=request.budget,
                applied=request.budget,
            ),
            completeness=completeness,
            truncated=truncated,
            omissions=omissions,
            warnings=warnings,
            history=history,
            capabilities=GraphCapabilities(
                expand=True,
                evidence=False,
                # The client exports only this already actor-authorized,
                # filtered scene as a user-started Mermaid file.
                export=True,
                saved_view=False,
                timeline=True,
                as_of=False,
            ),
        )

    def _projection_candidates(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        actor: ActorContext | None,
        seed_nodes: dict[str, GraphNode],
        active_layers: set[str],
        hydration_context: GraphHydrationContext,
        usage_signals_enabled: bool,
        now: datetime,
        min_partition_cap: int = 1,
        exclude_structural_neighbors: bool = False,
    ) -> tuple[list[_EdgeCandidate], bool]:
        result: list[_EdgeCandidate] = []
        explicit_edge_kinds = set(request.edge_kinds)
        allowed_edge_kinds: set[str] = set()
        for edge_kind, semantic in EDGE_SEMANTICS.items():
            if explicit_edge_kinds and edge_kind not in explicit_edge_kinds:
                continue
            if semantic.layer not in active_layers:
                continue
            if not semantic.default_visible:
                continue
            if request.action_kinds and semantic.action_kind is not None:
                if semantic.action_kind not in request.action_kinds:
                    continue
            allowed_edge_kinds.add(edge_kind)
        # Rows the switch hides must not spend a partition or a queue; the
        # post-filter in query() still covers every other tier.
        if not usage_signals_enabled:
            allowed_edge_kinds.discard("co_access")
        if not allowed_edge_kinds:
            return [], False

        allowed_neighbor_kinds = set(NODE_FAMILY_BY_KIND)
        if request.node_families:
            allowed_neighbor_kinds = {
                kind
                for kind in allowed_neighbor_kinds
                if NODE_FAMILY_BY_KIND[kind] in request.node_families
            }
        if request.node_kinds:
            allowed_neighbor_kinds &= set(request.node_kinds)
        # Context versions are historical artifacts. Library source
        # citations still resolve to canonical LibraryItemVersion rows until
        # their projection can carry the stable Library item identity.
        allowed_neighbor_kinds.discard("context_page_version")
        if exclude_structural_neighbors:
            allowed_neighbor_kinds.discard("workspace_tree_node")
        if not allowed_neighbor_kinds:
            return [], False

        seed_by_entity = {
            (node.entity_kind, node.entity_id): node for node in seed_nodes.values()
        }
        # One ranked read for every seed: each relation kind is collected and
        # ranked on its own, so no kind's raw weight decides another's place.
        projected = self.graph_service.ranked_projection_edges(
            session,
            workspace_id,
            seeds=tuple(seed_by_entity),
            actor=actor,
            edge_kinds=tuple(sorted(allowed_edge_kinds)),
            neighbor_kinds=tuple(sorted(allowed_neighbor_kinds)),
            occurred_from=request.time.from_,
            occurred_to=request.time.to,
            direction=request.direction,
            # Frontier discovery spends node slots independently of the final
            # relation display quota. A one-edge display must not discover
            # only two neighbours when the node budget admits many more.
            edge_budget=max(request.budget.nodes, request.budget.edges),
            now=now,
            hydration_context=hydration_context,
            min_partition_cap=min_partition_cap,
        )
        capped = projected.scan_capped
        for projected_edge in projected:
            seed_node = seed_by_entity[
                (projected_edge.source_kind, projected_edge.source_id)
                if projected_edge.direction == "out"
                else (projected_edge.target_kind, projected_edge.target_id)
            ]
            semantic = edge_semantic(projected_edge.edge_kind)
            if projected_edge.edge_kind not in EDGE_SEMANTICS:
                continue
            if not semantic.default_visible:
                continue
            if semantic.layer not in active_layers:
                continue
            if not _time_allowed(projected_edge.occurred_at, request):
                continue

            neighbor_node = _node_from_projection(projected_edge)
            if not _node_filter_allows(neighbor_node, request):
                continue
            source_node = (
                seed_node
                if projected_edge.source_id == seed_node.entity_id
                and projected_edge.source_kind == seed_node.entity_kind
                else neighbor_node
            )
            target_node = (
                seed_node
                if projected_edge.target_id == seed_node.entity_id
                and projected_edge.target_kind == seed_node.entity_kind
                else neighbor_node
            )
            edge_id = graph_edge_id(
                workspace_id,
                source_node.id,
                target_node.id,
                projected_edge.edge_kind,
                "current",
            )
            edge = GraphEdge(
                id=edge_id,
                source=source_node.id,
                target=target_node.id,
                edge_kind=projected_edge.edge_kind,
                action_kind=semantic.action_kind,
                label=(
                    semantic.label
                    if projected_edge.direction == "out"
                    else semantic.inverse_label
                ),
                layer=semantic.layer,
                provenance=projected_edge.provenance,
                direction="directed" if semantic.directed else "undirected",
                weight=max(0.0, float(projected_edge.weight)),
                occurred_at=projected_edge.occurred_at,
                aggregate=projected_edge.edge_kind == "co_access",
                support_session_count=projected_edge.support_session_count,
                first_observed_at=projected_edge.first_observed_at,
                rank_score=projected_edge.rank_score,
            )
            result.append(
                _EdgeCandidate(
                    edge=edge,
                    nodes=(source_node, target_node),
                    priority=0,
                    semantic_identity=(
                        edge.source,
                        edge.target,
                        edge.edge_kind,
                        edge.action_kind,
                    ),
                    anchor=seed_node.id,
                    role=(
                        projected_edge.direction if semantic.directed else "undirected"
                    ),
                )
            )
        return result, capped

    def _current_projection_scene(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        actor: ActorContext | None,
        seed_nodes: dict[str, GraphNode],
        candidates: list[_EdgeCandidate],
        active_layers: set[str],
        hydration_context: GraphHydrationContext,
        usage_signals_enabled: bool,
        now: datetime,
        observation_scan: _ObservationScanState,
    ) -> tuple[list[GraphNode], list[GraphEdge], bool, bool, list[GraphOmission]]:
        """Select visible hop frontiers before filling the induced relation set.

        Node discovery and relation display have different budgets. In
        particular, an edge from one chosen neighbour to another must never
        consume a node slot or be lost because seed spokes spent edge slots.
        """

        seeds = dict(sorted(seed_nodes.items(), key=lambda item: _entity_key(item[1])))
        seed_ids = set(seeds)
        reserve = min(20, max(0, request.budget.nodes - len(seeds)) // 5)
        document_slots = request.budget.nodes - len(seeds) - reserve
        h1_target = (
            document_slots if request.depth == 1 else (3 * document_slots + 4) // 5
        )

        def discovery_candidates(
            values: list[_EdgeCandidate], anchors: set[str], known: set[str]
        ) -> list[_EdgeCandidate]:
            return [
                candidate
                for candidate in values
                if candidate.anchor in anchors
                and any(
                    node.id not in known
                    and node.family != "structural"
                    and node.entity_kind != "context_page_version"
                    for node in candidate.nodes
                )
            ]

        def select_frontier(
            known: dict[str, GraphNode],
            values: list[_EdgeCandidate],
            target_size: int,
        ) -> dict[str, GraphNode]:
            limit = min(request.budget.nodes, len(known) + target_size)
            selection_request = request.model_copy(
                update={
                    "budget": request.budget.model_copy(
                        update={"nodes": limit, "edges": 750}
                    )
                }
            )
            picked, _spokes, _capped = self._apply_budget(
                request=selection_request,
                seed_nodes=known,
                candidates=values,
                extra_nodes={},
            )
            return {
                node.id: node
                for node in sorted(picked, key=_entity_key)
                if node.id not in known
            }

        first_candidates = discovery_candidates(candidates, seed_ids, seed_ids)
        # Every visible direct neighbour has shortest distance one, even if
        # its first-hop allocation could not admit it yet. Do not relabel such
        # a neighbour as hop two merely because a selected neighbour also
        # points to it.
        direct_neighbor_ids = {
            node.id
            for candidate in first_candidates
            for node in candidate.nodes
            if node.id not in seed_ids
            and node.family != "structural"
            and node.entity_kind != "context_page_version"
        }
        h1 = select_frontier(seeds, first_candidates, h1_target)
        known = {**seeds, **h1}
        second_candidates: list[_EdgeCandidate] = []
        second_capped = False
        if request.depth == 2 and h1:
            second_candidates, second_capped = self._projection_candidates(
                session,
                workspace_id,
                request=request,
                actor=actor,
                seed_nodes=h1,
                active_layers=active_layers,
                hydration_context=hydration_context,
                usage_signals_enabled=usage_signals_enabled,
                now=now,
                min_partition_cap=8,
                exclude_structural_neighbors=True,
            )
            if request.time.from_ is not None or request.time.to is not None:
                second_candidates.extend(
                    self._current_observation_candidates(
                        session,
                        workspace_id,
                        request=request,
                        actor=actor,
                        frontier=h1,
                        active_layers=active_layers,
                        projected=second_candidates,
                        hydration_context=hydration_context,
                        usage_signals_enabled=usage_signals_enabled,
                        now=now,
                        scan=observation_scan,
                    )
                )
        second_discovery = discovery_candidates(
            second_candidates, set(h1), set(known) | direct_neighbor_ids
        )
        # Unused first-hop allocation transfers to the second frontier.
        h2_target = max(0, document_slots - len(h1))
        h2 = select_frontier(known, second_discovery, h2_target)
        known.update(h2)
        # If the second frontier is short, spend the remainder on first-hop
        # candidates. Their hop remains one even though they arrived later.
        if len(h1) + len(h2) < document_slots:
            more_h1 = select_frontier(
                known,
                discovery_candidates(first_candidates, seed_ids, set(known)),
                document_slots - len(h1) - len(h2),
            )
            h1.update(more_h1)
            known.update(more_h1)

        # Tree nodes are structural context for selected documents. Reserve
        # their own slots so a busy folder cannot crowd out the neighbours.
        folder_selection_cache: dict[
            frozenset[str], tuple[list[GraphNode], list[GraphNode], dict[str, set[str]]]
        ] = {}

        def choose_folders(
            selected_documents: dict[str, GraphNode],
        ) -> tuple[list[GraphNode], list[GraphNode], dict[str, set[str]]]:
            if (
                "knowledge" not in active_layers
                or (request.edge_kinds and "tree" not in request.edge_kinds)
                or (request.node_families and "structural" not in request.node_families)
                or (
                    request.node_kinds
                    and "workspace_tree_node" not in request.node_kinds
                )
            ):
                return [], [], defaultdict(set)
            cache_key = frozenset(selected_documents)
            if cache_key in folder_selection_cache:
                return folder_selection_cache[cache_key]
            folder_candidates, memberships, placed_documents = (
                self._primary_placement_folders(
                    session,
                    workspace_id,
                    nodes=selected_documents,
                    hydration_context=hydration_context,
                )
            )
            for candidate in [*candidates, *second_candidates]:
                if candidate.edge.edge_kind != "tree":
                    continue
                folder = next(
                    (node for node in candidate.nodes if node.family == "structural"),
                    None,
                )
                document = next(
                    (node for node in candidate.nodes if node.id in selected_documents),
                    None,
                )
                if (
                    folder is None
                    or document is None
                    or document.id == folder.id
                    or document.id in placed_documents
                ):
                    continue
                folder_candidates[folder.id] = folder
                memberships[folder.id].add(document.id)
            ordered = sorted(
                folder_candidates.values(),
                key=lambda node: (-len(memberships[node.id]), _entity_key(node)),
            )
            result = ordered, ordered[:reserve], memberships
            folder_selection_cache[cache_key] = result
            return result

        folder_cap_events_before = hydration_context.source_cap_events
        ordered_folders, folders, folder_frequency = choose_folders(known)
        known.update({node.id: node for node in folders})

        # Return spare folder slots to the next ranked document candidates.
        if len(known) < request.budget.nodes:
            spare = request.budget.nodes - len(known)
            extra_h1 = select_frontier(
                known,
                discovery_candidates(first_candidates, seed_ids, set(known)),
                spare,
            )
            h1.update(extra_h1)
            known.update(extra_h1)
        if len(known) < request.budget.nodes and request.depth == 2:
            spare = request.budget.nodes - len(known)
            extra_h2 = select_frontier(
                known,
                discovery_candidates(
                    second_candidates,
                    set(h1),
                    set(known) | direct_neighbor_ids,
                ),
                spare,
            )
            h2.update(extra_h2)
            known.update(extra_h2)

        # Spare-slot documents may belong to a different primary folder.
        # Recompute F from the final document set. Reserved folder slots that
        # were lent to more documents are already spent; new folders may use
        # only the remaining total-node capacity, still in frequency/key order.
        document_nodes = {**seeds, **h1, **h2}
        ordered_folders, folders, folder_frequency = choose_folders(document_nodes)
        folders = folders[: max(0, request.budget.nodes - len(document_nodes))]
        known = {**document_nodes, **{node.id: node for node in folders}}
        folder_source_capped = (
            hydration_context.source_cap_events > folder_cap_events_before
        )

        omissions: list[GraphOmission] = []
        for hop, available in ((1, first_candidates), (2, second_discovery)):
            if hop == 2 and request.depth == 1:
                continue
            if any(
                node.id not in known and node.family != "structural"
                for candidate in available
                for node in candidate.nodes
            ):
                omissions.append(
                    GraphOmission(scope="node", reason="node_quota", hop=hop)
                )
        if len(ordered_folders) > len(folders):
            omissions.append(GraphOmission(scope="node", reason="folder_node"))
        if folder_source_capped:
            omissions.append(GraphOmission(scope="node", reason="folder_source_cap"))
        if second_capped:
            omissions.append(GraphOmission(scope="node", reason="frontier_cap", hop=2))

        hop_by_id = {
            **dict.fromkeys(seeds, 0),
            **dict.fromkeys(h1, 1),
            **dict.fromkeys(h2, 2),
        }
        for folder in folders:
            hop_by_id[folder.id] = 0

        induced, induced_source_capped = self._induced_projection_edges(
            session,
            workspace_id,
            request=request,
            actor=actor,
            nodes=known,
            hop_by_id=hop_by_id,
            active_layers=active_layers,
            usage_signals_enabled=usage_signals_enabled,
            now=now,
        )
        omissions.extend(
            GraphOmission(
                scope="relation",
                reason="relation_source_cap",
                relation_class=relation_class,
            )
            for relation_class in induced_source_capped
        )
        if (
            "knowledge" in active_layers
            and (not request.edge_kinds or "tree" in request.edge_kinds)
            and request.time.from_ is None
            and request.time.to is None
        ):
            seen_memberships = {
                (candidate.edge.source, candidate.edge.target)
                for candidate in induced
                if candidate.edge.edge_kind == "tree"
            }
            for folder in folders:
                for document_id in sorted(folder_frequency[folder.id]):
                    if (folder.id, document_id) in seen_memberships:
                        continue
                    document = known[document_id]
                    edge = GraphEdge(
                        id=graph_edge_id(
                            workspace_id, folder.id, document.id, "tree", "current"
                        ),
                        source=folder.id,
                        target=document.id,
                        edge_kind="tree",
                        label=edge_semantic("tree").label,
                        layer="knowledge",
                        provenance="derived",
                    )
                    induced.append(
                        _EdgeCandidate(
                            edge=edge,
                            nodes=(folder, document),
                            priority=0,
                            semantic_identity=(folder.id, document.id, "tree", None),
                            anchor=document.id,
                            role="in",
                        )
                    )
        observed_induced = candidates
        if request.time.from_ is not None or request.time.to is not None:
            # Frontier evidence establishes which documents may enter the
            # scene, not its full relation set. Read the selected induced
            # scope too, including H1-H1 and H2-H2 occurrences in either
            # direction. Both endpoints are constrained before the bounded
            # scan, and the original hop-direction quotas still apply below.
            induced_request = request.model_copy(update={"direction": "both"})
            observed_induced = self._current_observation_candidates(
                session,
                workspace_id,
                request=induced_request,
                actor=actor,
                frontier=known,
                active_layers=active_layers,
                projected=induced,
                hydration_context=hydration_context,
                usage_signals_enabled=usage_signals_enabled,
                now=now,
                scan=observation_scan,
                induced_only=True,
            )
        # Authored observations enrich only admitted current projection
        # relations; activity may legitimately have no projection row. In
        # either case both endpoints must belong to the visible selection.
        seen = {candidate.semantic_identity for candidate in induced}
        for candidate in observed_induced:
            if candidate.priority == 0 or candidate.semantic_identity in seen:
                continue
            if all(node.id in known for node in candidate.nodes):
                induced.append(candidate)
                seen.add(candidate.semantic_identity)

        if request.direction != "both":
            induced = [
                candidate
                for candidate in induced
                if _induced_direction_allows(
                    candidate.edge, hop_by_id, known, request.direction
                )
            ]
        selected_edges, omitted_classes = _select_induced_edges(
            induced, hop_by_id, known, request.budget.edges, request.depth
        )
        omissions.extend(
            GraphOmission(
                scope="relation",
                reason="relation_quota",
                relation_class=relation_class,
            )
            for relation_class in omitted_classes
        )
        budget_capped = any(
            omission.reason in {"node_quota", "relation_quota"}
            for omission in omissions
        ) or (
            len(known) >= request.budget.nodes
            and any(omission.reason == "folder_node" for omission in omissions)
        )
        return (
            sorted(known.values(), key=_entity_key),
            sorted(
                selected_edges,
                key=lambda edge: (
                    edge.edge_kind,
                    _entity_key(known[edge.source]),
                    _entity_key(known[edge.target]),
                    edge.occurred_at.isoformat() if edge.occurred_at else "",
                ),
            ),
            budget_capped,
            second_capped or folder_source_capped or bool(induced_source_capped),
            omissions,
        )

    def _current_observation_candidates(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        actor: ActorContext | None,
        frontier: dict[str, GraphNode],
        active_layers: set[str],
        projected: list[_EdgeCandidate],
        hydration_context: GraphHydrationContext,
        usage_signals_enabled: bool,
        now: datetime,
        scan: _ObservationScanState,
        induced_only: bool = False,
    ) -> list[_EdgeCandidate]:
        """Enrich one current scope without creating a second evidence budget."""

        self._observations_for_seeds(
            session,
            workspace_id,
            request,
            actor=actor,
            through=None,
            hydration_context=hydration_context,
            usage_signals_enabled=usage_signals_enabled,
            seed_nodes=frontier,
            induced_only=induced_only,
            scan=scan,
        )
        observed, _nodes = self._observation_scene(
            session,
            workspace_id,
            request=request,
            actor=actor,
            seed_nodes=frontier,
            active_layers=active_layers,
            observations=scan.ordered(),
            at=now,
            hydration_context=hydration_context,
        )
        return self._ranked_observations(
            session,
            workspace_id,
            actor=actor,
            observed=observed,
            projected=projected,
            now=now,
        )

    @staticmethod
    def _primary_placement_folders(
        session: Session,
        workspace_id: UUID,
        *,
        nodes: dict[str, GraphNode],
        hydration_context: GraphHydrationContext,
    ) -> tuple[dict[str, GraphNode], dict[str, set[str]], set[str]]:
        """Resolve only selected documents' visible primary folder ancestors."""

        document_refs = {
            (node.entity_kind, node.entity_id): node
            for node in nodes.values()
            if node.entity_kind in {"context_page", "library_item"}
        }
        if not document_refs:
            return {}, defaultdict(set), set()
        placements = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.primary_placement.is_(True),
                WorkspaceTreeNode.visibility_status == "visible",
                WorkspaceTreeNode.target_kind.in_(("context_page", "library_item")),
                WorkspaceTreeNode.target_id.in_(
                    [entity_id for _kind, entity_id in document_refs]
                ),
            )
        ).all()
        visible = hydration_context.visible_tree_nodes(
            [placement.id for placement in placements]
        )
        folders: dict[str, GraphNode] = {}
        memberships: dict[str, set[str]] = defaultdict(set)
        placed_documents: set[str] = set()
        for placement in sorted(placements, key=lambda item: str(item.id)):
            if placement.id not in visible or placement.target_id is None:
                continue
            document = document_refs.get((placement.target_kind, placement.target_id))
            if document is None:
                continue
            parent_id = placement.parent_id
            seen: set[UUID] = set()
            while parent_id is not None and parent_id not in seen:
                seen.add(parent_id)
                parent = hydration_context.visible_tree_nodes([parent_id]).get(
                    parent_id
                )
                if parent is None:
                    break
                if parent.node_kind == "folder":
                    folder = GraphNode(
                        id=graph_node_id("workspace_tree_node", parent.id),
                        entity_kind="workspace_tree_node",
                        entity_id=parent.id,
                        family="structural",
                        label=parent.title[:200],
                    )
                    folders[folder.id] = folder
                    memberships[folder.id].add(document.id)
                    placed_documents.add(document.id)
                    break
                parent_id = parent.parent_id
        return folders, memberships, placed_documents

    def _induced_projection_edges(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        actor: ActorContext | None,
        nodes: dict[str, GraphNode],
        hop_by_id: dict[str, int],
        active_layers: set[str],
        usage_signals_enabled: bool,
        now: datetime,
    ) -> tuple[list[_EdgeCandidate], list[str]]:
        """Read a bounded candidate window in every induced relation class.

        A global first-N window would allow a dense H1 mesh to erase folder,
        seed, or H2 relations before the display quotas see them. We cap each
        class independently, fetch one overflow sentinel, and mark the scene
        as a bounded sample whenever a class may have lost higher-ranked rows.
        """

        allowed_kinds = tuple(
            sorted(
                kind
                for kind, semantic in EDGE_SEMANTICS.items()
                if semantic.default_visible
                and semantic.layer in active_layers
                and (not request.edge_kinds or kind in request.edge_kinds)
                and (usage_signals_enabled or kind != "co_access")
                and (
                    not request.action_kinds
                    or semantic.action_kind is None
                    or semantic.action_kind in request.action_kinds
                )
            )
        )
        if not allowed_kinds or len(nodes) < 2:
            return [], []
        selected = (
            values(
                column("kind", String(64)),
                column("id", Uuid(as_uuid=True)),
                column("hop", Integer()),
                column("family", String(32)),
                column("is_seed", Boolean()),
            )
            .data(
                [
                    (
                        node.entity_kind,
                        node.entity_id,
                        hop_by_id[node.id],
                        node.family,
                        node.is_seed,
                    )
                    for node in sorted(nodes.values(), key=_entity_key)
                ]
            )
            .cte("selected_nodes")
        )
        sources = selected.alias("chosen_source")
        targets = selected.alias("chosen_target")
        relation_class = case(
            (
                and_(
                    EdgeProjection.edge_kind == "tree",
                    or_(
                        sources.c.family == "structural",
                        targets.c.family == "structural",
                    ),
                ),
                literal("folder"),
            ),
            (
                or_(sources.c.is_seed.is_(True), targets.c.is_seed.is_(True)),
                literal("seed"),
            ),
            (and_(sources.c.hop == 1, targets.c.hop == 1), literal("h1")),
            (
                or_(
                    and_(sources.c.hop == 1, targets.c.hop == 2),
                    and_(sources.c.hop == 2, targets.c.hop == 1),
                ),
                literal("h12"),
            ),
            (and_(sources.c.hop == 2, targets.c.hop == 2), literal("h2")),
            else_=literal("h1"),
        )
        # Four candidate rows per display slot usually leaves ranking ample
        # room; the 128-row minimum keeps tiny edge budgets from omitting a
        # relation class after only one or two raw-weight rows. This is still
        # a sample, not an exact top-rank claim, when its sentinel appears.
        class_cap = max(128, 4 * request.budget.edges)
        ranked = (
            select(
                EdgeProjection,
                relation_class.label("relation_class"),
                func.row_number()
                .over(
                    partition_by=relation_class,
                    order_by=(
                        EdgeProjection.weight.desc(),
                        EdgeProjection.occurred_at.desc().nulls_last(),
                        EdgeProjection.edge_kind,
                        EdgeProjection.source_kind,
                        EdgeProjection.source_id,
                        EdgeProjection.target_kind,
                        EdgeProjection.target_id,
                    ),
                )
                .label("rn"),
            )
            .join(
                sources,
                and_(
                    EdgeProjection.source_kind == sources.c.kind,
                    EdgeProjection.source_id == sources.c.id,
                ),
            )
            .join(
                targets,
                and_(
                    EdgeProjection.target_kind == targets.c.kind,
                    EdgeProjection.target_id == targets.c.id,
                ),
            )
            .where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind.in_(allowed_kinds),
                graph_neighbor_visibility_predicate(
                    EdgeProjection.source_kind,
                    EdgeProjection.source_id,
                    workspace_id=workspace_id,
                    actor=actor,
                    allow_tree_fallback=True,
                ),
                graph_neighbor_visibility_predicate(
                    EdgeProjection.target_kind,
                    EdgeProjection.target_id,
                    workspace_id=workspace_id,
                    actor=actor,
                    allow_tree_fallback=True,
                ),
            )
        )
        if request.time.from_ is not None:
            ranked = ranked.where(EdgeProjection.occurred_at >= request.time.from_)
        if request.time.to is not None:
            ranked = ranked.where(EdgeProjection.occurred_at <= request.time.to)
        if request.direction != "both":
            # Direction must be applied before the per-class row window. A
            # high-weight inbound seed flood must not spend the candidate
            # slots of an outbound query (or vice versa).
            undirected_kinds = tuple(
                kind for kind in allowed_kinds if not EDGE_SEMANTICS[kind].directed
            )
            source_structural = sources.c.family == "structural"
            target_structural = targets.c.family == "structural"
            mixed_structural = or_(
                and_(source_structural, ~target_structural),
                and_(~source_structural, target_structural),
            )
            directed_match = or_(
                and_(
                    mixed_structural,
                    target_structural
                    if request.direction == "out"
                    else source_structural,
                ),
                and_(
                    ~mixed_structural,
                    sources.c.hop <= targets.c.hop
                    if request.direction == "out"
                    else targets.c.hop <= sources.c.hop,
                ),
            )
            ranked = ranked.where(
                or_(
                    EdgeProjection.edge_kind.in_(undirected_kinds),
                    directed_match,
                )
            )
        ranked = ranked.subquery()
        projected = aliased(EdgeProjection, ranked)
        scanned = session.execute(
            select(projected, ranked.c.relation_class, ranked.c.rn)
            .where(ranked.c.rn <= class_cap + 1)
            .order_by(ranked.c.relation_class, ranked.c.rn)
            .execution_options(graph_rank_phase="induced_relations")
        ).all()
        source_capped = sorted(
            {relation_class for _row, relation_class, rn in scanned if rn > class_cap}
        )
        rows = [row for row, _relation_class, rn in scanned if rn <= class_cap]
        denominators = self.graph_service.relation_rank_denominators(
            session,
            workspace_id,
            actor=actor,
            sources=tuple(
                sorted(
                    {
                        (row.source_kind, row.source_id)
                        for row in rows
                        if row.edge_kind in DENOMINATOR_KINDS
                    },
                    key=lambda ref: (ref[0], str(ref[1])),
                )
            ),
        )
        result: list[_EdgeCandidate] = []
        for row in rows:
            source = nodes[graph_node_id(row.source_kind, row.source_id)]
            target = nodes[graph_node_id(row.target_kind, row.target_id)]
            semantic = edge_semantic(row.edge_kind)
            edge = GraphEdge(
                id=graph_edge_id(
                    workspace_id, source.id, target.id, row.edge_kind, "current"
                ),
                source=source.id,
                target=target.id,
                edge_kind=row.edge_kind,
                action_kind=semantic.action_kind,
                label=semantic.label,
                layer=semantic.layer,
                provenance=row.provenance,
                direction="directed" if semantic.directed else "undirected",
                weight=max(0.0, float(row.weight)),
                occurred_at=row.occurred_at,
                aggregate=row.edge_kind == "co_access",
                support_session_count=row.support_session_count,
                first_observed_at=row.first_observed_at,
                rank_score=rank_score(
                    row.edge_kind,
                    effective_weight(
                        row.edge_kind,
                        float(row.weight),
                        denominators.get((row.source_kind, row.source_id)),
                    ),
                    row.occurred_at,
                    now,
                ),
            )
            anchor, role = _anchor_and_role(edge, nodes)
            result.append(
                _EdgeCandidate(
                    edge=edge,
                    nodes=(source, target),
                    priority=0,
                    semantic_identity=(
                        edge.source,
                        edge.target,
                        edge.edge_kind,
                        edge.action_kind,
                    ),
                    anchor=anchor,
                    role=role,
                )
            )
        return result, source_capped

    def _ranked_observations(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        actor: ActorContext | None,
        observed: list[_EdgeCandidate],
        projected: list[_EdgeCandidate],
        now: datetime,
    ) -> list[_EdgeCandidate]:
        """Give observed relations the rank their current relation has.

        An observed edge that is also a current projection edge takes that
        edge's rank. Others are ranked from their recorded weight with the
        same actor-visible denominators, so F13 widths and F11 label sums
        compare one unit across tiers.
        """

        if not observed:
            return observed
        current = {
            candidate.semantic_identity: candidate.edge.rank_score
            for candidate in projected
            if candidate.priority == 0
        }
        # nodes[0] is the edge's stored source, which the denominators count.
        sources = {
            (candidate.nodes[0].entity_kind, candidate.nodes[0].entity_id)
            for candidate in observed
            if candidate.semantic_identity not in current
            and candidate.edge.edge_kind in DENOMINATOR_KINDS
        }
        denominators = (
            self.graph_service.relation_rank_denominators(
                session,
                workspace_id,
                actor=actor,
                sources=tuple(sorted(sources, key=lambda ref: (ref[0], str(ref[1])))),
            )
            if sources
            else {}
        )
        ranked: list[_EdgeCandidate] = []
        for candidate in observed:
            edge = candidate.edge
            rank = current.get(candidate.semantic_identity)
            if rank is None:
                source = candidate.nodes[0]
                rank = rank_score(
                    edge.edge_kind,
                    effective_weight(
                        edge.edge_kind,
                        edge.weight,
                        denominators.get((source.entity_kind, source.entity_id)),
                    ),
                    edge.occurred_at,
                    now,
                )
            ranked.append(
                replace(candidate, edge=edge.model_copy(update={"rank_score": rank}))
            )
        return ranked

    def _domain_actions(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        actor: ActorContext | None,
        seed_nodes: dict[str, GraphNode],
        active_layers: set[str],
    ) -> tuple[
        list[_EdgeCandidate],
        dict[str, GraphNode],
        list[_TimelineDraft],
        bool,
    ]:
        # Every domain action is modeled event/source -> seed, so it is
        # seed-relative inbound. Session -> event edges only contextualize that
        # same inbound path; they are not an independent both-direction path.
        # Gate before querying any domain table or constructing timeline rows.
        if request.direction == "out":
            return [], {}, [], False

        seed_by_id = {node.entity_id: node for node in seed_nodes.values()}
        seed_ids = list(seed_by_id)
        candidates: list[_EdgeCandidate] = []
        nodes: dict[str, GraphNode] = {}
        timeline: list[_TimelineDraft] = []
        source_limit = min(1_000, request.budget.nodes + request.budget.edges)
        capped = False

        version_statement = (
            select(ContextPageVersion)
            .where(ContextPageVersion.page_id.in_(seed_ids))
            .order_by(ContextPageVersion.created_at, ContextPageVersion.id)
        )
        if not actor_can_read_drafts(actor):
            version_statement = version_statement.where(
                ContextPageVersion.version_status.in_(
                    PUBLISHED_CONTEXT_VERSION_STATUSES
                )
            )
        version_statement = _apply_time_window(
            version_statement,
            ContextPageVersion.created_at,
            request,
        )
        version_rows = session.scalars(version_statement.limit(source_limit + 1)).all()
        capped = capped or len(version_rows) > source_limit
        versions = version_rows[:source_limit]
        for version in versions:
            if not context_version_is_visible(version, actor):
                continue
            action_kind = "created" if version.version_no == 1 else "edited"
            self._append_domain_action(
                workspace_id,
                request=request,
                active_layers=active_layers,
                seed=seed_by_id[version.page_id],
                source_kind="context_page_version",
                source_id=version.id,
                source_label=f"Version {version.version_no}",
                source_status=version.version_status,
                occurred_at=version.created_at,
                action_kind=action_kind,
                evidence_kind="context_page_version",
                evidence_id=version.id,
                candidates=candidates,
                nodes=nodes,
                timeline=timeline,
                timeline_operation=(
                    "node_created"
                    if request.time.mode == "appearance" and version.version_no == 1
                    else "action_occurred"
                ),
            )

        if request.time.mode == "action":
            ask_statement = (
                select(AskTurnCitation, AskTurn, AskSession)
                .join(AskTurn, AskTurnCitation.ask_turn_id == AskTurn.id)
                .join(AskSession, AskTurn.ask_session_id == AskSession.id)
                .where(
                    AskSession.workspace_id == workspace_id,
                    AskTurnCitation.target_kind == "context_page",
                    AskTurnCitation.target_id.in_(seed_ids),
                )
                .order_by(AskTurnCitation.created_at, AskTurnCitation.id)
            )
            ask_statement = _apply_time_window(
                ask_statement,
                AskTurnCitation.created_at,
                request,
            )
            ask_page = session.execute(ask_statement.limit(source_limit + 1)).all()
            capped = capped or len(ask_page) > source_limit
            ask_rows = ask_page[:source_limit]
            for citation, turn, ask_session in ask_rows:
                self._append_domain_action(
                    workspace_id,
                    request=request,
                    active_layers=active_layers,
                    seed=seed_by_id[citation.target_id],
                    source_kind="ask_turn",
                    source_id=turn.id,
                    source_label=f"Ask turn {turn.turn_no}",
                    source_status="evidence_included",
                    occurred_at=citation.created_at,
                    action_kind="included",
                    evidence_kind="ask_turn_citation",
                    evidence_id=citation.id,
                    candidates=candidates,
                    nodes=nodes,
                    timeline=timeline,
                )
                event_node = nodes.get(graph_node_id("ask_turn", turn.id))
                if event_node is not None:
                    self._append_session_relation(
                        workspace_id,
                        request=request,
                        active_layers=active_layers,
                        session_kind="ask_session",
                        session_id=ask_session.id,
                        session_label="Ask session",
                        session_status=(
                            "ended" if ask_session.ended_at is not None else "active"
                        ),
                        occurred_at=ask_session.started_at,
                        event_node=event_node,
                        action_kind="included",
                        evidence_kind="ask_turn_citation",
                        evidence_id=citation.id,
                        candidates=candidates,
                        nodes=nodes,
                    )

            audit_sources_allowed = actor_can_read_audit_records(
                actor
            ) and _actor_role_allows(actor, "editor")
            mcp_rows = []
            if audit_sources_allowed:
                mcp_statement = (
                    select(
                        McpChatToolCallArtifact,
                        MCPToolCall,
                        McpChatSession,
                    )
                    .join(
                        MCPToolCall,
                        McpChatToolCallArtifact.mcp_tool_call_id == MCPToolCall.id,
                    )
                    .join(
                        McpChatSession,
                        McpChatToolCallArtifact.chat_session_id == McpChatSession.id,
                    )
                    .where(
                        MCPToolCall.workspace_id == workspace_id,
                        McpChatSession.workspace_id == workspace_id,
                        McpChatToolCallArtifact.workspace_id == workspace_id,
                        McpChatToolCallArtifact.resource_kind == "context_page",
                        McpChatToolCallArtifact.resource_id.in_(seed_ids),
                        McpChatToolCallArtifact.verb.in_(tuple(_MCP_ACTIONS)),
                    )
                    .order_by(
                        McpChatToolCallArtifact.occurred_at,
                        McpChatToolCallArtifact.id,
                    )
                )
                mcp_statement = _apply_time_window(
                    mcp_statement,
                    McpChatToolCallArtifact.occurred_at,
                    request,
                )
                mcp_page = session.execute(mcp_statement.limit(source_limit + 1)).all()
                capped = capped or len(mcp_page) > source_limit
                mcp_rows = mcp_page[:source_limit]
            for artifact, tool_call, chat_session in mcp_rows:
                action_kind = _MCP_ACTIONS.get(artifact.verb)
                if action_kind is None:
                    continue
                self._append_domain_action(
                    workspace_id,
                    request=request,
                    active_layers=active_layers,
                    seed=seed_by_id[artifact.resource_id],
                    source_kind="mcp_tool_call",
                    source_id=tool_call.id,
                    source_label=f"MCP {tool_call.tool_name}"[:200],
                    source_status=tool_call.result_status,
                    occurred_at=artifact.occurred_at,
                    action_kind=action_kind,
                    evidence_kind="mcp_tool_call_artifact",
                    evidence_id=artifact.id,
                    candidates=candidates,
                    nodes=nodes,
                    timeline=timeline,
                )
                event_node = nodes.get(graph_node_id("mcp_tool_call", tool_call.id))
                if event_node is not None:
                    self._append_session_relation(
                        workspace_id,
                        request=request,
                        active_layers=active_layers,
                        session_kind="mcp_chat_session",
                        session_id=chat_session.id,
                        session_label="MCP chat",
                        session_status=(
                            "archived"
                            if chat_session.archived_at is not None
                            else "active"
                        ),
                        occurred_at=chat_session.started_at,
                        event_node=event_node,
                        action_kind="tool_produced",
                        evidence_kind="mcp_tool_call_artifact",
                        evidence_id=artifact.id,
                        candidates=candidates,
                        nodes=nodes,
                    )

            change_rows = []
            if audit_sources_allowed:
                allowed_change_operations = tuple(
                    operation
                    for operation, action_kind in _CHANGE_ACTIONS.items()
                    if not request.action_kinds or action_kind in request.action_kinds
                )
                change_statement = (
                    select(ChangeOperation, ChangeSet)
                    .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
                    .where(
                        ChangeSet.workspace_id == workspace_id,
                        ChangeOperation.target_type == "context_page",
                        ChangeOperation.target_id.in_(seed_ids),
                        ChangeOperation.operation.in_(allowed_change_operations),
                    )
                    .order_by(ChangeOperation.created_at, ChangeOperation.id)
                )
                change_statement = _apply_time_window(
                    change_statement,
                    ChangeOperation.created_at,
                    request,
                )
                change_page = session.execute(
                    change_statement.limit(source_limit + 1)
                ).all()
                capped = capped or len(change_page) > source_limit
                change_rows = change_page[:source_limit]
            for operation, _change_set in change_rows:
                action_kind = _CHANGE_ACTIONS.get(operation.operation)
                if action_kind is None:
                    continue
                self._append_domain_action(
                    workspace_id,
                    request=request,
                    active_layers=active_layers,
                    seed=seed_by_id[operation.target_id],
                    source_kind="change_operation",
                    source_id=operation.id,
                    source_label=f"Change: {action_semantic(action_kind).label}",
                    source_status=operation.operation,
                    occurred_at=operation.created_at,
                    action_kind=action_kind,
                    evidence_kind="change_operation",
                    evidence_id=operation.id,
                    candidates=candidates,
                    nodes=nodes,
                    timeline=timeline,
                )

            # ReviewDecision identifies a ReviewItem/ChangeSet, not necessarily
            # the mutable ContextPage head. Until ``diff_ref`` has a typed,
            # actor-authorized immutable-version resolver, emitting
            # decision -> current page would overstate what was reviewed.

            if audit_sources_allowed and _actor_role_allows(actor, "owner"):
                audit_statement = (
                    select(AuditEvent)
                    .where(
                        AuditEvent.workspace_id == workspace_id,
                        AuditEvent.target_kind == "context_page",
                        AuditEvent.target_id.in_(seed_ids),
                    )
                    .order_by(AuditEvent.created_at, AuditEvent.id)
                )
                audit_statement = _apply_time_window(
                    audit_statement,
                    AuditEvent.created_at,
                    request,
                )
                audit_page = session.scalars(
                    audit_statement.limit(source_limit + 1)
                ).all()
                capped = capped or len(audit_page) > source_limit
                audit_events = audit_page[:source_limit]
                for audit_event in audit_events:
                    action_kind = _audit_action(audit_event.action)
                    if action_kind is None:
                        continue
                    self._append_domain_action(
                        workspace_id,
                        request=request,
                        active_layers=active_layers,
                        seed=seed_by_id[audit_event.target_id],
                        source_kind="audit_event",
                        source_id=audit_event.id,
                        source_label=f"Audit: {action_semantic(action_kind).label}",
                        source_status=audit_event.retention_class,
                        occurred_at=audit_event.created_at,
                        action_kind=action_kind,
                        evidence_kind="audit_event",
                        evidence_id=audit_event.id,
                        candidates=candidates,
                        nodes=nodes,
                        timeline=timeline,
                    )

        return candidates, nodes, timeline, capped

    @staticmethod
    def _append_session_relation(
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        active_layers: set[str],
        session_kind: str,
        session_id: UUID,
        session_label: str,
        session_status: str,
        occurred_at: datetime,
        event_node: GraphNode,
        action_kind: str,
        evidence_kind: str,
        evidence_id: UUID,
        candidates: list[_EdgeCandidate],
        nodes: dict[str, GraphNode],
    ) -> None:
        # This non-seed edge is subordinate to an inbound event -> seed path.
        if request.direction == "out":
            return
        if not _time_allowed(occurred_at, request):
            return
        semantic = action_semantic(action_kind)
        if action_kind not in ACTION_SEMANTICS or not semantic.default_visible:
            return
        if semantic.layer not in active_layers:
            return
        if request.action_kinds and action_kind not in request.action_kinds:
            return
        family = NODE_FAMILY_BY_KIND.get(session_kind)
        if family not in {"chat", "ask"}:
            return
        session_node = GraphNode(
            id=graph_node_id(session_kind, session_id),
            entity_kind=session_kind,
            entity_id=session_id,
            family=family,
            label=session_label,
            status=session_status,
            occurred_at=occurred_at,
            evidence=GraphEvidenceRef(
                kind=evidence_kind,
                available=False,
            ),
        )
        if not _node_filter_allows(session_node, request):
            return
        evidence = GraphEvidenceRef(
            kind=evidence_kind,
            available=False,
        )
        edge = GraphEdge(
            id=graph_edge_id(
                workspace_id,
                session_node.id,
                event_node.id,
                action_kind,
                f"domain:{evidence_kind}:{evidence_id}",
            ),
            source=session_node.id,
            target=event_node.id,
            edge_kind=action_kind,
            action_kind=action_kind,
            label=semantic.label,
            layer=semantic.layer,
            provenance="domain",
            direction="directed",
            weight=1.0,
            occurred_at=occurred_at,
            evidence=evidence,
        )
        candidates.append(
            _EdgeCandidate(
                edge=edge,
                nodes=(session_node, event_node),
                priority=2,
                semantic_identity=(
                    edge.source,
                    edge.target,
                    edge.edge_kind,
                    action_kind,
                ),
                anchor=None,
                role="context",
            )
        )
        nodes[session_node.id] = session_node

    @staticmethod
    def _append_domain_action(
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        active_layers: set[str],
        seed: GraphNode,
        source_kind: str,
        source_id: UUID,
        source_label: str,
        source_status: str | None,
        occurred_at: datetime,
        action_kind: str,
        evidence_kind: str,
        evidence_id: UUID,
        candidates: list[_EdgeCandidate],
        nodes: dict[str, GraphNode],
        timeline: list[_TimelineDraft],
        timeline_operation: str = "action_occurred",
    ) -> None:
        if request.direction == "out":
            return
        semantic = action_semantic(action_kind)
        if action_kind not in ACTION_SEMANTICS or not semantic.default_visible:
            return
        if request.action_kinds and action_kind not in request.action_kinds:
            return
        if semantic.layer not in active_layers:
            return
        if not _time_allowed(occurred_at, request):
            return

        family = NODE_FAMILY_BY_KIND.get(source_kind)
        if family is None:
            return
        source = GraphNode(
            id=graph_node_id(source_kind, source_id),
            entity_kind=source_kind,
            entity_id=source_id,
            family=family,
            label=source_label[:200] or source_kind.replace("_", " "),
            status=source_status,
            occurred_at=occurred_at,
            evidence=GraphEvidenceRef(
                kind=evidence_kind,
                available=False,
            ),
        )
        if not _node_filter_allows(source, request):
            return
        edge_id = graph_edge_id(
            workspace_id,
            source.id,
            seed.id,
            action_kind,
            f"domain:{evidence_kind}:{evidence_id}",
        )
        evidence = GraphEvidenceRef(
            kind=evidence_kind,
            available=False,
        )
        edge = GraphEdge(
            id=edge_id,
            source=source.id,
            target=seed.id,
            edge_kind=action_kind,
            action_kind=action_kind,
            label=semantic.label,
            layer=semantic.layer,
            provenance="domain",
            direction="directed",
            weight=1.0,
            occurred_at=occurred_at,
            evidence=evidence,
        )
        candidates.append(
            _EdgeCandidate(
                edge=edge,
                nodes=(source, seed),
                priority=2,
                semantic_identity=(
                    edge.source,
                    edge.target,
                    edge.edge_kind,
                    action_kind,
                ),
                anchor=seed.id,
                role="in",
            )
        )
        nodes[source.id] = source
        timeline.append(
            _TimelineDraft(
                identity=f"{evidence_kind}:{evidence_id}:{action_kind}",
                operation=timeline_operation,
                action_kind=action_kind,
                label=semantic.label,
                occurred_at=occurred_at,
                node_ids=(source.id, seed.id),
                edge_ids=(edge.id,),
                evidence=evidence,
                node_snapshots=(source, seed),
            )
        )

    def _observations_for_seeds(
        self,
        session: Session,
        workspace_id: UUID,
        request: GraphQueryRequest,
        *,
        actor: ActorContext | None,
        through: datetime | None,
        hydration_context: GraphHydrationContext,
        usage_signals_enabled: bool,
        seed_nodes: dict[str, GraphNode] | None = None,
        induced_only: bool = False,
        scan: _ObservationScanState | None = None,
    ) -> tuple[list[GraphObservation], bool]:
        seed_refs = (
            [(node.entity_kind, node.entity_id) for node in seed_nodes.values()]
            if seed_nodes is not None
            else [(seed.kind, seed.id) for seed in request.seeds]
        )
        if not seed_refs:
            return [], False
        if scan is None:
            scan = _ObservationScanState(
                visible_limit=min(1_000, request.budget.nodes + request.budget.edges)
            )
        if scan.capped:
            return [], True
        page_size = min(512, max(128, scan.visible_limit * 2))

        active_layers = set(request.layers) & _PRESET_LAYERS[request.preset]
        allowed_edge_kinds = {
            edge_kind
            for edge_kind, semantic in EDGE_SEMANTICS.items()
            if semantic.default_visible
            and semantic.layer in active_layers
            and (not request.edge_kinds or edge_kind in request.edge_kinds)
            and (
                not request.action_kinds
                or semantic.action_kind is None
                or semantic.action_kind in request.action_kinds
            )
        }
        # Disabled usage history must not consume the visible tail and evict
        # authored creations, especially when a current occurrence period
        # relies on observations for undated wikilink projections.
        if not usage_signals_enabled:
            allowed_edge_kinds.discard("co_access")
        allowed_action_kinds = {
            action_kind
            for action_kind, semantic in ACTION_SEMANTICS.items()
            if request.time.mode == "action"
            and semantic.layer in active_layers
            and (not request.action_kinds or action_kind in request.action_kinds)
        }
        semantic_predicates = []
        if allowed_edge_kinds:
            semantic_predicates.append(
                and_(
                    GraphObservation.operation.in_(("edge_created", "edge_removed")),
                    GraphObservation.edge_kind.in_(tuple(sorted(allowed_edge_kinds))),
                )
            )
        if allowed_action_kinds:
            semantic_predicates.append(
                and_(
                    GraphObservation.operation == "action_occurred",
                    GraphObservation.action_kind.in_(
                        tuple(sorted(allowed_action_kinds))
                    ),
                )
            )
        if not semantic_predicates:
            return [], False

        target_visible = graph_neighbor_visibility_predicate(
            GraphObservation.target_kind,
            GraphObservation.target_id,
            workspace_id=workspace_id,
            actor=actor,
            allow_tree_fallback=True,
        )
        source_visible = graph_neighbor_visibility_predicate(
            GraphObservation.source_kind,
            GraphObservation.source_id,
            workspace_id=workspace_id,
            actor=actor,
            allow_tree_fallback=True,
        )
        source_seed_branch = and_(
            tuple_(GraphObservation.source_kind, GraphObservation.source_id).in_(
                seed_refs
            ),
            or_(
                and_(
                    GraphObservation.target_kind.is_(None),
                    GraphObservation.target_id.is_(None),
                ),
                target_visible,
            ),
        )
        target_seed_branch = and_(
            tuple_(GraphObservation.target_kind, GraphObservation.target_id).in_(
                seed_refs
            ),
            source_visible,
        )
        endpoint_predicates = []
        if request.direction in {"out", "both"}:
            endpoint_predicates.append(source_seed_branch)
        if request.direction in {"in", "both"}:
            endpoint_predicates.append(target_seed_branch)
        base_statement = select(GraphObservation).where(
            GraphObservation.workspace_id == workspace_id,
            or_(*endpoint_predicates),
            or_(*semantic_predicates),
        )
        if induced_only:
            selected_endpoints = (
                tuple_(GraphObservation.source_kind, GraphObservation.source_id).in_(
                    seed_refs
                ),
                tuple_(GraphObservation.target_kind, GraphObservation.target_id).in_(
                    seed_refs
                ),
            )
            base_statement = base_statement.where(*selected_endpoints)
        if scan.scanned_ids:
            # The same immutable event can touch two successive frontiers.
            # Re-reading it must not spend the one request-wide scan budget.
            unread_events = GraphObservation.id.not_in(
                tuple(sorted(scan.scanned_ids, key=str))
            )
            base_statement = base_statement.where(unread_events)
        if through is not None:
            base_statement = base_statement.where(
                GraphObservation.occurred_at <= through
            )
        if request.time.mode != "current":
            base_statement = _apply_time_window(
                base_statement,
                GraphObservation.occurred_at,
                request,
            )
        # Current state must fold the available creation/removal tail before
        # selecting an occurrence period. Filtering events here can discard a
        # later removal (or re-add) and resurrect an old authored relation.
        # History modes still select their period-scoped event catalog; the
        # same bounded scan and source-cap warnings apply in every mode.

        visible: list[GraphObservation] = []
        last_key: tuple[datetime, int] | None = None
        raw_scan_capped = False
        source_cap_events_before = hydration_context.source_cap_events
        while len(scan.visible) <= scan.visible_limit:
            remaining_raw = GRAPH_OBSERVATION_RAW_SCAN_LIMIT - scan.raw_scanned
            remaining_pages = GRAPH_OBSERVATION_PAGE_LIMIT - scan.pages_scanned
            if remaining_raw <= 0 or remaining_pages <= 0:
                raw_scan_capped = True
                break
            fetch_size = min(page_size, remaining_raw)
            statement = base_statement
            if last_key is not None:
                last_occurred_at, last_sequence = last_key
                statement = statement.where(
                    or_(
                        GraphObservation.occurred_at < last_occurred_at,
                        and_(
                            GraphObservation.occurred_at == last_occurred_at,
                            GraphObservation.workspace_sequence < last_sequence,
                        ),
                    )
                )
            page = list(
                session.scalars(
                    statement.order_by(
                        GraphObservation.occurred_at.desc(),
                        GraphObservation.workspace_sequence.desc(),
                    ).limit(fetch_size)
                ).all()
            )
            scan.pages_scanned += 1
            scan.raw_scanned += len(page)
            scan.scanned_ids.update(row.id for row in page)
            if not page:
                break

            endpoint_refs = {
                (observation.source_kind, observation.source_id) for observation in page
            } | {
                (observation.target_kind, observation.target_id)
                for observation in page
                if observation.target_kind is not None
                and observation.target_id is not None
            }
            hydrated = self.graph_service.hydrate_entities(
                session,
                workspace_id,
                entities=list(endpoint_refs),
                actor=actor,
                hydration_context=hydration_context,
            )
            for observation in page:
                source_ref = (observation.source_kind, observation.source_id)
                if source_ref not in hydrated:
                    continue
                if (
                    observation.target_kind is not None
                    and observation.target_id is not None
                    and (observation.target_kind, observation.target_id) not in hydrated
                ):
                    continue
                visible.append(observation)
                scan.visible.append(observation)
                if len(scan.visible) > scan.visible_limit:
                    break

            last = page[-1]
            last_key = (last.occurred_at, last.workspace_sequence)
            if len(page) < fetch_size:
                break
            if len(scan.visible) <= scan.visible_limit and (
                scan.raw_scanned >= GRAPH_OBSERVATION_RAW_SCAN_LIMIT
                or scan.pages_scanned >= GRAPH_OBSERVATION_PAGE_LIMIT
            ):
                raw_scan_capped = True
                break

        capped = (
            raw_scan_capped
            or len(scan.visible) > scan.visible_limit
            or hydration_context.source_cap_events > source_cap_events_before
        )
        scan.capped = scan.capped or capped
        if len(scan.visible) > scan.visible_limit:
            sentinel = scan.visible.pop()
            visible.remove(sentinel)
        bounded = sorted(
            visible, key=lambda row: (row.occurred_at, row.workspace_sequence)
        )
        return bounded, capped

    @staticmethod
    def _admit_current_observations(
        session: Session,
        workspace_id: UUID,
        *,
        actor: ActorContext | None,
        active: dict[_ObservationIdentity, GraphObservation],
    ) -> dict[_ObservationIdentity, GraphObservation]:
        """Keep authored evidence only when current visible state admits it.

        A rebuilt projection can correct an earlier extractor's claim without
        rewriting immutable history. Activity observations, whose semantics
        are not curated, need not have a current projection row.
        """

        curated = {
            identity
            for identity in active
            if edge_semantic(identity[4]).provenance == "curated"
        }
        if not curated:
            return active
        projected = set(
            session.execute(
                select_projection_in_workspace(
                    EdgeProjection,
                    workspace_id=workspace_id,
                    columns=(
                        EdgeProjection.source_kind,
                        EdgeProjection.source_id,
                        EdgeProjection.target_kind,
                        EdgeProjection.target_id,
                        EdgeProjection.edge_kind,
                    ),
                    predicates=(
                        tuple_(
                            EdgeProjection.source_kind,
                            EdgeProjection.source_id,
                            EdgeProjection.target_kind,
                            EdgeProjection.target_id,
                            EdgeProjection.edge_kind,
                        ).in_(
                            sorted(
                                curated, key=lambda identity: tuple(map(str, identity))
                            )
                        ),
                        graph_neighbor_visibility_predicate(
                            EdgeProjection.source_kind,
                            EdgeProjection.source_id,
                            workspace_id=workspace_id,
                            actor=actor,
                            allow_tree_fallback=True,
                        ),
                        graph_neighbor_visibility_predicate(
                            EdgeProjection.target_kind,
                            EdgeProjection.target_id,
                            workspace_id=workspace_id,
                            actor=actor,
                            allow_tree_fallback=True,
                        ),
                    ),
                )
            ).all()
        )
        return {
            identity: observation
            for identity, observation in active.items()
            if identity not in curated or identity in projected
        }

    def _observation_scene(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        actor: ActorContext | None,
        seed_nodes: dict[str, GraphNode],
        active_layers: set[str],
        observations: list[GraphObservation],
        at: datetime,
        hydration_context: GraphHydrationContext,
    ) -> tuple[list[_EdgeCandidate], dict[str, GraphNode]]:
        active: dict[_ObservationIdentity, GraphObservation] = {}
        seed_refs = {(node.entity_kind, node.entity_id) for node in seed_nodes.values()}
        for observation in observations:
            if not _observation_direction_allows(
                observation,
                seed_refs=seed_refs,
                direction=request.direction,
            ):
                continue
            if observation.operation not in {"edge_created", "edge_removed"}:
                continue
            if observation.target_kind is None or observation.target_id is None:
                continue
            if not observation.edge_kind:
                continue
            identity = (
                observation.source_kind,
                observation.source_id,
                observation.target_kind,
                observation.target_id,
                observation.edge_kind,
            )
            if observation.operation == "edge_removed":
                active.pop(identity, None)
            elif _observation_valid_at(observation, at):
                active[identity] = observation

        if request.time.mode == "current":
            # A surviving relationship belongs to its latest creation/re-add
            # occurrence, not an older creation or its later removal. The
            # period is inclusive and already normalized to UTC by the API.
            active = {
                identity: observation
                for identity, observation in active.items()
                if _time_allowed(observation.occurred_at, request)
            }
            active = self._admit_current_observations(
                session, workspace_id, actor=actor, active=active
            )

        endpoint_refs = {
            (observation.source_kind, observation.source_id)
            for observation in active.values()
        } | {
            (observation.target_kind, observation.target_id)
            for observation in active.values()
            if observation.target_kind is not None and observation.target_id is not None
        }
        hydrated = self.graph_service.hydrate_entities(
            session,
            workspace_id,
            entities=list(endpoint_refs),
            actor=actor,
            hydration_context=hydration_context,
        )
        seed_by_ref = {
            (node.entity_kind, node.entity_id): node for node in seed_nodes.values()
        }
        candidates: list[_EdgeCandidate] = []
        nodes: dict[str, GraphNode] = {}
        for identity, observation in active.items():
            if identity[:2] not in hydrated or identity[2:4] not in hydrated:
                continue
            semantic = edge_semantic(identity[4])
            if identity[4] not in EDGE_SEMANTICS or not semantic.default_visible:
                continue
            if request.edge_kinds and identity[4] not in request.edge_kinds:
                continue
            if semantic.layer not in active_layers:
                continue
            source = seed_by_ref.get(identity[:2]) or _node_from_hydrated(
                identity[0], identity[1], hydrated[identity[:2]]
            )
            target = seed_by_ref.get(identity[2:4]) or _node_from_hydrated(
                identity[2], identity[3], hydrated[identity[2:4]]
            )
            if not _node_filter_allows(source, request) or not _node_filter_allows(
                target, request
            ):
                continue
            edge_id = graph_edge_id(
                workspace_id,
                source.id,
                target.id,
                identity[4],
                "current",
            )
            evidence = GraphEvidenceRef(
                kind="graph_observation",
                available=False,
            )
            edge = GraphEdge(
                id=edge_id,
                source=source.id,
                target=target.id,
                edge_kind=identity[4],
                action_kind=semantic.action_kind,
                label=semantic.label,
                layer=semantic.layer,
                provenance="observed",
                direction="directed" if semantic.directed else "undirected",
                weight=max(0.0, float(observation.weight_delta or 1.0)),
                occurred_at=observation.occurred_at,
                sequence=observation.workspace_sequence,
                evidence=evidence,
            )
            anchor, role = _anchor_and_role(edge, seed_nodes)
            candidates.append(
                _EdgeCandidate(
                    edge=edge,
                    nodes=(source, target),
                    priority=1,
                    semantic_identity=(
                        edge.source,
                        edge.target,
                        edge.edge_kind,
                        edge.action_kind,
                    ),
                    anchor=anchor,
                    role=role,
                )
            )
            nodes[source.id] = source
            nodes[target.id] = target
        return candidates, nodes

    def _observation_timeline(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: GraphQueryRequest,
        actor: ActorContext | None,
        seed_nodes: dict[str, GraphNode],
        active_layers: set[str],
        observations: list[GraphObservation],
        hydration_context: GraphHydrationContext,
    ) -> list[_TimelineDraft]:
        endpoint_refs = {
            (observation.source_kind, observation.source_id)
            for observation in observations
        } | {
            (observation.target_kind, observation.target_id)
            for observation in observations
            if observation.target_kind is not None and observation.target_id is not None
        }
        hydrated = self.graph_service.hydrate_entities(
            session,
            workspace_id,
            entities=list(endpoint_refs),
            actor=actor,
            hydration_context=hydration_context,
        )
        seed_by_ref = {
            (node.entity_kind, node.entity_id): node for node in seed_nodes.values()
        }
        drafts: list[_TimelineDraft] = []
        seed_refs = {(seed.kind, seed.id) for seed in request.seeds}
        for observation in observations:
            if not _observation_direction_allows(
                observation,
                seed_refs=seed_refs,
                direction=request.direction,
            ):
                continue
            if not _time_allowed(observation.occurred_at, request):
                continue
            source_ref = (observation.source_kind, observation.source_id)
            if source_ref not in hydrated:
                continue
            source = seed_by_ref.get(source_ref) or _node_from_hydrated(
                source_ref[0], source_ref[1], hydrated[source_ref]
            )
            event_nodes = [source]
            target = None
            if (
                observation.target_kind is not None
                and observation.target_id is not None
            ):
                target_ref = (observation.target_kind, observation.target_id)
                if target_ref not in hydrated:
                    continue
                target = seed_by_ref.get(target_ref) or _node_from_hydrated(
                    target_ref[0], target_ref[1], hydrated[target_ref]
                )
                event_nodes.append(target)
            if any(not _node_filter_allows(node, request) for node in event_nodes):
                continue

            if observation.operation == "action_occurred":
                if request.time.mode != "action" or not observation.action_kind:
                    continue
                semantic = action_semantic(observation.action_kind)
                if observation.action_kind not in ACTION_SEMANTICS:
                    continue
                if (
                    request.action_kinds
                    and observation.action_kind not in request.action_kinds
                ):
                    continue
                action_kind = observation.action_kind
            else:
                if request.time.mode != "appearance" or not observation.edge_kind:
                    continue
                semantic = edge_semantic(observation.edge_kind)
                if (
                    observation.edge_kind not in EDGE_SEMANTICS
                    or not semantic.default_visible
                ):
                    continue
                if (
                    request.edge_kinds
                    and observation.edge_kind not in request.edge_kinds
                ):
                    continue
                action_kind = observation.action_kind or observation.operation
            if semantic.layer not in active_layers:
                continue
            edge_ids: tuple[str, ...] = ()
            if target is not None and observation.edge_kind:
                edge_ids = (
                    graph_edge_id(
                        workspace_id,
                        source.id,
                        target.id,
                        observation.edge_kind,
                        "current",
                    ),
                )
            evidence = GraphEvidenceRef(
                kind="graph_observation",
                available=False,
            )
            edge_snapshots: tuple[GraphEdge, ...] = ()
            node_snapshots: tuple[GraphNode, ...] = ()
            if (
                observation.operation == "edge_created"
                and target is not None
                and observation.edge_kind is not None
            ):
                edge_snapshots = (
                    GraphEdge(
                        id=edge_ids[0],
                        source=source.id,
                        target=target.id,
                        edge_kind=observation.edge_kind,
                        action_kind=semantic.action_kind,
                        label=semantic.label,
                        layer=semantic.layer,
                        provenance="observed",
                        direction="directed" if semantic.directed else "undirected",
                        weight=max(0.0, float(observation.weight_delta or 1.0)),
                        occurred_at=observation.occurred_at,
                        sequence=observation.workspace_sequence,
                        evidence=evidence,
                    ),
                )
                node_snapshots = tuple(event_nodes)
            drafts.append(
                _TimelineDraft(
                    identity=f"graph_observation:{observation.id}",
                    operation=observation.operation,
                    action_kind=action_kind,
                    label=semantic.label,
                    occurred_at=observation.occurred_at,
                    node_ids=tuple(node.id for node in event_nodes),
                    edge_ids=edge_ids,
                    evidence=evidence,
                    node_snapshots=node_snapshots,
                    edge_snapshots=edge_snapshots,
                    source_sequence=observation.workspace_sequence,
                )
            )
        return drafts

    @staticmethod
    def _apply_budget(
        *,
        request: GraphQueryRequest,
        seed_nodes: dict[str, GraphNode],
        candidates: list[_EdgeCandidate],
        extra_nodes: dict[str, GraphNode],
    ) -> tuple[list[GraphNode], list[GraphEdge], bool]:
        """Fill the budget tier by tier: fair shares first, then merit.

        Tiers keep strict priority (current, observed, domain). Within one,
        candidates queue by (anchor, kind, role) in rank order. Every seed
        with candidates, and then the relations no seed anchors, gets an
        equal share of the slots the tier can still fill, split evenly over
        its queues and taken round-robin, so one kind's flood cannot empty
        another kind or another seed. What the shares leave goes by rank.
        A candidate that does not fit is consumed; a repeat of a shown
        relation costs nothing.

        Relations no seed anchors (a session and one of its events) hang off
        an event already shown: they are offered after every seed's
        candidates, and one whose event is not in the scene is passed over.
        So they never spend the nodes an anchored share was sized for, and
        never leave a piece of the scene with no path to a seed.
        """

        selected_nodes = dict(seed_nodes)
        selected_edges: list[GraphEdge] = []
        truncated = False
        seen_semantics: set[tuple[str, str, str, str | None]] = set()

        def take(candidate: _EdgeCandidate) -> Outcome:
            nonlocal truncated
            if candidate.semantic_identity in seen_semantics:
                return "skipped"
            if candidate.anchor is None and candidate.nodes[1].id not in selected_nodes:
                return "skipped"
            unseen_nodes = {
                node.id: node
                for node in candidate.nodes
                if node.id not in selected_nodes
            }
            if len(selected_edges) >= request.budget.edges or (
                len(selected_nodes) + len(unseen_nodes) > request.budget.nodes
            ):
                truncated = True
                return "rejected"
            seen_semantics.add(candidate.semantic_identity)
            selected_nodes.update(unseen_nodes)
            selected_edges.append(candidate.edge)
            return "admitted"

        seed_position = {seed_id: index for index, seed_id in enumerate(seed_nodes)}

        def group_position(anchor: str | None) -> int:
            if anchor is None:
                return len(seed_position) + 1
            return seed_position.get(anchor, len(seed_position))

        for priority in sorted({candidate.priority for candidate in candidates}):
            queues: dict[tuple[str | None, str, str], list[_EdgeCandidate]] = (
                defaultdict(list)
            )
            for candidate in candidates:
                if candidate.priority == priority:
                    queues[
                        (candidate.anchor, candidate.edge.edge_kind, candidate.role)
                    ].append(candidate)
            for queue in queues.values():
                queue.sort(key=_budget_order_key)
            keys = sorted(
                queues,
                key=lambda key: (
                    group_position(key[0]),
                    kind_order_key(key[1]),
                    ROLE_ORDER.index(key[2]),
                ),
            )
            groups: dict[str | None, list[tuple[str | None, str, str]]] = {}
            for key in keys:
                groups.setdefault(key[0], []).append(key)

            examined = dict.fromkeys(keys, 0)
            slots = min(
                request.budget.edges - len(selected_edges),
                request.budget.nodes - len(selected_nodes),
            )
            if slots > 0:
                shares = dict(
                    zip(groups, fair_share_quotas(slots, len(groups)), strict=True)
                )
                # Every seed's round-robin first, then the unanchored group's,
                # so a two-node session relation cannot take an anchored node.
                for anchored in (True, False):
                    flat: list[tuple[str | None, str, str]] = []
                    quotas: list[int] = []
                    for anchor, group_keys in groups.items():
                        if (anchor is not None) is not anchored:
                            continue
                        flat.extend(group_keys)
                        quotas.extend(
                            fair_share_quotas(shares[anchor], len(group_keys))
                        )
                    ends = round_robin_take([queues[key] for key in flat], quotas, take)
                    examined.update(zip(flat, ends, strict=True))
            remainder = sorted(
                (
                    candidate
                    for key in keys
                    for candidate in queues[key][examined[key] :]
                ),
                key=lambda candidate: (
                    candidate.anchor is None,
                    *_budget_order_key(candidate),
                ),
            )
            for candidate in remainder:
                take(candidate)

        for node in sorted(extra_nodes.values(), key=_node_sort_key):
            if node.id in selected_nodes:
                continue
            if len(selected_nodes) >= request.budget.nodes:
                truncated = True
                break
            selected_nodes[node.id] = node

        return (
            sorted(selected_nodes.values(), key=_node_sort_key),
            sorted(
                selected_edges,
                key=lambda edge: (edge.source, edge.edge_kind, edge.target, edge.id),
            ),
            truncated,
        )

    @staticmethod
    def _finalize_timeline(
        drafts: list[_TimelineDraft],
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        *,
        node_limit: int,
        edge_limit: int,
    ) -> tuple[list[GraphTimelineEvent], bool]:
        current_node_ids = {node.id for node in nodes}
        current_edge_ids = {edge.id for edge in edges}
        ordered_drafts = sorted(
            drafts,
            key=lambda draft: (
                draft.occurred_at,
                draft.source_sequence,
                draft.identity,
            ),
        )
        catalog_node_ids = set(current_node_ids)
        catalog_edge_ids = set(current_edge_ids)
        events: list[GraphTimelineEvent] = []
        capped = False
        for draft in ordered_drafts:
            if len(events) >= edge_limit:
                capped = True
                continue
            draft_nodes = {node.id: node for node in draft.node_snapshots}
            draft_edges = {edge.id: edge for edge in draft.edge_snapshots}
            required_node_ids = set(draft.node_ids)
            required_edge_ids = set(draft.edge_ids)
            if not required_node_ids.issubset(catalog_node_ids | set(draft_nodes)):
                capped = True
                continue
            if not required_edge_ids.issubset(catalog_edge_ids | set(draft_edges)):
                capped = True
                continue
            next_node_ids = catalog_node_ids | set(draft_nodes)
            next_edge_ids = catalog_edge_ids | set(draft_edges)
            if len(next_node_ids) > node_limit or len(next_edge_ids) > edge_limit:
                capped = True
                continue
            catalog_node_ids = next_node_ids
            catalog_edge_ids = next_edge_ids
            events.append(
                GraphTimelineEvent(
                    id=graph_timeline_id(draft.identity),
                    operation=draft.operation,
                    action_kind=draft.action_kind,
                    label=draft.label,
                    occurred_at=draft.occurred_at,
                    sequence=len(events) + 1,
                    node_ids=list(draft.node_ids),
                    edge_ids=list(draft.edge_ids),
                    node_snapshots=[
                        node
                        for node in draft.node_snapshots
                        if node.id not in current_node_ids
                    ],
                    edge_snapshots=list(draft.edge_snapshots),
                    evidence=draft.evidence,
                )
            )
        return events, capped

    @staticmethod
    def _coverages(
        session: Session,
        workspace_id: UUID,
        request: GraphQueryRequest,
    ) -> list[GraphHistoryCoverage]:
        by_id = {
            coverage.stream_id: coverage
            for coverage in session.scalars(
                select(GraphHistoryCoverage).where(
                    GraphHistoryCoverage.workspace_id == workspace_id,
                    GraphHistoryCoverage.stream_kind == "context_page",
                    GraphHistoryCoverage.stream_id.in_(
                        [seed.id for seed in request.seeds]
                    ),
                )
            ).all()
        }
        return [
            by_id.get(seed.id) or _unsupported_coverage(workspace_id, seed.id)
            for seed in request.seeds
        ]

    @staticmethod
    def _history_metadata(
        *,
        request: GraphQueryRequest,
        observations: list[GraphObservation],
        timeline: list[GraphTimelineEvent],
        coverages: list[GraphHistoryCoverage],
    ) -> GraphHistoryMetadata:
        if request.time.mode == "current":
            return GraphHistoryMetadata(
                mode="current",
                basis="current_projection",
                status="complete",
            )
        statuses = {coverage.status for coverage in coverages}
        status = (
            "complete"
            if statuses == {"complete"}
            else "reconstructed"
            if statuses <= {"complete", "reconstructed"}
            else "partial"
        )
        has_domain = any(
            event.evidence is not None and event.evidence.kind != "graph_observation"
            for event in timeline
        )
        basis = (
            "mixed"
            if has_domain and observations
            else "observations"
            if observations
            else "domain_events"
        )
        instants = [event.occurred_at for event in timeline]
        coverage_starts = [
            coverage.coverage_from
            for coverage in coverages
            if coverage.coverage_from is not None
        ]
        watermarks = [
            coverage.watermark
            for coverage in coverages
            if coverage.watermark is not None
        ]
        warnings = [] if status == "complete" else ["history_coverage_partial"]
        return GraphHistoryMetadata(
            mode=request.time.mode,
            basis=basis,
            status=status,
            coverage_from=min([*coverage_starts, *instants], default=None),
            watermark=max([*watermarks, *instants], default=None),
            warnings=warnings,
        )

    @staticmethod
    def _unsupported_as_of_payload(
        *,
        request: GraphQueryRequest,
        workspace: Workspace,
        generated_at: datetime,
        seed_nodes: dict[str, GraphNode],
        warnings: list[str],
        coverages: list[GraphHistoryCoverage],
    ) -> GraphQueryPayload:
        warnings = [*warnings, "as_of_history_unsupported"]
        coverage_starts = [
            coverage.coverage_from
            for coverage in coverages
            if coverage.coverage_from is not None
        ]
        watermarks = [
            coverage.watermark
            for coverage in coverages
            if coverage.watermark is not None
        ]
        nodes = sorted(seed_nodes.values(), key=_node_sort_key)
        return GraphQueryPayload(
            query=request,
            projection_revision=workspace.edge_projection_version,
            generated_at=generated_at,
            nodes=nodes,
            edges=[],
            timeline=[],
            mermaid=None,
            facets=_facets(nodes, [], []),
            budget=GraphBudgetResult(
                requested=request.budget,
                applied=request.budget,
            ),
            completeness="bounded_sample",
            truncated=False,
            warnings=warnings,
            history=GraphHistoryMetadata(
                mode="as_of",
                basis="observations",
                status="unsupported",
                coverage_from=min(coverage_starts, default=None),
                watermark=max(watermarks, default=None),
                warnings=["as_of_history_unsupported"],
            ),
            capabilities=GraphCapabilities(
                expand=False,
                evidence=False,
                export=True,
                saved_view=False,
                timeline=True,
                as_of=False,
            ),
        )


def graph_node_id(kind: str, entity_id: UUID) -> str:
    return f"g1n:{kind}:{entity_id}"


def graph_edge_id(
    workspace_id: UUID,
    source_id: str,
    target_id: str,
    edge_kind: str,
    occurrence_identity: str,
) -> str:
    material = (
        f"dtxwiki.graph-edge.v1|{workspace_id}|{source_id}|{target_id}|"
        f"{edge_kind}|{occurrence_identity}"
    )
    return f"g1e:{uuid5(GRAPH_TRANSPORT_NAMESPACE, material)}"


def graph_timeline_id(identity: str) -> str:
    material = f"dtxwiki.graph-timeline.v1|{identity}"
    return f"g1t:{uuid5(GRAPH_TRANSPORT_NAMESPACE, material)}"


def _node_from_projection(edge: VisibleProjectionEdge) -> GraphNode:
    family = NODE_FAMILY_BY_KIND.get(edge.neighbor_kind)
    if family is None:
        raise ValueError("visible projection kind has no graph node family")
    return GraphNode(
        id=graph_node_id(edge.neighbor_kind, edge.neighbor_id),
        entity_kind=edge.neighbor_kind,
        entity_id=edge.neighbor_id,
        family=family,
        label=edge.neighbor_title[:200],
        path=edge.neighbor_path,
    )


def _node_from_hydrated(kind: str, entity_id: UUID, hydrated) -> GraphNode:
    family = NODE_FAMILY_BY_KIND.get(kind)
    if family is None:
        raise ValueError("hydrated entity kind has no graph node family")
    return GraphNode(
        id=graph_node_id(kind, entity_id),
        entity_kind=kind,
        entity_id=entity_id,
        family=family,
        label=hydrated.title[:200],
        path=hydrated.path,
    )


def _node_filter_allows(node: GraphNode, request: GraphQueryRequest) -> bool:
    if node.is_seed:
        return True
    if request.node_families and node.family not in request.node_families:
        return False
    return not request.node_kinds or node.entity_kind in request.node_kinds


def _entity_key(node: GraphNode) -> tuple[str, str]:
    """A stable order across projection rebuilds and node label edits."""

    return node.family, str(node.entity_id)


def _induced_direction_allows(
    edge: GraphEdge,
    hop_by_id: dict[str, int],
    nodes: dict[str, GraphNode],
    direction: str,
) -> bool:
    """Keep directed induced relations oriented along the requested frontier."""

    if edge.direction == "undirected" or direction == "both":
        return True
    source_structural = nodes[edge.source].family == "structural"
    target_structural = nodes[edge.target].family == "structural"
    if source_structural != target_structural:
        # A folder membership is inbound to its document regardless of the
        # document's hop. Folder nodes are layout context, not hop-frontier
        # anchors from which an outbound traversal starts.
        return source_structural if direction == "in" else target_structural
    source_hop = hop_by_id[edge.source]
    target_hop = hop_by_id[edge.target]
    if direction == "out":
        return source_hop <= target_hop
    return target_hop <= source_hop


def _select_induced_edges(
    candidates: list[_EdgeCandidate],
    hop_by_id: dict[str, int],
    nodes: dict[str, GraphNode],
    edge_budget: int,
    depth: int,
) -> tuple[list[GraphEdge], list[str]]:
    """Reserve relation classes, then share spare slots round-robin."""

    classes = ("folder", "seed", "h1", "h12", "h2")
    class_queues: dict[str, list[_EdgeCandidate]] = {name: [] for name in classes}
    seen: set[tuple[str, str, str, str | None]] = set()
    for candidate in candidates:
        edge = candidate.edge
        if candidate.semantic_identity in seen:
            continue
        seen.add(candidate.semantic_identity)
        source, target = nodes[edge.source], nodes[edge.target]
        source_hop, target_hop = hop_by_id[edge.source], hop_by_id[edge.target]
        if edge.edge_kind == "tree" and (
            source.family == "structural" or target.family == "structural"
        ):
            relation_class = "folder"
        elif source.is_seed or target.is_seed:
            relation_class = "seed"
        elif source_hop == 1 and target_hop == 1:
            relation_class = "h1"
        elif {source_hop, target_hop} == {1, 2}:
            relation_class = "h12"
        elif source_hop == 2 and target_hop == 2:
            relation_class = "h2"
        else:
            # Non-tree structural or observation context may be present, but
            # it must not masquerade as a seed relation.
            relation_class = "h1"
        class_queues[relation_class].append(candidate)

    def order(candidate: _EdgeCandidate) -> tuple:
        edge = candidate.edge
        source, target = nodes[edge.source], nodes[edge.target]
        source_key, target_key = _entity_key(source), _entity_key(target)
        if edge.direction == "undirected":
            source_key, target_key = sorted((source_key, target_key))
        return (
            -edge.rank_score,
            edge.edge_kind,
            source_key,
            target_key,
            edge.occurred_at.isoformat() if edge.occurred_at else "",
        )

    for queue in class_queues.values():
        queue.sort(key=order)
    weights, denominator = (
        ({"folder": 10, "seed": 30, "h1": 25, "h12": 20, "h2": 15}, 100)
        if depth == 2
        else ({"folder": 11, "seed": 54, "h1": 45, "h12": 0, "h2": 0}, 110)
    )
    cursors = {
        name: min(len(class_queues[name]), edge_budget * weights[name] // denominator)
        for name in classes
    }
    remaining = edge_budget - sum(cursors.values())
    while remaining > 0:
        progressed = False
        for name in classes:
            if cursors[name] >= len(class_queues[name]):
                continue
            cursors[name] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            break
    selected = [
        candidate.edge
        for name in classes
        for candidate in class_queues[name][: cursors[name]]
    ]
    omitted = [name for name in classes if cursors[name] < len(class_queues[name])]
    return selected, omitted


def _time_allowed(occurred_at: datetime | None, request: GraphQueryRequest) -> bool:
    if request.time.from_ is None and request.time.to is None:
        return True
    if occurred_at is None:
        return False
    if request.time.from_ is not None and occurred_at < request.time.from_:
        return False
    return request.time.to is None or occurred_at <= request.time.to


def _apply_time_window(statement, occurred_at_column, request: GraphQueryRequest):
    if request.time.from_ is not None:
        statement = statement.where(occurred_at_column >= request.time.from_)
    if request.time.to is not None:
        statement = statement.where(occurred_at_column <= request.time.to)
    return statement


def _budget_order_key(candidate: _EdgeCandidate) -> tuple:
    """Rank, then dated relations newest first, then the neighbour's identity.

    For current relations this is the collector's queue order
    (candidate_order_key), which ranked_projection_edges relies on to hydrate
    each queue only as far as the budget can reach. Domain and session edges
    carry rank 0, so within an action kind the newest events come first.
    """

    edge = candidate.edge
    source, target = candidate.nodes
    neighbour = target if candidate.anchor == source.id else source
    return (
        *candidate_order_key(
            edge.rank_score,
            edge.occurred_at,
            neighbour.entity_kind,
            neighbour.entity_id,
        ),
        edge.id,
    )


def _anchor_and_role(
    edge: GraphEdge, seed_nodes: dict[str, GraphNode]
) -> tuple[str | None, str]:
    """The first seed, in request order, at either end of ``edge``."""

    for seed_id in seed_nodes:
        if seed_id in (edge.source, edge.target):
            if edge.direction == "undirected":
                return seed_id, "undirected"
            return seed_id, "out" if edge.source == seed_id else "in"
    return None, "context"


def _requests_co_access(request: GraphQueryRequest, active_layers: set[str]) -> bool:
    semantic = EDGE_SEMANTICS["co_access"]
    return (
        semantic.default_visible
        and semantic.layer in active_layers
        and (not request.edge_kinds or "co_access" in request.edge_kinds)
    )


def _observation_valid_at(observation: GraphObservation, at: datetime) -> bool:
    if observation.valid_from is not None and observation.valid_from > at:
        return False
    return observation.valid_to is None or observation.valid_to > at


def _observation_direction_allows(
    observation: GraphObservation,
    *,
    seed_refs: set[tuple[str, UUID]],
    direction: str,
) -> bool:
    """Defend the SQL direction contract at every observation consumer."""

    source_matches = (observation.source_kind, observation.source_id) in seed_refs
    target_matches = (
        observation.target_kind is not None
        and observation.target_id is not None
        and (observation.target_kind, observation.target_id) in seed_refs
    )
    if direction == "both":
        return source_matches or target_matches
    if direction == "out":
        return source_matches
    return direction == "in" and target_matches


def _actor_role_allows(
    actor: ActorContext | None,
    required_role: str,
) -> bool:
    if actor is None:
        return False
    if actor.actor_kind == "system":
        return True
    try:
        return _role_allows(actor.workspace_role, required_role)
    except ValueError:
        return False


def _audit_action(action: str) -> str | None:
    normalized = action.casefold()
    for needle, action_kind in (
        ("request_changes", "request_changes"),
        ("publish", "published"),
        ("approve", "approved"),
        ("reject", "rejected"),
        ("restore", "restored"),
        ("promot", "promoted"),
        ("create", "created"),
        ("write", "edited"),
        ("update", "edited"),
        ("edit", "edited"),
        ("review", "reviewed"),
    ):
        if needle in normalized:
            return action_kind
    return None


def _coverage_supports_as_of(
    coverage: GraphHistoryCoverage,
    as_of: datetime | None,
) -> bool:
    if coverage.status != "complete" or as_of is None:
        return False
    if coverage.coverage_from is not None and as_of < coverage.coverage_from:
        return False
    return coverage.watermark is not None and coverage.watermark >= as_of


def _unsupported_coverage(
    workspace_id: UUID,
    stream_id: UUID,
) -> GraphHistoryCoverage:
    return GraphHistoryCoverage(
        workspace_id=workspace_id,
        stream_kind="context_page",
        stream_id=stream_id,
        basis="observations",
        status="unsupported",
        coverage_from=None,
        watermark=None,
        extractor_version="none",
    )


def _node_sort_key(node: GraphNode) -> tuple[int, str, str, str]:
    return (
        0 if node.is_seed else 1,
        node.family,
        node.label.casefold(),
        node.id,
    )


def _facets(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    timeline: list[GraphTimelineEvent],
) -> GraphFacets:
    facet_nodes = [
        *nodes,
        *(snapshot for event in timeline for snapshot in event.node_snapshots),
    ]
    facet_edges = [
        *edges,
        *(snapshot for event in timeline for snapshot in event.edge_snapshots),
    ]
    instants = [
        instant
        for instant in [
            *(node.occurred_at for node in nodes),
            *(edge.occurred_at for edge in edges),
            *(event.occurred_at for event in timeline),
        ]
        if instant is not None
    ]
    return GraphFacets(
        node_families=sorted({node.family for node in facet_nodes}),
        node_kinds=sorted({node.entity_kind for node in facet_nodes}),
        edge_kinds=sorted({edge.edge_kind for edge in facet_edges}),
        action_kinds=sorted(
            {
                *(
                    edge.action_kind
                    for edge in facet_edges
                    if edge.action_kind is not None
                ),
                *(event.action_kind for event in timeline),
            }
        ),
        layers=sorted({edge.layer for edge in facet_edges}),
        statuses=sorted(
            {node.status for node in facet_nodes if node.status is not None}
        ),
        time_from=min(instants, default=None),
        time_to=max(instants, default=None),
    )

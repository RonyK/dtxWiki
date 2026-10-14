"""Versioned contracts for the actor-visible Workspace Knowledge Map.

This contract is intentionally separate from the seed-based Local Graph
contract in :mod:`backend.schemas.graph`. Workspace scenes are document-grain
published-head projections and may use deterministic aggregate nodes; cluster
identifiers therefore are opaque strings rather than fake entity UUIDs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.schemas.common import RESTResponseBase

WorkspaceGraphLens = Literal[
    "combined",
    "links",
    "evidence",
    "directory",
    "compare",
    "growth",
    "provenance",
]
WorkspaceGraphDocumentKind = Literal["context", "library"]
WorkspaceGraphQuickFilter = Literal[
    "orphans",
    "highly_referenced",
    "cross_folder",
    "recently_connected",
]
WorkspaceGraphRepresentation = Literal["exact", "aggregated"]
WorkspaceGraphCompleteness = Literal[
    "complete",
    "capped",
    "partial",
    "privacy_suppressed",
]
WorkspaceGraphOmissionReason = Literal[
    "document_budget",
    "relation_budget",
    "projection_partial",
    "privacy_policy",
    "renderer_fallback",
]
WorkspaceGraphLayer = Literal[
    "knowledge",
    "evidence",
    "structure",
    "provenance",
]
WorkspaceGraphFolderKey = UUID | Literal["unplaced"]
WORKSPACE_GRAPH_MAX_SAFE_INTEGER = 9_007_199_254_740_991
WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT = 200_000


class WorkspaceGraphFolderFilter(BaseModel):
    include: list[WorkspaceGraphFolderKey] = Field(default_factory=list, max_length=64)
    exclude: list[WorkspaceGraphFolderKey] = Field(default_factory=list, max_length=64)
    only: list[WorkspaceGraphFolderKey] = Field(default_factory=list, max_length=64)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_folder_sets(self) -> WorkspaceGraphFolderFilter:
        if len(self.include) != len(set(self.include)):
            raise ValueError("folders.include must not contain duplicates")
        if len(self.exclude) != len(set(self.exclude)):
            raise ValueError("folders.exclude must not contain duplicates")
        if len(self.only) != len(set(self.only)):
            raise ValueError("folders.only must not contain duplicates")
        if set(self.include) & set(self.exclude):
            raise ValueError("folders.include and folders.exclude must be disjoint")
        if self.only and (self.include or self.exclude):
            raise ValueError(
                "folders.only is mutually exclusive with include and exclude"
            )
        return self


class WorkspaceGraphTimeFilter(BaseModel):
    """Actor-visible time constraint for the Workspace projection.

    ``document_updated`` filters on the published Context page or canonical
    current Library item ``updated_at``. ``relation_observed`` filters authored
    relations on their projection timestamp and retains only their visible
    endpoints. Both operate after visibility and never count hidden history.
    """

    basis: Literal["document_updated", "relation_observed"] = "document_updated"
    from_: AwareDatetime | None = Field(default=None, alias="from")
    to: AwareDatetime | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("from_", "to")
    @classmethod
    def normalize_to_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_window(self) -> WorkspaceGraphTimeFilter:
        if self.from_ is not None and self.to is not None and self.from_ > self.to:
            raise ValueError("time.from must be before or equal to time.to")
        return self


class WorkspaceGraphBudget(BaseModel):
    # Keep the inexpensive default overview while permitting explicit larger
    # actor-visible scenes. The request still bounds the rendered wire.
    documents: int = Field(default=250, ge=1, le=10_000)
    relations: int = Field(default=750, ge=1, le=100_000)

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphExpansion(BaseModel):
    cluster_id: str = Field(min_length=1, max_length=220)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphOverviewRequest(BaseModel):
    schema_version: Literal["workspace-graph-overview.v1"] = (
        "workspace-graph-overview.v1"
    )
    head_mode: Literal["published"] = "published"
    lens: WorkspaceGraphLens = "combined"
    document_kinds: list[WorkspaceGraphDocumentKind] = Field(
        default_factory=lambda: ["context", "library"],
        min_length=1,
        max_length=2,
    )
    search: str | None = Field(default=None, min_length=1, max_length=200)
    quick_filters: list[WorkspaceGraphQuickFilter] = Field(
        default_factory=list,
        max_length=4,
    )
    folders: WorkspaceGraphFolderFilter = Field(
        default_factory=WorkspaceGraphFolderFilter
    )
    time: WorkspaceGraphTimeFilter = Field(default_factory=WorkspaceGraphTimeFilter)
    budget: WorkspaceGraphBudget = Field(default_factory=WorkspaceGraphBudget)
    expansion: WorkspaceGraphExpansion | None = None
    # A deep link's target. The server reserves one document slot for it when
    # the scene is aggregated; it is part of the request fingerprint because
    # it changes how the remaining documents are grouped.
    focus_entity_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("search")
    @classmethod
    def normalize_search(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("search must contain visible characters")
        return normalized

    @model_validator(mode="after")
    def validate_unique_filters(self) -> WorkspaceGraphOverviewRequest:
        if len(self.document_kinds) != len(set(self.document_kinds)):
            raise ValueError("document_kinds must not contain duplicates")
        if len(self.quick_filters) != len(set(self.quick_filters)):
            raise ValueError("quick_filters must not contain duplicates")
        return self


class WorkspaceGraphPlacementRef(BaseModel):
    tree_node_id: UUID
    label: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=1024)

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphDocumentNode(BaseModel):
    grain: Literal["document"] = "document"
    id: str = Field(min_length=1, max_length=220)
    entity_kind: WorkspaceGraphDocumentKind
    entity_id: UUID
    label: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=1024)
    primary_folder_id: UUID | None = None
    primary_folder_label: str = Field(min_length=1, max_length=200)
    reference_count: int = Field(default=0, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    also_appears_in: list[WorkspaceGraphPlacementRef] = Field(
        default_factory=list,
        max_length=32,
    )

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphClusterNode(BaseModel):
    grain: Literal["cluster"] = "cluster"
    id: str = Field(min_length=1, max_length=220)
    cluster_kind: Literal["folder_cluster", "link_cluster", "other_cluster"]
    label: str = Field(min_length=1, max_length=200)
    member_count: int = Field(ge=1)
    relation_count: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


WorkspaceGraphNode = Annotated[
    WorkspaceGraphDocumentNode | WorkspaceGraphClusterNode,
    Field(discriminator="grain"),
]


class WorkspaceGraphEdge(BaseModel):
    id: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=220)
    target: str = Field(min_length=1, max_length=220)
    relation_kind: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=120)
    layer: WorkspaceGraphLayer
    direction: Literal["directed", "undirected"] = "directed"
    aggregate: bool = False
    relation_count: int = Field(default=1, ge=1)
    # Legacy scene.v1 fixtures may omit this field. Current projections carry
    # authored occurrences separately from the number of distinct relations.
    occurrence_weight: int | None = Field(
        default=None, strict=True, ge=1, le=WORKSPACE_GRAPH_MAX_SAFE_INTEGER
    )
    cross_folder: bool = False

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_occurrence_weight(self) -> WorkspaceGraphEdge:
        if (
            self.occurrence_weight is not None
            and self.occurrence_weight < self.relation_count
        ):
            raise ValueError("edge occurrence_weight must cover every relation")
        return self


class WorkspaceGraphFolder(BaseModel):
    id: UUID
    label: str = Field(min_length=1, max_length=200)
    parent_folder_id: UUID | None = None
    direct_member_count: int = Field(
        strict=True, ge=0, le=WORKSPACE_GRAPH_MAX_SAFE_INTEGER
    )

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphFolderMembership(BaseModel):
    node_id: str = Field(min_length=1, max_length=220)
    folder_id: UUID | None = None
    entity_kind: WorkspaceGraphDocumentKind
    member_count: int = Field(strict=True, ge=1, le=WORKSPACE_GRAPH_MAX_SAFE_INTEGER)

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphFolderEdgeSlice(BaseModel):
    edge_id: str = Field(min_length=1, max_length=240)
    source_folder_id: UUID | None = None
    source_entity_kind: WorkspaceGraphDocumentKind
    target_folder_id: UUID | None = None
    target_entity_kind: WorkspaceGraphDocumentKind
    relation_count: int = Field(strict=True, ge=1, le=WORKSPACE_GRAPH_MAX_SAFE_INTEGER)
    occurrence_weight: int = Field(
        strict=True, ge=1, le=WORKSPACE_GRAPH_MAX_SAFE_INTEGER
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_occurrence_weight(self) -> WorkspaceGraphFolderEdgeSlice:
        if self.occurrence_weight < self.relation_count:
            raise ValueError("slice occurrence_weight must cover every relation")
        return self


class WorkspaceGraphFolderTopology(BaseModel):
    """Lossless, actor-visible folder inputs, never layout coordinates.

    Membership distributions retain mixed aggregates. Correlated edge slices
    retain their real endpoint folder/kind pair, including aggregate self-edges;
    independent source/target marginals cannot reconstruct that information.
    """

    schema_version: Literal["workspace-graph-folders.v1"] = "workspace-graph-folders.v1"
    folders: list[WorkspaceGraphFolder] = Field(
        max_length=WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT
    )
    memberships: list[WorkspaceGraphFolderMembership] = Field(
        max_length=WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT
    )
    edge_slices: list[WorkspaceGraphFolderEdgeSlice] = Field(
        max_length=WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_folder_forest(self) -> WorkspaceGraphFolderTopology:
        if (
            len(self.folders) + len(self.memberships) + len(self.edge_slices)
            > WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT
        ):
            raise ValueError("folder topology exceeds its row budget")
        folders = {folder.id: folder for folder in self.folders}
        if len(folders) != len(self.folders):
            raise ValueError("folder topology folder ids must be unique")
        if any(
            folder.parent_folder_id is not None
            and folder.parent_folder_id not in folders
            for folder in self.folders
        ):
            raise ValueError("folder topology parent folders must exist")
        completed: set[UUID] = set()
        for folder in self.folders:
            path: set[UUID] = set()
            current: UUID | None = folder.id
            while current is not None and current not in completed:
                if current in path:
                    raise ValueError("folder topology must be acyclic")
                path.add(current)
                current = folders[current].parent_folder_id
            completed.update(path)
        return self


class WorkspaceGraphFacetValue(BaseModel):
    value: str = Field(min_length=1, max_length=220)
    label: str = Field(min_length=1, max_length=200)
    count: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphFacetCounts(BaseModel):
    document_kinds: list[WorkspaceGraphFacetValue] = Field(default_factory=list)
    relation_kinds: list[WorkspaceGraphFacetValue] = Field(default_factory=list)
    folders: list[WorkspaceGraphFacetValue] = Field(default_factory=list)
    quick_filters: list[WorkspaceGraphFacetValue] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphSceneCounts(BaseModel):
    candidate_documents: int = Field(ge=0)
    candidate_relations: int = Field(ge=0)
    rendered_documents: int = Field(ge=0)
    rendered_clusters: int = Field(ge=0)
    rendered_relations: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphCapabilities(BaseModel):
    expand: bool = False
    evidence: bool = False
    export: bool = False
    directory: bool = False
    compare: bool = False
    growth: bool = False
    provenance: bool = False

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphFocus(BaseModel):
    """Where the requested focus landed in this scene.

    ``document`` is the target's own node, ``cluster`` the aggregate that
    contains it when no slot could be reserved within the budget. Anything
    the actor cannot see in this scene is ``unavailable``: missing, unpublished,
    hidden, and filtered-out targets are deliberately indistinguishable.
    """

    status: Literal["document", "cluster", "unavailable"]
    node_id: str | None = Field(default=None, min_length=1, max_length=220)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_node_presence(self) -> WorkspaceGraphFocus:
        if (self.status == "unavailable") != (self.node_id is None):
            raise ValueError("focus node_id is required unless focus is unavailable")
        return self


class WorkspaceGraphScenePayload(BaseModel):
    schema_version: Literal["workspace-graph-scene.v1"] = "workspace-graph-scene.v1"
    workspace_scope_considered: Literal[True] = True
    head_mode: Literal["published"] = "published"
    graph_revision: str = Field(min_length=16, max_length=160)
    generated_at: AwareDatetime
    algorithm_version: str = Field(min_length=1, max_length=80)
    metric_basis: str = Field(min_length=1, max_length=200)
    representation: WorkspaceGraphRepresentation
    completeness: WorkspaceGraphCompleteness
    truncated: bool
    counts: WorkspaceGraphSceneCounts
    omission_reasons: list[WorkspaceGraphOmissionReason] = Field(default_factory=list)
    nodes: list[WorkspaceGraphNode]
    edges: list[WorkspaceGraphEdge]
    # Optional for older scene.v1 payloads and explicit topology row-budget
    # fallback. A current complete topology is validated against this scene.
    folder_topology: WorkspaceGraphFolderTopology | None = None
    facet_counts: WorkspaceGraphFacetCounts
    expansion_cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    warnings: list[str] = Field(default_factory=list, max_length=32)
    capabilities: WorkspaceGraphCapabilities = Field(
        default_factory=WorkspaceGraphCapabilities
    )
    # Present exactly when the request named a focus entity.
    focus: WorkspaceGraphFocus | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_truthful_scene(self) -> WorkspaceGraphScenePayload:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("workspace graph node ids must be unique")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("workspace graph edge ids must be unique")

        document_count = sum(node.grain == "document" for node in self.nodes)
        clusters = [node for node in self.nodes if node.grain == "cluster"]
        if self.counts.rendered_documents != document_count:
            raise ValueError("rendered_documents must equal document node count")
        if self.counts.rendered_clusters != len(clusters):
            raise ValueError("rendered_clusters must equal cluster node count")
        if self.counts.rendered_relations != len(self.edges):
            raise ValueError("rendered_relations must equal edge count")
        represented_documents = document_count + sum(
            node.member_count for node in clusters
        )
        if represented_documents != self.counts.candidate_documents:
            raise ValueError(
                "every candidate document must be represented exactly once"
            )
        represented_relations = sum(edge.relation_count for edge in self.edges)
        # The only allowed deficit is the irreducible authored-kind floor in
        # one packed node. Canonical Workspace kinds each have exactly one
        # layer, so full kind facets prove that retained self-edges contain
        # all relations of their kind and at least one entire kind is omitted.
        # The payload does not contain the request budget; the service also
        # checks that the identity floor actually exceeds that request's cap.
        authored_layers = {
            "wikilink": "knowledge",
            "citation_page": "evidence",
            "citation_source": "evidence",
        }
        relation_facets = self.facet_counts.relation_kinds
        full_kind_counts = {facet.value: facet.count for facet in relation_facets}
        kept_kinds = {edge.relation_kind for edge in self.edges}
        floor_cluster = clusters[0] if len(clusters) == 1 else None
        irreducible_floor_disclosed = (
            document_count == 0
            and floor_cluster is not None
            and floor_cluster.cluster_kind == "other_cluster"
            and floor_cluster.relation_count == self.counts.candidate_relations
            and bool(self.edges)
            and len({(edge.relation_kind, edge.layer) for edge in self.edges})
            == len(self.edges)
            and all(
                edge.aggregate
                and edge.source == floor_cluster.id
                and edge.target == floor_cluster.id
                and authored_layers.get(edge.relation_kind) == edge.layer
                and full_kind_counts.get(edge.relation_kind) == edge.relation_count
                for edge in self.edges
            )
            and len(full_kind_counts) == len(relation_facets)
            and sum(full_kind_counts.values()) == self.counts.candidate_relations
            and all(kind in authored_layers for kind in full_kind_counts)
            and any(
                count > 0 and kind not in kept_kinds
                for kind, count in full_kind_counts.items()
            )
        )
        relation_omission_declared = (
            self.representation == "aggregated"
            and self.completeness != "complete"
            and self.truncated
            and "relation_budget" in self.omission_reasons
            and irreducible_floor_disclosed
        )
        # Full relationship coverage remains the default, including ordinary
        # aggregation. A tiny kind/layer identity floor can exceed the cap even
        # in one packed node, so an explicitly capped aggregate may disclose a
        # deficit. Retained real edges must never claim omitted relationships.
        if represented_relations > self.counts.candidate_relations or (
            represented_relations < self.counts.candidate_relations
            and not relation_omission_declared
        ):
            raise ValueError(
                "edge relation_count values must represent candidate relations"
            )

        node_id_set = set(node_ids)
        if any(
            edge.source not in node_id_set or edge.target not in node_id_set
            for edge in self.edges
        ):
            raise ValueError("workspace graph edge endpoints must exist")

        if self.focus is not None and self.focus.node_id is not None:
            focus_grain = next(
                (node.grain for node in self.nodes if node.id == self.focus.node_id),
                None,
            )
            expected_grain = (
                "document" if self.focus.status == "document" else "cluster"
            )
            if focus_grain != expected_grain:
                raise ValueError("workspace graph focus must name a node of its grain")

        if self.representation == "exact":
            if clusters:
                raise ValueError("exact scenes may not contain cluster nodes")
            if any(edge.aggregate or edge.relation_count != 1 for edge in self.edges):
                raise ValueError("exact scenes may not contain aggregate relations")
        elif not clusters and self.counts.candidate_documents:
            raise ValueError("aggregated scenes must contain a cluster node")

        if self.completeness == "complete":
            if self.truncated or self.omission_reasons:
                raise ValueError("complete scenes cannot be truncated or omitted")
            if self.representation != "exact":
                raise ValueError("complete scenes must be exact")
        elif not self.truncated or not self.omission_reasons:
            raise ValueError(
                "non-complete scenes must disclose truncation and an omission reason"
            )

        if self.expansion_cursor is not None and not self.capabilities.expand:
            raise ValueError("expansion_cursor requires the expand capability")
        if len(self.omission_reasons) != len(set(self.omission_reasons)):
            raise ValueError("omission_reasons must not contain duplicates")
        if self.folder_topology is not None:
            self._validate_folder_topology()
        return self

    def _validate_folder_topology(self) -> None:
        topology = self.folder_topology
        assert topology is not None
        nodes = {node.id: node for node in self.nodes}
        edges = {edge.id: edge for edge in self.edges}
        folders = {folder.id: folder for folder in topology.folders}
        membership_counts: dict[str, int] = {}
        direct_counts: dict[UUID, int] = {}
        membership_keys: set[tuple[str, UUID | None, str]] = set()
        for membership in topology.memberships:
            identity = (
                membership.node_id,
                membership.folder_id,
                membership.entity_kind,
            )
            if identity in membership_keys:
                raise ValueError("folder topology memberships must be unique")
            membership_keys.add(identity)
            node = nodes.get(membership.node_id)
            if node is None:
                raise ValueError("folder topology membership nodes must exist")
            if membership.folder_id is not None:
                if membership.folder_id not in folders:
                    raise ValueError("folder topology membership folders must exist")
                direct_counts[membership.folder_id] = (
                    direct_counts.get(membership.folder_id, 0) + membership.member_count
                )
            if node.grain == "document" and (
                membership.entity_kind != node.entity_kind
                or membership.folder_id != node.primary_folder_id
                or membership.member_count != 1
            ):
                raise ValueError("document folder membership must match its primary")
            membership_counts[node.id] = (
                membership_counts.get(node.id, 0) + membership.member_count
            )
        if any(
            membership_counts.get(node.id, 0)
            != (1 if node.grain == "document" else node.member_count)
            for node in self.nodes
        ):
            raise ValueError("folder memberships must conserve every node's members")
        if any(
            folder.direct_member_count != direct_counts.get(folder.id, 0)
            for folder in topology.folders
        ):
            raise ValueError("folder direct_member_count must match memberships")
        required_folders = set(direct_counts)
        for folder_id in tuple(required_folders):
            parent = folders[folder_id].parent_folder_id
            while parent is not None and parent not in required_folders:
                required_folders.add(parent)
                parent = folders[parent].parent_folder_id
        if required_folders != set(folders):
            raise ValueError("folder topology may contain only member ancestor closure")

        slice_keys: set[tuple[str, UUID | None, str, UUID | None, str]] = set()
        relation_counts: dict[str, int] = {}
        occurrence_weights: dict[str, int] = {}
        for part in topology.edge_slices:
            identity = (
                part.edge_id,
                part.source_folder_id,
                part.source_entity_kind,
                part.target_folder_id,
                part.target_entity_kind,
            )
            if identity in slice_keys:
                raise ValueError("folder topology edge slices must be unique")
            slice_keys.add(identity)
            edge = edges.get(part.edge_id)
            if edge is None:
                raise ValueError("folder topology slice edges must exist")
            if (
                edge.source,
                part.source_folder_id,
                part.source_entity_kind,
            ) not in membership_keys or (
                edge.target,
                part.target_folder_id,
                part.target_entity_kind,
            ) not in membership_keys:
                raise ValueError(
                    "folder topology slices must match endpoint memberships"
                )
            relation_counts[edge.id] = (
                relation_counts.get(edge.id, 0) + part.relation_count
            )
            occurrence_weights[edge.id] = (
                occurrence_weights.get(edge.id, 0) + part.occurrence_weight
            )
        if any(
            relation_counts.get(edge.id, 0) != edge.relation_count
            or edge.occurrence_weight is None
            or occurrence_weights.get(edge.id, 0) != edge.occurrence_weight
            for edge in self.edges
        ):
            raise ValueError("folder edge slices must conserve edge counts and weights")


class WorkspaceGraphSceneResponse(RESTResponseBase):
    data: WorkspaceGraphScenePayload

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphMapCapability(BaseModel):
    """The workspace map's switch state as the product should present it."""

    effective_enabled: bool
    workspace_enabled: bool
    service_available: bool
    reason: Literal["enabled", "disabled_by_workspace", "service_unavailable"]

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphCapability(BaseModel):
    workspace_map: WorkspaceGraphMapCapability
    usage_relation_signals_enabled: bool
    # Owners change these switches in Settings; everyone else sees them.
    can_manage: bool

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphCapabilityResponse(RESTResponseBase):
    data: WorkspaceGraphCapability

    model_config = ConfigDict(extra="forbid")

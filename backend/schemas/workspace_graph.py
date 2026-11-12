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
WorkspaceGraphDocumentKind = Literal["context", "library", "agent"]
WorkspaceGraphQuickFilter = Literal[
    "orphans",
    "highly_referenced",
    "cross_folder",
    "recently_connected",
]
# "grouped" is an intentional grouping (Directory folders) that omits nothing;
# "aggregated" is a budget fallback.
WorkspaceGraphRepresentation = Literal["exact", "aggregated", "grouped"]
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
# TreeRail containment is drawn like any relation but is not an authored one:
# it never counts toward relation budgets, counts, facets or lens filters.
WORKSPACE_GRAPH_CONTAINMENT_KIND = "tree"


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
    # A pinned expansion stays open when later expansions need room; it folds
    # only after everything unpinned.
    pinned: bool = False

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphRetainedExpansion(BaseModel):
    """An earlier expansion the viewer keeps open, replayed in request order."""

    cluster_id: str = Field(min_length=1, max_length=220)
    pinned: bool = False
    # The member window this step showed (a paged expansion replays the same
    # page). Visibility is recomputed per actor, so the offset grants nothing;
    # one past the cluster's members only reports the step missing.
    offset: int = Field(default=0, ge=0)

    model_config = ConfigDict(extra="forbid")


WORKSPACE_GRAPH_RETAINED_EXPANSION_LIMIT = 7


class WorkspaceGraphExpansionOutcome(BaseModel):
    """What one replayed expansion revealed and what of it the scene still shows."""

    cluster_id: str = Field(min_length=1, max_length=220)
    # Absent for a missing step, which names nothing visible.
    label: str | None = Field(default=None, min_length=1, max_length=200)
    member_count: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
    revealed_count: int = Field(default=0, ge=0)
    visible_count: int = Field(default=0, ge=0)
    # Documents shown on their own just before this step that it folded.
    folded_count: int = Field(default=0, ge=0)
    pinned: bool = False
    status: Literal["applied", "partial", "folded", "missing", "no_capacity"]

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphOverviewRequest(BaseModel):
    schema_version: Literal["workspace-graph-overview.v1"] = (
        "workspace-graph-overview.v1"
    )
    head_mode: Literal["published"] = "published"
    lens: WorkspaceGraphLens = "combined"
    document_kinds: list[WorkspaceGraphDocumentKind] = Field(
        default_factory=lambda: ["context", "library", "agent"],
        min_length=1,
        max_length=3,
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
    # Earlier expansions to replay before ``expansion``. Their ordered cluster
    # ids and offsets bind cursors through the request fingerprint because they
    # change the grouping; pin flags are left out so re-pinning keeps a page.
    retained_expansions: list[WorkspaceGraphRetainedExpansion] = Field(
        default_factory=list,
        max_length=WORKSPACE_GRAPH_RETAINED_EXPANSION_LIMIT,
    )
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
        retained = [step.cluster_id for step in self.retained_expansions]
        if len(retained) != len(set(retained)):
            raise ValueError("retained_expansions must not repeat a cluster")
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


class WorkspaceGraphFolderNode(BaseModel):
    grain: Literal["folder"] = "folder"
    id: str = Field(min_length=1, max_length=220)
    folder_id: UUID
    label: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=1024)

    model_config = ConfigDict(extra="forbid")


WorkspaceGraphNode = Annotated[
    WorkspaceGraphDocumentNode | WorkspaceGraphClusterNode | WorkspaceGraphFolderNode,
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
    # The containment a node is laid out from: one per contained node.
    primary: bool = False

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_occurrence_weight(self) -> WorkspaceGraphEdge:
        if (
            self.occurrence_weight is not None
            and self.occurrence_weight < self.relation_count
        ):
            raise ValueError("edge occurrence_weight must cover every relation")
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
    facet_counts: WorkspaceGraphFacetCounts
    expansion_cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    expansions: list[WorkspaceGraphExpansionOutcome] = Field(
        default_factory=list,
        max_length=WORKSPACE_GRAPH_RETAINED_EXPANSION_LIMIT + 1,
    )
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
        relations = [
            edge
            for edge in self.edges
            if edge.relation_kind != WORKSPACE_GRAPH_CONTAINMENT_KIND
        ]
        if self.counts.rendered_documents != document_count:
            raise ValueError("rendered_documents must equal document node count")
        if self.counts.rendered_clusters != len(clusters):
            raise ValueError("rendered_clusters must equal cluster node count")
        if self.counts.rendered_relations != len(relations):
            raise ValueError("rendered_relations must equal relation edge count")
        represented_documents = document_count + sum(
            node.member_count for node in clusters
        )
        if represented_documents != self.counts.candidate_documents:
            raise ValueError(
                "every candidate document must be represented exactly once"
            )
        represented_relations = sum(edge.relation_count for edge in relations)
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
            "citation_agent": "evidence",
        }
        relation_facets = self.facet_counts.relation_kinds
        full_kind_counts = {facet.value: facet.count for facet in relation_facets}
        kept_kinds = {edge.relation_kind for edge in relations}
        floor_cluster = clusters[0] if len(clusters) == 1 else None
        irreducible_floor_disclosed = (
            document_count == 0
            and floor_cluster is not None
            and floor_cluster.cluster_kind == "other_cluster"
            and floor_cluster.relation_count == self.counts.candidate_relations
            and bool(relations)
            and len({(edge.relation_kind, edge.layer) for edge in relations})
            == len(relations)
            and all(
                edge.aggregate
                and edge.source == floor_cluster.id
                and edge.target == floor_cluster.id
                and authored_layers.get(edge.relation_kind) == edge.layer
                and full_kind_counts.get(edge.relation_kind) == edge.relation_count
                for edge in relations
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
            if any(edge.aggregate or edge.relation_count != 1 for edge in relations):
                raise ValueError("exact scenes may not contain aggregate relations")
        elif not clusters and self.counts.candidate_documents:
            raise ValueError(
                "aggregated and grouped scenes must contain a cluster node"
            )

        if self.representation == "grouped" and self.completeness != "complete":
            raise ValueError("grouped scenes must be complete")
        if self.completeness == "complete":
            if self.truncated or self.omission_reasons:
                raise ValueError("complete scenes cannot be truncated or omitted")
            if self.representation == "aggregated":
                raise ValueError("complete scenes must be exact or grouped")
        elif not self.truncated or not self.omission_reasons:
            raise ValueError(
                "non-complete scenes must disclose truncation and an omission reason"
            )

        if self.expansion_cursor is not None and not self.capabilities.expand:
            raise ValueError("expansion_cursor requires the expand capability")
        if len(self.omission_reasons) != len(set(self.omission_reasons)):
            raise ValueError("omission_reasons must not contain duplicates")
        return self


class WorkspaceGraphSceneResponse(RESTResponseBase):
    data: WorkspaceGraphScenePayload

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphMapCapability(BaseModel):
    """The workspace map's switch state as the product should present it."""

    effective_enabled: bool
    workspace_enabled: bool
    service_available: bool
    reason: Literal[
        "enabled", "disabled_by_workspace", "service_unavailable", "permission_denied"
    ]

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphCapability(BaseModel):
    workspace_map: WorkspaceGraphMapCapability
    usage_relation_signals_enabled: bool
    # Workspace administrators configure and use this experiment.
    can_manage: bool

    model_config = ConfigDict(extra="forbid")


class WorkspaceGraphCapabilityResponse(RESTResponseBase):
    data: WorkspaceGraphCapability

    model_config = ConfigDict(extra="forbid")

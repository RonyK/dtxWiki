"""Versioned, renderer-neutral contracts for the interactive Graph View."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
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

GraphNodeFamily = Literal[
    "document",
    "chat",
    "ask",
    "review",
    "agent",
    "event",
    "structural",
]
GraphLayer = Literal["knowledge", "evidence", "activity", "action"]
GraphHistoryMode = Literal["current", "appearance", "action", "as_of"]


class GraphSeed(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    id: UUID

    model_config = ConfigDict(extra="forbid")


class GraphTimeQuery(BaseModel):
    mode: GraphHistoryMode = "current"
    from_: AwareDatetime | None = Field(default=None, alias="from")
    to: AwareDatetime | None = None
    as_of: AwareDatetime | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("from_", "to", "as_of")
    @classmethod
    def normalize_to_utc(cls, value: datetime | None) -> datetime | None:
        # SQLite stores UTCDateTime values without an offset. Canonicalize
        # valid offset-aware API inputs before SQL binding so equal instants
        # compare equally instead of comparing their local wall-clock text.
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_window(self) -> GraphTimeQuery:
        if self.from_ is not None and self.to is not None and self.from_ > self.to:
            raise ValueError("time.from must be before or equal to time.to")
        if self.mode == "as_of" and self.as_of is None:
            raise ValueError("time.as_of is required when mode is as_of")
        if self.mode != "as_of" and self.as_of is not None:
            raise ValueError("time.as_of is only valid when mode is as_of")
        return self


class GraphBudget(BaseModel):
    nodes: int = Field(default=50, ge=1, le=250)
    edges: int = Field(default=150, ge=1, le=750)

    model_config = ConfigDict(extra="forbid")


class GraphQueryRequest(BaseModel):
    schema_version: Literal["dtxwiki.graph-query.v1"] = "dtxwiki.graph-query.v1"
    seeds: list[GraphSeed] = Field(min_length=1, max_length=8)
    scope: Literal["local"] = "local"
    preset: Literal[
        "linked-knowledge",
        "evidence",
        "activity",
        "all",
    ] = "all"
    layers: list[GraphLayer] = Field(
        default_factory=lambda: ["knowledge", "evidence", "activity", "action"],
        max_length=4,
    )
    node_families: list[GraphNodeFamily] = Field(default_factory=list, max_length=7)
    node_kinds: list[str] = Field(default_factory=list, max_length=32)
    edge_kinds: list[str] = Field(default_factory=list, max_length=32)
    action_kinds: list[str] = Field(default_factory=list, max_length=32)
    direction: Literal["in", "out", "both"] = "both"
    depth: Literal[1, 2] = 2
    time: GraphTimeQuery = Field(default_factory=GraphTimeQuery)
    budget: GraphBudget = Field(default_factory=GraphBudget)
    cursor: None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_unique_filters(self) -> GraphQueryRequest:
        if len(self.seeds) > self.budget.nodes:
            raise ValueError("node budget must accommodate every seed")
        for name in (
            "layers",
            "node_families",
            "node_kinds",
            "edge_kinds",
            "action_kinds",
        ):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicate values")
        for name in ("node_kinds", "edge_kinds", "action_kinds"):
            if any(not value or len(value) > 64 for value in getattr(self, name)):
                raise ValueError(f"{name} values must be 1..64 characters")
        return self


class GraphEvidenceRef(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    id: UUID | None = None
    ref: str | None = Field(default=None, max_length=512)
    label: str | None = Field(default=None, max_length=160)
    available: bool = True

    model_config = ConfigDict(extra="forbid")


class GraphNode(BaseModel):
    id: str = Field(min_length=1, max_length=220)
    entity_kind: str = Field(min_length=1, max_length=64)
    entity_id: UUID
    family: GraphNodeFamily
    label: str = Field(min_length=1, max_length=200)
    status: str | None = Field(default=None, max_length=64)
    path: str | None = Field(default=None, max_length=512)
    is_seed: bool = False
    occurred_at: datetime | None = None
    actor_label: str | None = Field(default=None, max_length=120)
    evidence: GraphEvidenceRef | None = None

    model_config = ConfigDict(extra="forbid")


class GraphEdge(BaseModel):
    id: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=220)
    target: str = Field(min_length=1, max_length=220)
    edge_kind: str = Field(min_length=1, max_length=64)
    action_kind: str | None = Field(default=None, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    layer: GraphLayer
    provenance: Literal["curated", "derived", "usage", "domain", "observed"]
    direction: Literal["directed", "undirected"] = "directed"
    weight: float = Field(default=1.0, ge=0)
    occurred_at: datetime | None = None
    # co_access only: the sessions that read both documents together and the
    # first of those reads; occurred_at is the last. None for other kinds.
    support_session_count: int | None = Field(default=None, ge=1)
    first_observed_at: datetime | None = None
    # Actor-visible relation rank in [0, 1) (graph_rank): comparable across
    # relation kinds, unlike weight. 0 for history, domain and session edges.
    # le=1 only so float rounding can never fail response validation.
    rank_score: float = Field(default=0.0, ge=0.0, le=1.0)
    sequence: int | None = Field(default=None, ge=0)
    aggregate: bool = False
    count: int | None = Field(default=None, ge=1)
    window_from: datetime | None = None
    window_to: datetime | None = None
    evidence: GraphEvidenceRef | None = None

    model_config = ConfigDict(extra="forbid")


class GraphTimelineEvent(BaseModel):
    id: str = Field(min_length=1, max_length=220)
    operation: Literal[
        "node_created",
        "edge_created",
        "edge_removed",
        "action_occurred",
    ]
    action_kind: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=160)
    occurred_at: datetime
    sequence: int = Field(ge=0)
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    # Historical-only endpoints must not be added to the current scene merely
    # so an earlier frame can render them. Events therefore carry the node
    # snapshots needed for replay alongside their stable transport IDs.
    node_snapshots: list[GraphNode] = Field(default_factory=list)
    # A removed relationship is absent from the current scene. Carry the
    # renderer-neutral snapshot observed at creation time so replay clients
    # can still add it before a later ``edge_removed`` event removes it.
    edge_snapshots: list[GraphEdge] = Field(default_factory=list)
    evidence: GraphEvidenceRef | None = None

    model_config = ConfigDict(extra="forbid")


class GraphFacets(BaseModel):
    node_families: list[str] = Field(default_factory=list)
    node_kinds: list[str] = Field(default_factory=list)
    edge_kinds: list[str] = Field(default_factory=list)
    action_kinds: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    time_from: datetime | None = None
    time_to: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class GraphBudgetResult(BaseModel):
    requested: GraphBudget
    applied: GraphBudget

    model_config = ConfigDict(extra="forbid")


class GraphOmission(BaseModel):
    scope: Literal["node", "relation"]
    reason: Literal[
        "node_quota",
        "frontier_cap",
        "folder_node",
        "folder_source_cap",
        "relation_quota",
        "relation_source_cap",
    ]
    hop: Literal[1, 2] | None = None
    relation_class: Literal["folder", "seed", "h1", "h12", "h2"] | None = None

    model_config = ConfigDict(extra="forbid")


class GraphHistoryMetadata(BaseModel):
    mode: GraphHistoryMode
    basis: Literal[
        "current_projection",
        "domain_events",
        "observations",
        "mixed",
    ]
    status: Literal["complete", "partial", "reconstructed", "unsupported"]
    coverage_from: datetime | None = None
    watermark: datetime | None = None
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class GraphCapabilities(BaseModel):
    # Capabilities are fail-closed: every response must opt in only after the
    # corresponding actor-authorized route and UI path exist.
    expand: bool = False
    evidence: bool = False
    export: bool = False
    saved_view: bool = False
    timeline: bool = False
    as_of: bool = False

    model_config = ConfigDict(extra="forbid")


class GraphQueryPayload(BaseModel):
    schema_version: Literal["dtxwiki.graph-scene.v1"] = "dtxwiki.graph-scene.v1"
    query: GraphQueryRequest
    projection_revision: int = Field(ge=0)
    generated_at: datetime
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    timeline: list[GraphTimelineEvent] = Field(default_factory=list)
    mermaid: str | None = None
    facets: GraphFacets
    budget: GraphBudgetResult
    completeness: Literal[
        "complete",
        "capped",
        "bounded_sample",
        "privacy_suppressed",
    ] = "complete"
    truncated: bool = False
    omissions: list[GraphOmission] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    history: GraphHistoryMetadata
    capabilities: GraphCapabilities = Field(default_factory=GraphCapabilities)

    model_config = ConfigDict(extra="forbid")


class GraphQueryResponse(RESTResponseBase):
    data: GraphQueryPayload

    model_config = ConfigDict(extra="forbid")

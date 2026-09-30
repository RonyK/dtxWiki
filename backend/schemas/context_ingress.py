from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase
from backend.schemas.read import TreePathEntry

ContextIngressSaveMode = Literal[
    "create_page",
    "create_child_page",
    "append_section",
    "update_draft",
    "feedback_only",
]
ContextIngressStatus = Literal["proposed", "accepted", "rejected", "superseded"]
PlacementConfidence = Literal["high", "medium", "low"]
ContextPageType = Literal[
    "overview",
    "source",
    "entity",
    "concept",
    "synthesis",
    "comparison",
    "decision",
    "contradiction",
    "query",
    "guide",
]


class AskTurnPromoteRequest(BaseModel):
    save_mode: ContextIngressSaveMode
    parent_node_id: UUID | None = None
    page_type: ContextPageType
    title: str | None = None
    body_markdown: str | None = None
    alternatives: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ProposalSourceRef(BaseModel):
    """What the proposal was promoted from (ask turn today; id is None when
    the source turn was deleted)."""

    kind: str
    id: UUID | None

    model_config = ConfigDict(extra="forbid")


class ContextIngressProposalDetail(BaseModel):
    id: UUID
    workspace_id: UUID
    source_kind: str
    source_ref: ProposalSourceRef
    source_item_ids: list[UUID] = Field(default_factory=list)
    proposed_context_page_type: str
    proposed_parent_tree_node_id: UUID
    proposed_parent_breadcrumb: list[TreePathEntry]
    proposed_title: str
    proposed_save_mode: str
    placement_reason: str | None
    placement_confidence: PlacementConfidence
    alternatives: list[dict]
    status: ContextIngressStatus
    accepted_change_set_id: UUID | None
    created_by_actor_type: str | None
    created_by_actor_id: UUID | None
    created_at: datetime
    updated_at: datetime
    accepted_at: datetime | None

    model_config = ConfigDict(extra="forbid")


class ContextIngressProposalListData(BaseModel):
    proposals: list[ContextIngressProposalDetail] = Field(default_factory=list)
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class ContextIngressAcceptRequest(BaseModel):
    final_save_mode: ContextIngressSaveMode
    final_parent_node_id: UUID
    final_title: str = Field(min_length=1)
    final_page_type: ContextPageType | None = None
    target_page_id: UUID | None = None
    body_markdown: str | None = None
    review_required: bool | None = None

    model_config = ConfigDict(extra="forbid")


class ContextIngressRejectRequest(BaseModel):
    reason: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


class ContextIngressAcceptResult(BaseModel):
    proposal_id: UUID
    change_set_id: UUID
    context_page_id: UUID | None = None
    context_page_version_id: UUID | None = None
    tree_node_id: UUID | None = None
    feedback_item_id: UUID | None = None
    review_item_id: UUID | None = None
    review_required_reason: str | None = None
    audit_event_id: UUID

    model_config = ConfigDict(extra="forbid")


class ContextIngressProposalResponse(RESTResponseBase):
    data: ContextIngressProposalDetail

    model_config = ConfigDict(extra="forbid")


class ContextIngressProposalListResponse(RESTResponseBase):
    data: ContextIngressProposalListData

    model_config = ConfigDict(extra="forbid")


class ContextIngressAcceptResponse(RESTResponseBase):
    data: ContextIngressAcceptResult

    model_config = ConfigDict(extra="forbid")

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase

WorkspaceJoinRequestStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "cancelled",
    "closed",
]


class WorkspaceJoinRequestItem(BaseModel):
    request_id: UUID
    workspace_id: UUID
    requester_user_id: UUID
    requester_email: str | None
    requester_display_name: str
    status: WorkspaceJoinRequestStatus
    attempt_no: int
    requested_at: datetime
    updated_at: datetime
    decided_at: datetime | None
    decided_by_user_id: UUID | None
    decision_note: str | None
    closed_reason: str | None

    model_config = ConfigDict(extra="forbid")


class WorkspaceJoinMutationData(BaseModel):
    request: WorkspaceJoinRequestItem
    new_attempt: bool
    membership_created: bool

    model_config = ConfigDict(extra="forbid")


class WorkspaceJoinMutationResponse(RESTResponseBase):
    data: WorkspaceJoinMutationData


class WorkspaceJoinRequestListData(BaseModel):
    items: list[WorkspaceJoinRequestItem]
    total: int
    limit: int
    offset: int
    has_more: bool

    model_config = ConfigDict(extra="forbid")


class WorkspaceJoinRequestListResponse(RESTResponseBase):
    data: WorkspaceJoinRequestListData


class WorkspaceJoinDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    expected_attempt_no: int = Field(ge=1)
    decision_note: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(extra="forbid")

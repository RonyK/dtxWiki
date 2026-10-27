from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.auth import EmailInput
from backend.schemas.common import RESTResponseBase

WorkspaceMemberRole = Literal["viewer", "editor", "owner"]


class WorkspaceMemberItem(BaseModel):
    user_id: UUID
    email: str | None
    display_name: str
    status: str
    role: WorkspaceMemberRole
    joined_at: datetime
    is_current_user: bool

    model_config = ConfigDict(extra="forbid")


class WorkspaceMemberListData(BaseModel):
    items: list[WorkspaceMemberItem]
    total: int
    limit: int
    offset: int
    has_more: bool
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class WorkspaceMemberListResponse(RESTResponseBase):
    data: WorkspaceMemberListData


class WorkspaceMemberAddRequest(BaseModel):
    user_id: UUID | None = None
    email: EmailInput | None = None
    role: str = Field(default="viewer", min_length=1, max_length=32)
    confirm_first_share: bool = False

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _one_selector(self):
        if (self.user_id is None) == (self.email is None):
            raise ValueError("provide exactly one of user_id or native email")
        return self


class WorkspaceMemberCandidateItem(BaseModel):
    user_id: UUID
    display_name: str
    provider_name: str | None
    provider_slug: str
    provider_label: dict[str, str]
    identity_label: str
    identity_verified: Literal[True] = True


class WorkspaceMemberCandidatesData(BaseModel):
    enabled: bool
    items: list[WorkspaceMemberCandidateItem]
    has_more: bool


class WorkspaceMemberCandidatesResponse(RESTResponseBase):
    data: WorkspaceMemberCandidatesData


class WorkspaceMemberRoleUpdateRequest(BaseModel):
    role: str = Field(min_length=1, max_length=32)

    model_config = ConfigDict(extra="forbid")


class WorkspaceMemberMutationData(BaseModel):
    member: WorkspaceMemberItem
    created: bool
    first_share: bool
    first_share_warning_code: str | None = None
    member_count: int

    model_config = ConfigDict(extra="forbid")


class WorkspaceMemberMutationResponse(RESTResponseBase):
    data: WorkspaceMemberMutationData


class WorkspaceMemberRemovalData(BaseModel):
    removed_user_id: UUID
    previous_role: WorkspaceMemberRole
    default_workspace_id: UUID | None = Field(
        description="Persisted landing preference after the removal."
    )
    fallback_workspace_id: UUID | None = Field(
        description=(
            "Accessible Workspace to select after leaving, or null when none remains."
        )
    )
    member_count: int

    model_config = ConfigDict(extra="forbid")


class WorkspaceMemberRemovalResponse(RESTResponseBase):
    data: WorkspaceMemberRemovalData

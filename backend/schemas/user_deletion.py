from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.auth import CurrentPasswordInput, EmailInput
from backend.schemas.common import RESTResponseBase


class UserDeletionPolicyData(BaseModel):
    retention_days: int = Field(strict=True, ge=0, le=36500)

    model_config = ConfigDict(extra="forbid")


class UserDeletionPersonalWorkspaceData(BaseModel):
    workspace_id: UUID
    title: str
    other_member_count: int

    model_config = ConfigDict(extra="forbid")


class UserDeletionTransferredWorkspaceData(BaseModel):
    workspace_id: UUID
    title: str

    model_config = ConfigDict(extra="forbid")


class UserDeletionPreviewData(UserDeletionPolicyData):
    # Deleted at cleanup, even when shared with other members.
    personal_workspace: UserDeletionPersonalWorkspaceData | None
    # Shared workspaces whose last active owner is leaving; every active
    # server administrator becomes an owner when the deletion is requested.
    transferred_workspaces: list[UserDeletionTransferredWorkspaceData]
    recipient_admin_count: int


class AdminUserDeletionRequest(BaseModel):
    confirmation_email: EmailInput
    expected_retention_days: int = Field(strict=True, ge=0, le=36500)

    model_config = ConfigDict(extra="forbid")


class SelfUserDeletionRequest(AdminUserDeletionRequest):
    current_password: CurrentPasswordInput


class UserDeletionData(BaseModel):
    user_id: UUID
    status: Literal["deletion_pending"]
    requested_at: datetime
    purge_after: datetime
    retention_days: int

    model_config = ConfigDict(extra="forbid")


class UserDeletionRestoreData(BaseModel):
    user_id: UUID
    status: Literal["active", "disabled"]

    model_config = ConfigDict(extra="forbid")


class UserDeletionPolicyResponse(RESTResponseBase):
    data: UserDeletionPolicyData


class UserDeletionPreviewResponse(RESTResponseBase):
    data: UserDeletionPreviewData


class UserDeletionResponse(RESTResponseBase):
    data: UserDeletionData


class UserDeletionRestoreResponse(RESTResponseBase):
    data: UserDeletionRestoreData

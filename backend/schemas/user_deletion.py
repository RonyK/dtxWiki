from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.auth import CurrentPasswordInput, EmailInput
from backend.schemas.common import RESTResponseBase


class UserDeletionPolicyData(BaseModel):
    retention_days: int = Field(strict=True, ge=0, le=36500)

    model_config = ConfigDict(extra="forbid")


class UserDeletionWorkspaceData(BaseModel):
    workspace_id: UUID
    title: str
    # Other members whose accounts are active.
    other_member_count: int

    model_config = ConfigDict(extra="forbid")


class UserDeletionPreviewData(UserDeletionPolicyData):
    # Workspaces this account is the only admin of: held with the account,
    # closed to members, and deleted when the account is.
    target_workspaces: list[UserDeletionWorkspaceData]
    # Self-withdrawal only: workspaces that still have other active members
    # must be deleted, or given another admin, first.
    blocking_workspaces: list[UserDeletionWorkspaceData]


class UserDeletionApprovedWorkspace(BaseModel):
    workspace_id: UUID
    other_member_count: int = Field(strict=True, ge=0)

    model_config = ConfigDict(extra="forbid")


class AdminUserDeletionRequest(BaseModel):
    confirmation_email: EmailInput
    expected_retention_days: int = Field(strict=True, ge=0, le=36500)
    # Exactly the target workspaces the preview showed; a mismatch is refused
    # so nothing is held that the confirming person did not see.
    approved_workspaces: list[UserDeletionApprovedWorkspace] = Field(max_length=1000)

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

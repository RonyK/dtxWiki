from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.auth import CurrentPasswordInput, EmailInput
from backend.schemas.common import RESTResponseBase


class UserDeletionPolicyData(BaseModel):
    retention_days: int = Field(strict=True, ge=0, le=36500)

    model_config = ConfigDict(extra="forbid")


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


class UserDeletionPolicyResponse(RESTResponseBase):
    data: UserDeletionPolicyData


class UserDeletionResponse(RESTResponseBase):
    data: UserDeletionData

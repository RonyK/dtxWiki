from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase
from backend.services.password_recovery_service import (
    DEFAULT_PASSWORD_RESET_TTL_SECONDS,
    MAX_PASSWORD_RESET_TTL_SECONDS,
    MIN_PASSWORD_RESET_TTL_SECONDS,
)


class AdminUserWorkspaceData(BaseModel):
    workspace_id: UUID
    slug: str
    title: str
    role: Literal["viewer", "editor", "owner"]

    model_config = ConfigDict(extra="forbid")


class AdminUserIdentityData(BaseModel):
    identity_id: UUID
    provider_slug: str
    issuer: str
    subject: str


class AdminIdentityRebindRequest(BaseModel):
    identity_id: UUID
    expected_issuer: str = Field(min_length=1, max_length=1024)
    expected_subject: str = Field(min_length=1, max_length=512)
    provider_slug: str = Field(min_length=1, max_length=127)
    issuer: str = Field(min_length=1, max_length=1024)
    subject: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=1000)
    model_config = ConfigDict(extra="forbid")


class AdminIdentityRebindData(AdminUserIdentityData):
    user_id: UUID


class AdminIdentityRebindResponse(RESTResponseBase):
    data: AdminIdentityRebindData


class AdminUserData(BaseModel):
    user_id: UUID
    email: str | None
    display_name: str
    status: str
    email_verified_at: datetime | None = None
    workspaces: list[AdminUserWorkspaceData]
    identities: list[AdminUserIdentityData]

    model_config = ConfigDict(extra="forbid")


class AdminUserListData(BaseModel):
    items: list[AdminUserData]
    total: int
    limit: int
    offset: int

    model_config = ConfigDict(extra="forbid")


class AdminPasswordResetRequest(BaseModel):
    delivery: Literal["email", "link"]
    expires_in_seconds: int = Field(
        default=DEFAULT_PASSWORD_RESET_TTL_SECONDS,
        ge=MIN_PASSWORD_RESET_TTL_SECONDS,
        le=MAX_PASSWORD_RESET_TTL_SECONDS,
    )
    revoke_api_tokens: bool = False

    model_config = ConfigDict(extra="forbid")


class AdminPasswordResetData(BaseModel):
    user_id: UUID
    delivery: Literal["email", "link"]
    expires_at: datetime
    reset_url: str | None = None
    revoke_api_tokens: bool

    model_config = ConfigDict(extra="forbid")


class AdminUserListResponse(RESTResponseBase):
    data: AdminUserListData


class AdminPasswordResetResponse(RESTResponseBase):
    data: AdminPasswordResetData

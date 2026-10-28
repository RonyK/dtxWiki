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
    # Held with this user's pending deletion; appoint an admin to keep it.
    deletion_held: bool = False

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
    deletion_requested_at: datetime | None = None
    purge_after: datetime | None = None
    deletion_failure_count: int | None = None
    deletion_last_error: str | None = None
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


class AdminEmailConflictTargetData(BaseModel):
    user_id: UUID
    display_name: str
    status: str
    created_at: datetime
    model_config = ConfigDict(extra="forbid")


class AdminEmailConflictData(BaseModel):
    request_id: UUID
    status: Literal["pending", "approved", "rejected", "resolved", "auto_released"]
    email: str
    provider_slug: str
    subject: str
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    reason: str | None
    target: AdminEmailConflictTargetData
    model_config = ConfigDict(extra="forbid")


class AdminEmailConflictListData(BaseModel):
    items: list[AdminEmailConflictData]
    total: int
    limit: int
    offset: int
    model_config = ConfigDict(extra="forbid")


class AdminEmailConflictListResponse(RESTResponseBase):
    data: AdminEmailConflictListData


class AdminEmailConflictDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=1000)
    model_config = ConfigDict(extra="forbid")


class AdminEmailConflictDecisionData(BaseModel):
    request_id: UUID
    status: Literal["approved", "rejected", "resolved"]
    model_config = ConfigDict(extra="forbid")


class AdminEmailConflictDecisionResponse(RESTResponseBase):
    data: AdminEmailConflictDecisionData


class AdminAuditEventData(BaseModel):
    event_id: UUID
    created_at: datetime
    action: str
    actor_kind: str
    actor_id: str
    target_kind: str
    target_id: UUID
    summary: str | None
    payload: dict | None
    model_config = ConfigDict(extra="forbid")


class AdminAuditEventListData(BaseModel):
    items: list[AdminAuditEventData]
    next_cursor: str | None
    model_config = ConfigDict(extra="forbid")


class AdminAuditEventListResponse(RESTResponseBase):
    data: AdminAuditEventListData

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase


class PolicyCard(BaseModel):
    name: str
    title: str
    policy: dict[str, Any]
    revision: str = Field(min_length=64, max_length=64)

    model_config = ConfigDict(extra="forbid")


class SettingsMcpClientData(BaseModel):
    id: UUID
    name: str
    client_name: str
    client_type: str
    scopes: list[str]
    status: Literal["active", "disabled"]
    connection_state: Literal["active", "expired", "revoked", "disabled"]
    oauth_client_id: str | None = None
    last_used_at: datetime | None = None
    next_expiry: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class SettingsMcpClientStatusCounts(BaseModel):
    active: int = Field(ge=0)
    disabled: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class SettingsMcpClientListData(BaseModel):
    data: list[SettingsMcpClientData]
    next_cursor: str | None = None
    total_count: int = Field(ge=0)
    filtered_count: int = Field(ge=0)
    status_counts: SettingsMcpClientStatusCounts

    model_config = ConfigDict(extra="forbid")


class SettingsMcpClientListResponse(RESTResponseBase):
    data: list[SettingsMcpClientData]
    next_cursor: str | None = None
    total_count: int = Field(ge=0)
    filtered_count: int = Field(ge=0)
    status_counts: SettingsMcpClientStatusCounts

    model_config = ConfigDict(extra="forbid")


class SettingsMcpClientDeleteData(BaseModel):
    deleted_client_id: UUID
    deleted_at: datetime

    model_config = ConfigDict(extra="forbid")


class SettingsMcpClientDeleteResponse(RESTResponseBase):
    data: SettingsMcpClientDeleteData

    model_config = ConfigDict(extra="forbid")


class SettingsPolicies(BaseModel):
    workspace: PolicyCard
    library: PolicyCard
    citation: PolicyCard
    context_write: PolicyCard
    agent_injection: PolicyCard
    review: PolicyCard
    lint: PolicyCard
    graph: PolicyCard
    mcp_clients: list[SettingsMcpClientData]
    mcp_clients_next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class SettingsPoliciesResponse(RESTResponseBase):
    data: SettingsPolicies

    model_config = ConfigDict(extra="forbid")


class SettingsPolicyUpdateRequest(BaseModel):
    group: Literal[
        "workspace",
        "library",
        "citation",
        "context_write",
        "agent_injection",
        "review",
        "lint",
        "graph",
    ]
    key: str = Field(min_length=1, max_length=128)
    value: Any

    model_config = ConfigDict(extra="forbid")


class SettingsPolicyBatchUpdateRequest(BaseModel):
    group: Literal[
        "workspace",
        "library",
        "citation",
        "context_write",
        "agent_injection",
        "review",
        "lint",
        "graph",
    ]
    expected_revision: str = Field(min_length=64, max_length=64)
    changes: dict[str, Any] = Field(min_length=1, max_length=64)

    model_config = ConfigDict(extra="forbid")


class SettingsMcpClientUpdateRequest(BaseModel):
    status: Literal["active", "disabled"]

    model_config = ConfigDict(extra="forbid")

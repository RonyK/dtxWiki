from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase

DirectoryRequestStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "cancelled",
    "closed",
]


class WorkspaceDirectoryItem(BaseModel):
    workspace_id: UUID
    title: str
    description: str | None
    visibility: Literal["team", "public"]
    join_policy: Literal["invite_only", "request"]
    current_request_status: DirectoryRequestStatus | None

    model_config = ConfigDict(extra="forbid")


class WorkspaceDirectoryData(BaseModel):
    items: list[WorkspaceDirectoryItem]
    next_cursor: str | None = Field(default=None, max_length=2048)

    model_config = ConfigDict(extra="forbid")


class WorkspaceDirectoryResponse(RESTResponseBase):
    data: WorkspaceDirectoryData

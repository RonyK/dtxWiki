from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from backend.schemas.common import RESTResponseBase

WorkspaceVisibility = Literal["private", "team", "public"]
WorkspaceJoinPolicy = Literal["invite_only", "request"]


class WorkspaceAccessSettings(BaseModel):
    workspace_id: UUID
    visibility: WorkspaceVisibility
    join_policy: WorkspaceJoinPolicy
    pending_join_request_count: int
    public_page_count: int
    closed_join_request_count: int = 0
    cleared_public_page_count: int = 0
    updated_at: datetime

    model_config = ConfigDict(extra="forbid")


class WorkspaceAccessResponse(RESTResponseBase):
    data: WorkspaceAccessSettings


class WorkspaceAccessUpdateRequest(BaseModel):
    visibility: WorkspaceVisibility | None = None
    join_policy: WorkspaceJoinPolicy | None = None
    expected_updated_at: datetime
    confirm_access_change: bool = False
    # Optional so older clients that only send expected_updated_at keep
    # working; when present, update_access also stale-guards these two
    # counts, which the confirmation dialog quotes but Workspace.updated_at
    # never reflects on its own (WS-07).
    expected_pending_join_request_count: int | None = None
    expected_public_page_count: int | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_change(self):
        if self.visibility is None and self.join_policy is None:
            raise ValueError("visibility or join_policy is required")
        return self

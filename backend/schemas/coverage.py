from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase


class CoverageGapRead(BaseModel):
    id: UUID
    workspace_id: UUID
    query_norm: str
    surface: str
    tried_lanes: list[str] = Field(default_factory=list)
    nearest_candidates: list[dict] = Field(default_factory=list)
    rewrite_attempts: list[dict] = Field(default_factory=list)
    status: str
    hit_count: int
    created_at: datetime
    resolved_at: datetime | None = None
    finding_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class CoverageGapListData(BaseModel):
    items: list[CoverageGapRead] = Field(default_factory=list)
    has_more: bool = False

    model_config = ConfigDict(extra="forbid")


class CoverageGapListResponse(RESTResponseBase):
    data: CoverageGapListData

    model_config = ConfigDict(extra="forbid")

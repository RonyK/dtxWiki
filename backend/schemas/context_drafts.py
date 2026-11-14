"""Read-only Context draft metadata shared by REST and MCP."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import WIRE_EXPLICIT_FIELD


class ContextDraftAuthor(BaseModel):
    actor_kind: str
    actor_id: str
    display_name: str | None = None

    model_config = ConfigDict(extra="forbid")


class ContextDraftReview(BaseModel):
    id: UUID
    status: str

    model_config = ConfigDict(extra="forbid")


class ContextDraftInfo(BaseModel):
    author: ContextDraftAuthor
    last_saved_at: datetime
    is_editing: bool
    review: ContextDraftReview | None = None
    is_behind: bool | None = Field(
        default=None,
        json_schema_extra=WIRE_EXPLICIT_FIELD,
        description=(
            "Whether the current published version is absent from this draft's "
            "ancestry; null for incomplete or cyclic ancestry."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class ContextDraftSummary(ContextDraftInfo):
    version_id: UUID
    version_status: Literal["editing", "draft"]
    is_mine: bool
    state: Literal["writing", "reviewing"]


class OpenDrafts(BaseModel):
    total: int = Field(default=0, ge=0)
    items: list[ContextDraftSummary] = Field(default_factory=list)
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")

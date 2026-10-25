"""MCP-specific read shapes, independent of REST and retriever identities."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.read import GuidePayload, SearchPayload
from backend.spi.types import SearchMatchReason, SearchResultType


class MCPRankedResult(BaseModel):
    id: UUID
    type: SearchResultType
    title: str
    snippet: str
    score: float
    citation_available: bool
    stale: bool = Field(default=False, deprecated=True)
    archived: bool = False
    summary: str | None = None
    location: list[str] | None = None
    path: str | None = Field(default=None, description="Library lookup path only.")
    concept_type: str | None = None
    version_id: UUID | None = None
    heading_path: list[str] | None = None
    section_id: str | None = None
    source_ref: dict[str, str] | None = None
    match_reason: SearchMatchReason | None = None
    matched_name: str | None = None
    entity_ref: dict[str, str] | None = None
    stable_url: str | None = None
    link_markdown: str | None = None
    model_config = ConfigDict(extra="forbid")


class MCPSearchPayload(SearchPayload):
    ranked_results: list[MCPRankedResult]

    @model_validator(mode="before")
    @classmethod
    def _model_data(cls, value):
        return value.model_dump() if isinstance(value, BaseModel) else value


class MCPHotConceptEntry(BaseModel):
    kind: str
    id: UUID
    title: str
    path: str | None = None
    stable_url: str | None = None
    score: int
    model_config = ConfigDict(extra="forbid")


class MCPGuidePayload(GuidePayload):
    hot_concepts: list[MCPHotConceptEntry] = Field(default_factory=list)


class MCPContextCandidate(BaseModel):
    id: UUID
    title: str
    match_reason: str
    stable_url: str
    link_markdown: str
    model_config = ConfigDict(extra="forbid")

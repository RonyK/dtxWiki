"""Shared typed payloads for Context merge results."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CONTENT_HASH_PATTERN = r"^[a-f0-9]{64}$"


class ContextMergeMetadata(BaseModel):
    title: str
    page_type: str
    tags: list[str]
    aliases: list[str]
    placement_parent_node_id: UUID | None
    model_config = ConfigDict(extra="forbid")


class ContextMergeConflict(BaseModel):
    ours: str
    theirs: str
    keep_both_start: int
    keep_both_end: int
    latest_start: int
    latest_end: int
    model_config = ConfigDict(extra="forbid")


class ContextMergeCandidate(BaseModel):
    body_md: str
    page_metadata: ContextMergeMetadata
    block_map: list[dict[str, Any]]
    result_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    conflicts: list[ContextMergeConflict]
    offset_unit: Literal["unicode_code_points"]
    conflict_body_md: str | None = None
    model_config = ConfigDict(extra="forbid")

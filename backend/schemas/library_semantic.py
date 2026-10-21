"""REST contracts for the editable Library semantic Markdown overlay."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.domain.input_policy import validate_body
from backend.schemas.common import RESTResponseBase
from backend.schemas.input_policy import InputRequest
from backend.schemas.page import BODY_MD_MAX_LENGTH

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
LIBRARY_SEMANTIC_MARKDOWN_MAX_BYTES = BODY_MD_MAX_LENGTH


class LibrarySemanticMarkdownPayload(BaseModel):
    library_item_id: UUID
    version_id: UUID
    base_artifact_id: UUID
    base_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    overlay_id: UUID | None = None
    revision: int = Field(ge=0)
    source: Literal["base", "overlay"]
    body_markdown: str
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_size: int = Field(ge=0)
    updated_at: datetime

    model_config = ConfigDict(extra="forbid")


class LibrarySemanticMarkdownUpdateRequest(InputRequest):
    body_markdown: str = Field(min_length=1, max_length=BODY_MD_MAX_LENGTH)
    expected_revision: int = Field(ge=0)
    expected_base_artifact_id: UUID
    expected_base_content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("body_markdown")
    @classmethod
    def _bounded_utf8_body(cls, value: str) -> str:
        validate_body(value)
        if len(value.encode("utf-8")) > LIBRARY_SEMANTIC_MARKDOWN_MAX_BYTES:
            raise ValueError("body_markdown exceeds the 1000000-byte UTF-8 limit")
        return value

    model_config = ConfigDict(extra="forbid")


class LibrarySemanticMarkdownResponse(RESTResponseBase):
    data: LibrarySemanticMarkdownPayload

    model_config = ConfigDict(extra="forbid")


__all__ = [
    "LIBRARY_SEMANTIC_MARKDOWN_MAX_BYTES",
    "LibrarySemanticMarkdownPayload",
    "LibrarySemanticMarkdownResponse",
    "LibrarySemanticMarkdownUpdateRequest",
]

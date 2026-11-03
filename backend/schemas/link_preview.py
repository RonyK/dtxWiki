"""Read-only Markdown projection for an unsaved editor buffer."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.domain.input_policy import BODY_MAX, validate_body
from backend.schemas.common import RESTResponseBase
from backend.schemas.document_reference import ReferenceView
from backend.schemas.read import ContextLinkTarget


class LinkPreviewRequest(BaseModel):
    body_md: str = Field(max_length=BODY_MAX)
    source_page_id: UUID | None = None
    source_version_id: UUID | None = None
    request_id: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid")

    @field_validator("body_md")
    @classmethod
    def validate_markdown(cls, value: str) -> str:
        return validate_body(value)


class LinkPreviewPayload(BaseModel):
    workspace_id: UUID
    source_page_id: UUID | None
    source_version_id: UUID | None
    request_id: str
    link_targets: list[ContextLinkTarget] = Field(default_factory=list)
    references: list[ReferenceView] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class LinkPreviewResponse(RESTResponseBase):
    data: LinkPreviewPayload

    model_config = ConfigDict(extra="forbid")

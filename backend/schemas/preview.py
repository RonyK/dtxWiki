"""REST wire contracts for typed Library Preview manifests."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase


class PreviewVariantPayload(BaseModel):
    id: UUID | None = None
    kind: Literal[
        "markdown",
        "text",
        "table",
        "pdf",
        "image",
        "audio",
        "video",
        "metadata",
    ]
    media_type: str
    delivery: Literal["inline", "range", "text"]
    content_url: str
    renderer_contract: str
    rank: int = Field(ge=0)
    byte_size: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    duration_ms: int | None = Field(default=None, ge=1)
    page_count: int | None = Field(default=None, ge=1)
    source: Literal["generated", "semantic", "original"]
    provider_id: str | None = None
    provider_version: str | None = None
    codecs: tuple[str, ...] | None = Field(
        default=None,
        max_length=16,
        exclude_if=lambda value: value is None,
    )

    model_config = ConfigDict(extra="forbid")


class LibraryPreviewManifestPayload(BaseModel):
    schema_version: Literal["1"] = "1"
    status: Literal[
        "queued",
        "generating",
        "ready",
        "ready_partial",
        "unavailable",
        "failed",
        "timed_out",
    ]
    version_id: UUID
    selected_preview_attempt_id: UUID | None = None
    variants: tuple[PreviewVariantPayload, ...]
    fallbacks: tuple[Literal["metadata", "download"], ...]
    unavailable_reason: str | None = None
    download_url: str | None = None
    is_current_preview: bool

    model_config = ConfigDict(extra="forbid")


class LibraryPreviewManifestResponse(RESTResponseBase):
    data: LibraryPreviewManifestPayload

    model_config = ConfigDict(extra="forbid")


__all__ = [
    "LibraryPreviewManifestPayload",
    "LibraryPreviewManifestResponse",
    "PreviewVariantPayload",
]

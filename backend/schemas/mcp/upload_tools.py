from __future__ import annotations

import base64
import binascii
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.context_media.domain.image_formats import ContextImageMediaType
from backend.context_media.domain.policy import MAX_CONTEXT_IMAGE_UPLOAD_BYTES
from backend.schemas.common import MCPResultEnvelope
from backend.schemas.mcp.page_input import MCPPageInput

ImageMimeType = ContextImageMediaType
MCP_MAX_IMAGE_BYTES = MAX_CONTEXT_IMAGE_UPLOAD_BYTES
MAX_ENCODED_IMAGE_CHARS = ((MCP_MAX_IMAGE_BYTES + 2) // 3) * 4
# Built once at import so the decoder never formats a payload-adjacent message
# dynamically. The rejected field is the image payload, and this message may
# travel to the client and into a trace summary.
_OVERSIZE_MESSAGE = (
    f"content_base64 exceeds the {MCP_MAX_IMAGE_BYTES} decoded-byte limit"
)


class UploadImageInput(BaseModel):
    workspace_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    mime_type: ImageMimeType
    content_base64: str = Field(min_length=1, max_length=MAX_ENCODED_IMAGE_CHARS)
    marker_uuid: UUID
    page_id: MCPPageInput
    editing_version_id: UUID = Field(
        description=(
            "The caller's own currently-open editing version for this page. "
            "The image is registered against it; uploading requires an open "
            "editing session, not merely page access."
        ),
    )
    alt_text: str | None = Field(default=None, max_length=1000)
    conversation_token: str = Field(
        min_length=1,
        max_length=160,
        description=(
            "Required opaque MCP conversation token. Echo one from an earlier "
            "envelope or pass the literal 'new'."
        ),
    )

    @field_validator("filename")
    @classmethod
    def _filename_is_plain(cls, value: str) -> str:
        if value != value.strip() or any(
            char in value for char in ("/", "\\", "\x00")
        ):
            raise ValueError(
                "filename must be a plain filename without path separators"
            )
        return value

    model_config = ConfigDict(extra="forbid")


class UploadImageResult(BaseModel):
    asset_id: UUID
    logical_ref: str = Field(
        pattern=(
            r"^dtx-media://asset/"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    )
    content_url: str = Field(
        pattern=(
            r"^/api/v1/workspaces/[0-9a-f-]+/context/pages/[0-9a-f-]+"
            r"/versions/[0-9a-f-]+/media/[0-9a-f-]+/content$"
        )
    )
    markdown: str
    mime_type: ImageMimeType
    bytes: int = Field(ge=1, le=MCP_MAX_IMAGE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    marker_uuid: UUID
    reused: bool

    model_config = ConfigDict(extra="forbid")


class UploadImageToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: UploadImageResult | None = None

    model_config = ConfigDict(extra="forbid")


def decode_image_base64(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("content_base64 must be valid RFC 4648 base64") from exc
    if not decoded:
        raise ValueError("content_base64 must decode to a non-empty image")
    if len(decoded) > MCP_MAX_IMAGE_BYTES:
        raise ValueError(_OVERSIZE_MESSAGE)
    return decoded

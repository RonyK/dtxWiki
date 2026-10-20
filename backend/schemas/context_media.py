"""REST wire contracts for the Context attachment MVP."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.context_media.domain.image_formats import ContextImageMediaType
from backend.schemas.common import RESTResponseBase


class ContextMediaCapabilitiesPayload(BaseModel):
    policy_revision: str
    uploadable_media_types: tuple[ContextImageMediaType, ...]
    max_upload_bytes: int = Field(ge=1)
    # Additive for newer servers; optional so clients can still validate the
    # original capability payload and fall back to sequential uploads.
    max_files_per_batch: int | None = Field(default=None, ge=1)
    max_batch_bytes: int | None = Field(default=None, ge=1)
    max_parallel_uploads: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(extra="forbid")


class ContextMediaCapabilitiesResponse(RESTResponseBase):
    data: ContextMediaCapabilitiesPayload


class ContextMediaUploadPayload(BaseModel):
    asset_id: UUID
    logical_ref: str
    content_url: str
    mime: ContextImageMediaType
    bytes: int = Field(ge=1)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")

    model_config = ConfigDict(extra="forbid")


class ContextMediaUploadResponse(RESTResponseBase):
    data: ContextMediaUploadPayload


class ContextMediaAssetPayload(BaseModel):
    id: UUID
    logical_ref: str
    original_filename: str
    media_type: ContextImageMediaType
    bytes: int = Field(ge=0)
    association_state: Literal["pending_install", "active", "tombstoned"]
    usage_state: Literal["active", "history_only", "unused"]
    latest_draft_reference_count: int = Field(ge=0)
    current_published_reference_count: int = Field(ge=0)
    historical_reference_count: int = Field(ge=0)
    can_delete: bool
    usage_token: str
    content_url: str

    model_config = ConfigDict(extra="forbid")


class ContextMediaAssetListResponse(RESTResponseBase):
    data: tuple[ContextMediaAssetPayload, ...]


class ContextMediaDeletePayload(BaseModel):
    id: UUID
    association_state: Literal["tombstoned"] = "tombstoned"
    gc_state: Literal["queued"] = "queued"

    model_config = ConfigDict(extra="forbid")


class ContextMediaDeleteResponse(RESTResponseBase):
    data: ContextMediaDeletePayload


class ContextMediaOrphanPayload(BaseModel):
    """A page-owned media asset written into no version's Markdown -- either
    never (an upload abandoned mid-edit, the usual case) or no longer.

    An asset still protected by an open "editing" session (P3.4:
    ``created_for_version_id`` names a version whose own status is
    ``"editing"``) is never listed here at all -- that is an upload in
    progress, not an orphan.

    Not retention-gated -- every current orphan is listed regardless of
    age; only automatic removal (the ``context_media_orphan_sweep`` job) is
    gated by ``DTXWIKI_CONTEXT_MEDIA_ORPHAN_RETENTION_HOURS``.
    """

    asset_id: UUID
    workspace_id: UUID
    page_id: UUID
    page_title: str | None
    """Carried so the screen can name the page instead of printing its id:
    an operator can act on "Release Notes Draft" and cannot act on a
    UUID."""
    uploaded_by_display_name: str
    """Always resolvable -- ``resolve_actor_display_names`` falls back to a
    role label ("MCP client", "Agent worker") for non-human actors rather
    than returning nothing, so this is never null and the UI needs no
    fallback branch."""
    original_filename: str
    media_type: ContextImageMediaType
    bytes: int = Field(ge=0)
    association_state: Literal["pending_install", "active", "tombstoned"]
    created_by_actor_kind: str
    created_by_actor_id: str
    created_at: datetime
    orphaned_since: datetime

    model_config = ConfigDict(extra="forbid")


class ContextMediaOrphanListData(BaseModel):
    items: tuple[ContextMediaOrphanPayload, ...]

    model_config = ConfigDict(extra="forbid")


class ContextMediaOrphanListResponse(RESTResponseBase):
    data: ContextMediaOrphanListData


class ContextMediaAttachmentPayload(BaseModel):
    """One row of the workspace attachment listing.

    Two deliberate departures from ``ContextMediaOrphanPayload``:

    ``media_type`` is a plain ``str`` and ``kind`` is carried alongside it. The
    orphan payload narrows the type to ``ContextImageMediaType`` (four image
    literals), but ``context_media_asset.kind`` already admits ``audio`` and
    ``video``. A workspace-wide listing is exactly where the first non-image
    asset would appear, and a ``Literal`` there turns that row into a 500 on
    response validation rather than a row the reader can act on.

    ``can_delete`` answers "may THIS caller delete this now", not just the
    domain's usage question -- the page-scoped listing can publish the bare
    domain answer because that route is editor-gated, while this one is
    readable by a viewer.
    """

    asset_id: UUID
    workspace_id: UUID
    page_id: UUID
    page_title: str | None
    created_for_version_id: UUID
    logical_ref: str
    original_filename: str
    kind: Literal["image", "audio", "video"]
    media_type: str
    bytes: int = Field(ge=0)
    association_state: Literal["pending_install", "active", "tombstoned"]
    usage_state: Literal["active", "history_only", "unused"]
    latest_draft_reference_count: int = Field(ge=0)
    current_published_reference_count: int = Field(ge=0)
    historical_reference_count: int = Field(ge=0)
    is_orphan: bool
    is_mine: bool
    can_delete: bool
    usage_token: str
    content_url: str | None
    uploaded_by_display_name: str
    """Never null: ``resolve_actor_display_names`` falls back to a role label
    ("MCP client", "Agent worker") for non-human actors, so the UI needs no
    fallback branch."""
    created_by_actor_kind: str
    created_by_actor_id: str
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class ContextMediaRetentionPolicyPayload(BaseModel):
    orphan_retention_hours: int | None = Field(ge=0)
    effective_orphan_retention_hours: int = Field(ge=0)
    default_orphan_retention_hours: int = Field(ge=0)
    max_orphan_retention_hours: int = Field(gt=0)
    source: Literal["server", "workspace"]
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")

    model_config = ConfigDict(extra="forbid")


class ContextMediaAttachmentListData(BaseModel):
    items: tuple[ContextMediaAttachmentPayload, ...]
    next_cursor: str | None = Field(
        description=(
            "Keyset cursor for the next page, or null when this is the last "
            "page. Non-null does not imply `items` is non-empty: with a "
            "`usage` filter applied, a page can legitimately contain zero "
            "matching items while more unfiltered rows -- and possibly more "
            "matches -- remain beyond it. Always follow a non-null cursor "
            "rather than stopping on an empty `items` list."
        )
    )
    retention_policy: ContextMediaRetentionPolicyPayload | None = None
    scope: Literal["mine", "workspace"]
    """Echoed back so the client can tell a "no attachments" answer for the
    whole workspace apart from one for just this uploader."""

    model_config = ConfigDict(extra="forbid")


class ContextMediaAttachmentListResponse(RESTResponseBase):
    data: ContextMediaAttachmentListData


class AdminContextMediaAttachmentPayload(ContextMediaAttachmentPayload):
    workspace_title: str


class AdminContextMediaAttachmentListData(BaseModel):
    items: tuple[AdminContextMediaAttachmentPayload, ...]
    next_cursor: str | None
    scope: Literal["server"] = "server"

    model_config = ConfigDict(extra="forbid")


class AdminContextMediaAttachmentListResponse(RESTResponseBase):
    data: AdminContextMediaAttachmentListData

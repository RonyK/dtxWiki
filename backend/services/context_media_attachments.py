"""Wire projection and keyset cursor for the workspace attachment listing.

The api layer never imports ``backend.models`` or the context-media domain
directly; this module is the seam, the same role
``context_media_orphan_sweep.orphan_read_projection`` plays for the orphan
listing.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from backend.context_media.domain import ContextMediaAttachmentProjection

_CURSOR_PREFIX = "attach1:"


def encode_attachment_cursor(created_at: datetime, asset_id: UUID) -> str:
    """Opaque keyset cursor for ``(created_at DESC, asset_id DESC)``."""

    raw = f"{_CURSOR_PREFIX}{created_at.isoformat()}:{asset_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_attachment_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a cursor, or ``None`` to start from the first page.

    Tolerant on purpose: a stale, truncated or hand-edited cursor restarts the
    listing instead of 500ing. Same policy as ``_decode_job_cursor``.
    """

    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not raw.startswith(_CURSOR_PREFIX):
        return None
    payload = raw[len(_CURSOR_PREFIX) :]
    # rsplit: an ISO timestamp contains colons of its own.
    timestamp, separator, asset = payload.rpartition(":")
    if not separator:
        return None
    try:
        created_at = datetime.fromisoformat(timestamp)
        asset_id = UUID(asset)
    except ValueError:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at, asset_id


def attachment_content_url(
    *, workspace_id: UUID, page_id: UUID, version_id: UUID, asset_id: UUID
) -> str:
    """The existing viewer-role content route for one asset.

    Built here rather than adding a workspace-wide content endpoint: every
    asset carries a NOT NULL ``page_id`` and ``created_for_version_id``, so the
    per-version URL is always derivable and already serves these bytes.
    """

    return (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
        f"/versions/{version_id}/media/{asset_id}/content"
    )


def attachment_read_projection(
    projection: ContextMediaAttachmentProjection,
    *,
    workspace_id: UUID,
    uploaded_by_display_name: str,
    viewer_actor_kind: str,
    viewer_actor_id: str,
    caller_may_delete: bool,
) -> dict[str, Any]:
    """The wire shape for one attachment row.

    ``uploaded_by_display_name`` is a required keyword for the same reason it is
    on ``orphan_read_projection``: it comes from a separate identity-plane read,
    and forgetting it should fail loudly rather than quietly render a raw actor
    id at a person.

    ``caller_may_delete`` narrows the domain's own ``can_delete``. The
    page-scoped listing can publish the domain answer unqualified because that
    route is already editor-gated; this one is readable by a viewer, so without
    the role term the UI would offer a delete button that 403s.
    """

    asset = projection.asset
    record = asset.asset
    return {
        "asset_id": record.asset_id,
        "workspace_id": record.workspace_id,
        "page_id": record.page_id,
        "page_title": projection.page_title,
        "created_for_version_id": record.created_for_version_id,
        "logical_ref": record.logical_reference,
        "original_filename": record.original_filename,
        "kind": projection.kind,
        "media_type": record.canonical_media_type,
        "bytes": record.logical_bytes,
        "association_state": record.association_state.value,
        "usage_state": asset.usage_state.value,
        "latest_draft_reference_count": asset.latest_draft_reference_count,
        "current_published_reference_count": (
            asset.current_published_reference_count
        ),
        "historical_reference_count": asset.historical_reference_count,
        "is_orphan": projection.is_orphan,
        "is_mine": (
            record.created_by.kind == viewer_actor_kind
            and str(record.created_by.id) == viewer_actor_id
        ),
        "can_delete": asset.can_delete and caller_may_delete,
        "usage_token": asset.usage_token,
        "content_url": attachment_content_url(
            workspace_id=workspace_id,
            page_id=record.page_id,
            version_id=record.created_for_version_id,
            asset_id=record.asset_id,
        ),
        "uploaded_by_display_name": uploaded_by_display_name,
        "created_by_actor_kind": record.created_by.kind,
        "created_by_actor_id": str(record.created_by.id),
        "created_at": projection.created_at,
    }

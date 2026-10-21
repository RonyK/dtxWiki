"""Explicit server-operator attachment metadata and orphan cleanup.

CONTROL supplies the roster; assets are always read through session_for(id).
The operator view grants metadata/cleanup, never a content-preview capability.
"""

from __future__ import annotations

import base64
import binascii
import json
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.context_media.application import DeleteUnusedMedia
from backend.context_media.domain import (
    ActorIdentity,
    ContextMediaAssetProjection,
    ContextMediaAttachmentProjection,
    ContextMediaError,
    ContextMediaNotFoundError,
    MediaInUseError,
    StaleUsageTokenError,
)
from backend.models.workspace import Workspace
from backend.repositories.admin_context_media_repo import AdminAttachmentRepository
from backend.services.audit_service import AuditService
from backend.services.context_media_attachments import (
    attachment_read_projection,
    decode_attachment_cursor,
    encode_attachment_cursor,
)
from backend.utils.actor_display import resolve_actor_display_names

# Export the error boundary without making the API layer depend on domain types.
AdminAttachmentError = ContextMediaError
AdminAttachmentUnavailable = SQLAlchemyError


def _encode_cursor(workspace_id, position, filters):
    return base64.urlsafe_b64encode(
        json.dumps(
            {
                "v": 1,
                "workspace": str(workspace_id),
                "after": encode_attachment_cursor(*position),
                "filters": filters,
            },
            separators=(",", ":"),
        ).encode()
    ).decode()


def _decode_cursor(cursor, filters):
    if cursor is None:
        return None, None
    try:
        if len(cursor) > 2048:
            raise ValueError
        raw = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        position = decode_attachment_cursor(raw["after"])
        if raw["v"] != 1 or raw["filters"] != filters or position is None:
            raise ValueError
        return UUID(raw["workspace"]), position
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid attachment cursor or changed filters") from exc


def list_admin_attachments(
    *,
    db,
    repository_factory,
    actor_id: UUID,
    q: str = "",
    mine=False,
    orphan_only=False,
    limit=24,
    cursor=None,
):
    filters = sha256(
        json.dumps(
            [q.strip(), mine, orphan_only],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    start_workspace, after = _decode_cursor(cursor, filters)
    with db.control_session_factory()() as control:
        statement = select(Workspace.id, Workspace.title).order_by(Workspace.id)
        if start_workspace is not None:
            statement = statement.where(Workspace.id >= start_workspace)
        roster = control.execute(statement).all()
    # Stable workspace-id order, then the existing per-workspace keyset order.
    # Fetch one probe row across the boundary so a short workspace is not EOF.
    rows = []
    for workspace_id, title in roster:
        # Fail without advancing past any unavailable shard.
        with db.session_for(workspace_id)() as session:
            fetched = AdminAttachmentRepository(session, workspace_id).list_rows(
                actor_id=actor_id,
                q=q,
                mine=mine,
                orphan_only=orphan_only,
                after=after if workspace_id == start_workspace else None,
                limit=limit + 1 - len(rows),
            )
            repository = repository_factory(session)
            for asset, page_title, is_orphan in fetched:
                snapshot = repository.get_page_asset_usage(
                    workspace_id, asset.page_id, asset.id
                )
                if snapshot is None:
                    continue
                projection = ContextMediaAttachmentProjection(
                    asset=ContextMediaAssetProjection.from_snapshot(snapshot),
                    page_title=page_title,
                    kind=asset.kind,
                    created_at=asset.created_at,
                    is_orphan=bool(is_orphan),
                    preview_version_id=None,
                )
                rows.append((workspace_id, title, projection))
        if len(rows) > limit:
            break
    has_more = len(rows) > limit
    rows = rows[:limit]
    with db.control_session_factory()() as control:
        names = resolve_actor_display_names(
            control,
            (
                (p.asset.asset.created_by.kind, str(p.asset.asset.created_by.id))
                for _, _, p in rows
            ),
        )
    items = []
    for workspace_id, title, projection in rows:
        creator = projection.asset.asset.created_by
        item = attachment_read_projection(
            projection,
            workspace_id=workspace_id,
            uploaded_by_display_name=names[(creator.kind, str(creator.id))],
            viewer_actor_kind="human",
            viewer_actor_id=str(actor_id),
            caller_may_delete=projection.is_orphan,
        )
        item["workspace_title"] = title
        items.append(item)
    next_cursor = None
    if has_more:
        workspace_id, _, last = rows[-1]
        next_cursor = _encode_cursor(
            workspace_id, (last.created_at, last.asset.asset.asset_id), filters
        )
    return {"items": items, "next_cursor": next_cursor, "scope": "server"}


def delete_admin_attachment(
    *,
    db,
    repository_factory,
    workspace_id: UUID,
    asset_id: UUID,
    actor_id: UUID,
    usage_token: str,
):
    with db.control_session_factory()() as control:
        if control.get(Workspace, workspace_id) is None:
            raise ContextMediaNotFoundError("Workspace was not found")
    with db.session_for(workspace_id).begin() as session:
        scoped = AdminAttachmentRepository(session, workspace_id)
        asset = scoped.get_asset(asset_id)
        if asset is None:
            raise ContextMediaNotFoundError("Context media asset was not found")
        repository = repository_factory(session)
        snapshot = repository.get_page_asset_usage(
            workspace_id, asset.page_id, asset_id
        )
        if snapshot is None:
            raise ContextMediaNotFoundError("Context media asset was not found")
        if snapshot.usage_token != usage_token:
            raise StaleUsageTokenError("usage token is stale")
        orphan = scoped.is_orphan(asset_id)
        if not orphan:
            raise MediaInUseError("Asset is referenced or protected by an open edit")
        DeleteUnusedMedia().execute(
            repository,
            workspace_id=workspace_id,
            page_id=asset.page_id,
            asset_id=asset_id,
            usage_token=usage_token,
            actor=ActorIdentity("human", str(actor_id)),
        )
        AuditService().emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id=str(actor_id),
            action="admin.context_media.deleted",
            target_kind="context_media_asset",
            target_id=asset_id,
            payload={"page_id": str(asset.page_id)},
            event_summary="A server admin deleted an unused attachment",
        )

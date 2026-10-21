"""Membership-only, bounded workspace discovery for credential editors."""

import base64
import binascii
import hashlib
import hmac
import json
from uuid import UUID

from sqlalchemy import and_, case, collate, func, or_, select
from sqlalchemy.orm import Session

from backend.models.normalized_keys import normalize_search_key
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.db_read_snapshot import acquire_consistent_read_snapshot


def list_credential_workspaces(
    session: Session,
    *,
    user_id: UUID,
    secret: str,
    query: str = "",
    cursor: str | None = None,
    limit: int = 8,
):
    if not 1 <= limit <= 50:
        raise ValueError("invalid page size")
    acquire_consistent_read_snapshot(session)
    user = session.get(User, user_id)
    if user is None or user.status != "active":
        raise PermissionError("active user required")
    query = normalize_search_key(query)
    default_id = user.default_workspace_id
    context = [str(user_id), query, str(default_id), limit]
    title = collate(
        Workspace.directory_title_key,
        "C" if session.get_bind().dialect.name == "postgresql" else "BINARY",
    )
    rank = case((Workspace.id == default_id, 0), else_=1)
    filters = [WorkspaceMembership.user_id == user_id]
    if query:
        filters.append(
            or_(
                title.contains(query, autoescape=True),
                func.lower(Workspace.slug).contains(query, autoescape=True),
            )
        )
    base = (
        select(Workspace, WorkspaceMembership.role)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .where(*filters)
    )
    count = int(session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    if cursor:
        try:
            payload, signature = cursor.split(".")
            expected = hmac.new(
                secret.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            decoded = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            if decoded["context"] != context:
                raise ValueError("context")
            after_rank, after_title, after_id = decoded["after"]
            after_id = UUID(after_id)
        except (ValueError, TypeError, KeyError, binascii.Error) as exc:
            raise ValueError("invalid workspace cursor") from exc
        base = base.where(
            or_(
                rank > after_rank,
                and_(rank == after_rank, title > after_title),
                and_(rank == after_rank, title == after_title, Workspace.id > after_id),
            )
        )
    rows = session.execute(
        base.order_by(rank, title, Workspace.id).limit(limit + 1)
    ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        workspace = page[-1][0]
        payload = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "context": context,
                        "after": [
                            0 if workspace.id == default_id else 1,
                            workspace.directory_title_key,
                            str(workspace.id),
                        ],
                    },
                    separators=(",", ":"),
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        signature = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        next_cursor = f"{payload}.{signature}"
    return {
        "data": [
            {
                "id": w.id,
                "title": w.title,
                "slug": w.slug,
                "role": role,
                "is_default": w.id == default_id,
            }
            for w, role in page
        ],
        "total_count": count,
        "next_cursor": next_cursor,
    }

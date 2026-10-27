"""Server-level audit records, readable only through the server-admin API.

Workspace activity feeds select ``workspace_id == <workspace>``, so rows with
``workspace_id IS NULL`` (account, credential and server-admin changes) never
reach a workspace member. This service is their only reader.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select

from backend.models.audit import AuditEvent
from backend.services.audit_service import AuditService


class AdminAuditCursorError(ValueError):
    error_code = "invalid_audit_cursor"


def _encode_cursor(event: AuditEvent) -> str:
    raw = json.dumps([event.created_at.isoformat(), str(event.id)])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        created_at, event_id = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        )
        return datetime.fromisoformat(created_at), UUID(event_id)
    except (ValueError, TypeError, binascii.Error):
        raise AdminAuditCursorError("invalid audit cursor") from None


@dataclass
class AdminAuditService:
    session_factory: object

    def list_events(
        self,
        *,
        actor_user_id: UUID,
        action_prefix: str | None,
        target_user_id: UUID | None,
        actor_id: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict], str | None]:
        predicates = [AuditEvent.workspace_id.is_(None)]
        if action_prefix:
            predicates.append(
                AuditEvent.action.startswith(action_prefix, autoescape=True)
            )
        if target_user_id is not None:
            predicates.extend(
                [
                    AuditEvent.target_kind == "user",
                    AuditEvent.target_id == target_user_id,
                ]
            )
        if actor_id:
            predicates.append(AuditEvent.actor_id == actor_id)
        if since is not None:
            predicates.append(AuditEvent.created_at >= since)
        if until is not None:
            predicates.append(AuditEvent.created_at < until)
        if cursor:
            created_at, event_id = _decode_cursor(cursor)
            predicates.append(
                or_(
                    AuditEvent.created_at < created_at,
                    and_(AuditEvent.created_at == created_at, AuditEvent.id < event_id),
                )
            )
        with self.session_factory.begin() as session:
            rows = list(
                session.scalars(
                    select(AuditEvent)
                    .where(*predicates)
                    .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                    .limit(limit + 1)
                )
            )
            # Reading account history is itself privileged and recorded, like
            # the protected user directory search.
            AuditService().emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=str(actor_user_id),
                action="admin.audit.listed",
                target_kind="user",
                target_id=actor_user_id,
                payload={
                    "action_prefix": action_prefix,
                    "target_user_id": str(target_user_id) if target_user_id else None,
                    "actor_id": actor_id,
                    "page": bool(cursor),
                },
                retention_class="sensitive",
            )
            page = rows[:limit]
            items = [
                {
                    "event_id": event.id,
                    "created_at": event.created_at,
                    "action": event.action,
                    "actor_kind": event.actor_kind,
                    "actor_id": event.actor_id,
                    "target_kind": event.target_kind,
                    "target_id": event.target_id,
                    "summary": event.event_summary,
                    "payload": event.payload,
                }
                for event in page
            ]
            return items, (_encode_cursor(page[-1]) if len(rows) > limit else None)

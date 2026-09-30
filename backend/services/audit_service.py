from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.audit import AuditEvent
from backend.repositories.audit_repo import insert_audit_event


class AuditService:
    def emit(
        self,
        session: Session,
        workspace_id: UUID | None,
        actor_kind: str,
        actor_id: str,
        action: str,
        target_kind: str,
        target_id: UUID,
        payload: dict[str, Any] | None = None,
        event_summary: str | None = None,
        event_payload_ref: str | None = None,
        retention_class: str = "standard",
    ) -> AuditEvent:
        return insert_audit_event(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            payload=payload,
            event_summary=event_summary or action,
            event_payload_ref=event_payload_ref,
            retention_class=retention_class,
        )

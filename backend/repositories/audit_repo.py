from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.audit import AuditEvent


def insert_audit_event(
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
    event = AuditEvent(
        workspace_id=workspace_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        actor_type=actor_kind,
        action=action,
        target_kind=target_kind,
        target_type=target_kind,
        target_id=target_id,
        payload=payload,
        event_summary=event_summary or action,
        event_payload_ref=event_payload_ref,
        retention_class=retention_class,
    )
    session.add(event)
    session.flush()
    return event


def select_workspace_audit_events(workspace_id: UUID, *, conditions=()):
    """Read one workspace's audit rows, excluding global/detached history.

    AuditEvent is deliberately a mixed-plane table with nullable scope, so
    the workspace-plane model helpers cannot express this scoped projection.
    A correlated workspace column is supported like the scoped query helpers.
    """
    if workspace_id is None:
        raise ValueError("a workspace scope is required for audit projection")
    return select(AuditEvent).where(
        AuditEvent.workspace_id == workspace_id, *conditions
    )

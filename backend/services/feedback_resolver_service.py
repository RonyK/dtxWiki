from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.audit import AuditEvent
from backend.models.feedback import FeedbackItem
from backend.repositories.workspace_scoped import get_in_workspace
from backend.services.audit_service import AuditService
from backend.services.feedback_errors import FeedbackItemNotFoundError

INTAKE_ACKNOWLEDGED: Final[str] = "intake_acknowledged"
"""Resolver decision for PoC feedback intake without a verdict."""


@dataclass(frozen=True)
class FeedbackResolutionResult:
    feedback_item_id: UUID
    feedback_status: str
    audit_event_id: UUID


class FeedbackResolverService:
    def __init__(self, audit_service: AuditService | None = None) -> None:
        self.audit_service = audit_service or AuditService()

    def resolve(
        self,
        session: Session,
        workspace_id: UUID,
        feedback_item_id: UUID,
        actor_id: str,
    ) -> FeedbackResolutionResult:
        feedback = get_in_workspace(
            session,
            FeedbackItem,
            workspace_id=workspace_id,
            resource_id=feedback_item_id,
        )
        if feedback is None:
            raise FeedbackItemNotFoundError(
                feedback_item_id=str(feedback_item_id),
                workspace_id=str(workspace_id),
            )

        existing = session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == "feedback.resolve",
                AuditEvent.target_kind == "feedback_item",
                AuditEvent.target_id == feedback.id,
                AuditEvent.actor_kind == "agent",
                AuditEvent.actor_id == actor_id,
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        if existing is not None:
            return FeedbackResolutionResult(
                feedback_item_id=feedback.id,
                feedback_status=feedback.status,
                audit_event_id=existing.id,
            )

        event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind="agent",
            actor_id=actor_id,
            action="feedback.resolve",
            target_kind="feedback_item",
            target_id=feedback.id,
            payload={
                "decision": INTAKE_ACKNOWLEDGED,
                "feedback_status": feedback.status,
                "severity": feedback.severity,
            },
        )
        return FeedbackResolutionResult(
            feedback_item_id=feedback.id,
            feedback_status=feedback.status,
            audit_event_id=event.id,
        )

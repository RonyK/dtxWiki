from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.audit import AuditEvent
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.job import Job
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.services.audit_service import AuditService
from backend.services.db_write_intent import acquire_write_intent
from backend.services.job_queue import JobQueueService
from backend.services.review_risk_policy import normalized_review_policy

RECONCILIATION_REMINDER_JOB_TYPE = "context_reconciliation_reminder"
RECONCILIATION_REMINDER_AUDIT_ACTION = "context_page.reconciliation.reminder"
_LIVE_RECONCILIATION_REVIEW_STATUSES = frozenset({"pending", "changes_requested"})


@dataclass(frozen=True)
class ReconciliationReminderResult:
    outcome: str
    unresolved: bool
    activity_created: bool
    activity_event_id: UUID | None = None
    page_title: str | None = None


def reconciliation_reminder_idempotency_key(branch_version_id: UUID) -> str:
    return f"context_reconciliation_reminder:{branch_version_id}"


def reconciliation_reminder_event_ref(branch_version_id: UUID) -> str:
    return f"context_reconciliation_reminder:{branch_version_id}"


def enqueue_reconciliation_reminder(
    *,
    session: Session,
    job_queue: JobQueueService,
    workspace_id: UUID,
    page_id: UUID,
    branch_version_id: UUID,
    review_item_id: UUID,
) -> Job:
    """Persist the one delayed reminder owned by a diverged branch.

    The Job is inserted in the same workspace transaction as the branch and its
    reconciliation ReviewItem. ``next_attempt_at`` is the durable delay; no
    periodic scan has to rediscover branches later. The branch id is the job's
    idempotency identity, so replaying branch persistence cannot create another
    reminder.
    """

    workspace = session.scalar(select(Workspace).where(Workspace.id == workspace_id))
    branch = session.scalar(
        select(ContextPageVersion).where(
            ContextPageVersion.workspace_id == workspace_id,
            ContextPageVersion.page_id == page_id,
            ContextPageVersion.id == branch_version_id,
        )
    )
    if workspace is None or branch is None:
        raise ValueError("reconciliation reminder target is unavailable")

    reminder_after_hours = int(
        normalized_review_policy(workspace.review_policy)["reconciliation"][
            "reminder_after_hours"
        ]
    )
    due_at = branch.created_at + timedelta(hours=reminder_after_hours)
    expected_target = {
        "page_id": str(page_id),
        "branch_version_id": str(branch_version_id),
        "review_item_id": str(review_item_id),
        "scheduled_for": due_at.isoformat(),
        "reminder_after_hours": reminder_after_hours,
    }
    job = job_queue.enqueue(
        session=session,
        workspace_id=workspace_id,
        job_type=RECONCILIATION_REMINDER_JOB_TYPE,
        trigger_type="context_reconciliation_unresolved",
        idempotency_key=reconciliation_reminder_idempotency_key(branch_version_id),
        target_ref=expected_target,
    )
    if dict(job.target_ref or {}) != expected_target:
        raise RuntimeError("reconciliation reminder idempotency target mismatch")
    if job.status == "queued":
        # Deterministic for this branch, so duplicate enqueue callers may safely
        # converge on the same timestamp. Never rewrite terminal/running work.
        job.next_attempt_at = due_at
    session.flush()
    return job


class ContextReconciliationReminderService:
    def __init__(self, audit_service: AuditService | None = None) -> None:
        self.audit_service = audit_service or AuditService()

    def execute(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        page_id: UUID,
        branch_version_id: UUID,
        review_item_id: UUID,
        reminder_after_hours: int,
    ) -> ReconciliationReminderResult:
        """Revalidate one exact branch and create its in-app reminder once.

        The page then branch row locks match reconciliation/discard's global
        lock order and serialize both concurrent Job claims and a simultaneous
        terminal transition. A stable audit
        payload reference is the replay ledger: if phase-1 business work commits
        but the worker's later Job-status update fails, the next attempt sees the
        existing event and cannot create a duplicate Activity row.
        """

        acquire_write_intent(session)
        page = session.scalar(
            select(ContextPage)
            .where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.id == page_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        branch = session.scalar(
            select(ContextPageVersion)
            .where(
                ContextPageVersion.workspace_id == workspace_id,
                ContextPageVersion.page_id == page_id,
                ContextPageVersion.id == branch_version_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        review = session.scalar(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.id == review_item_id,
            )
        )
        if branch is None or page is None or review is None:
            return ReconciliationReminderResult(
                outcome="target_missing",
                unresolved=False,
                activity_created=False,
            )
        if (
            branch.version_status != "draft"
            or branch.graph_status != "branched"
            or branch.change_set_id != review.change_set_id
            or review.review_type != "context_reconciliation"
            or review.status not in _LIVE_RECONCILIATION_REVIEW_STATUSES
        ):
            return ReconciliationReminderResult(
                outcome="already_resolved",
                unresolved=False,
                activity_created=False,
                page_title=page.title,
            )

        event_ref = reconciliation_reminder_event_ref(branch_version_id)
        existing_event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == RECONCILIATION_REMINDER_AUDIT_ACTION,
                AuditEvent.target_kind == "context_page",
                AuditEvent.target_id == page_id,
                AuditEvent.event_payload_ref == event_ref,
            )
        )
        if existing_event is not None:
            return ReconciliationReminderResult(
                outcome="already_reminded",
                unresolved=True,
                activity_created=False,
                activity_event_id=existing_event.id,
                page_title=page.title,
            )

        event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind="system",
            actor_id="worker:context_reconciliation_reminder",
            action=RECONCILIATION_REMINDER_AUDIT_ACTION,
            target_kind="context_page",
            target_id=page_id,
            payload={
                "target_title": page.title,
                "branch_version_id": str(branch_version_id),
                "review_item_id": str(review_item_id),
                "reminder_after_hours": reminder_after_hours,
            },
            event_summary=(
                "Reconciliation remains unresolved after "
                f"{reminder_after_hours} hours"
            ),
            event_payload_ref=event_ref,
        )
        return ReconciliationReminderResult(
            outcome="reminded",
            unresolved=True,
            activity_created=True,
            activity_event_id=event.id,
            page_title=page.title,
        )

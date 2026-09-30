from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.context import ContextPageVersion
from backend.models.job import Job
from backend.models.mail import EmailOutbox
from backend.models.review import ReviewItem
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.schemas.context_edit import PatchDraftRequest, ReplaceBlockOperation
from backend.services.context_reconciliation_reminder import (
    RECONCILIATION_REMINDER_AUDIT_ACTION,
    RECONCILIATION_REMINDER_JOB_TYPE,
    reconciliation_reminder_idempotency_key,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.mail_delivery import EmailDeliveryService
from backend.services.review_risk_policy import default_review_policy
from backend.utils.block_identity import block_map_for_version
from backend.worker.runtime import OneShotWorker


def _create_diverged_branch(
    page_service,
    *,
    slug: str,
    reminder_after_hours: int = 24,
):
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        policy = deepcopy(default_review_policy())
        policy["reconciliation"]["reminder_after_hours"] = reminder_after_hours
        workspace.review_policy = policy
        workspace_id = workspace.id

    base = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title="Reminder contract",
        body_md="alpha\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        base_map = block_map_for_version(base_version.block_map, base_version.body_md)
        target = next(entry for entry in base_map if entry["kind"] == "p")
    page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title="Reminder contract",
        body_md="alpha at head\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    with page_service.session_factory.begin() as session:
        diverged = ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(
                page_id=base.id,
                base=str(base.version_id),
                reason="create reminder branch",
                operations=[
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=target["id"],
                        expected=target["hash"],
                        content="alpha on branch",
                    )
                ],
            ),
            actor_kind="mcp_client",
            actor_id="reminder-test-client",
        )
    branch_id = diverged.alternative_draft_id
    assert branch_id is not None
    return workspace_id, base.id, branch_id


def _reminder_job(session, *, workspace_id, branch_id) -> Job:
    job = session.scalar(
        select(Job).where(
            Job.workspace_id == workspace_id,
            Job.job_type == RECONCILIATION_REMINDER_JOB_TYPE,
            Job.idempotency_key == reconciliation_reminder_idempotency_key(branch_id),
        )
    )
    assert job is not None
    return job


def test_diverged_branch_enqueues_one_policy_delayed_reminder(page_service):
    workspace_id, page_id, branch_id = _create_diverged_branch(
        page_service,
        slug="reconciliation-reminder-delay",
        reminder_after_hours=7,
    )

    with page_service.session_factory() as session:
        branch = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == workspace_id,
                ContextPageVersion.page_id == page_id,
                ContextPageVersion.id == branch_id,
            )
        )
        job = _reminder_job(
            session,
            workspace_id=workspace_id,
            branch_id=branch_id,
        )

    assert job.next_attempt_at == branch.created_at + timedelta(hours=7)
    assert job.target_ref == {
        "page_id": str(page_id),
        "branch_version_id": str(branch_id),
        "review_item_id": job.target_ref["review_item_id"],
        "scheduled_for": job.next_attempt_at.isoformat(),
        "reminder_after_hours": 7,
    }


def test_unresolved_worker_reminds_once_and_emails_only_verified_owners(page_service):
    workspace_id, page_id, branch_id = _create_diverged_branch(
        page_service,
        slug="reconciliation-reminder-worker",
    )
    with page_service.session_factory.begin() as session:
        verified = User(
            email="verified-reminder-owner@example.com",
            password_hash="not-used",
            display_name="Verified reminder owner",
            status="active",
            email_verified_at=datetime(2026, 8, 30, tzinfo=UTC),
        )
        unverified = User(
            email="unverified-reminder-owner@example.com",
            password_hash="not-used",
            display_name="Unverified reminder owner",
            status="active",
        )
        session.add_all([verified, unverified])
        session.flush()
        session.add_all(
            [
                WorkspaceMembership(
                    workspace_id=workspace_id,
                    user_id=verified.id,
                    role="owner",
                ),
                WorkspaceMembership(
                    workspace_id=workspace_id,
                    user_id=unverified.id,
                    role="owner",
                ),
            ]
        )
        job = _reminder_job(
            session,
            workspace_id=workspace_id,
            branch_id=branch_id,
        )
        job.next_attempt_at = None

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="reminder-test-secret",
        public_base_url="https://wiki.example.com",
        mail_provider="smtp",
        mail_from_address="noreply@example.com",
        smtp_host="smtp.example.com",
    )
    worker = OneShotWorker(
        settings=settings,
        email_delivery_service=EmailDeliveryService(settings=settings),
    )
    first = worker.dispatch_once_with_session_factory(
        page_service.session_factory,
        workspace_id=workspace_id,
        job_types={RECONCILIATION_REMINDER_JOB_TYPE},
    )
    assert first is not None
    assert first.status == "completed"
    assert first.detail["outcome"] == "reminded"
    assert first.detail["email_outbox_count"] == 1

    with page_service.session_factory.begin() as session:
        activity_count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == RECONCILIATION_REMINDER_AUDIT_ACTION,
                AuditEvent.target_id == page_id,
            )
        )
        outboxes = session.scalars(
            select(EmailOutbox).where(
                EmailOutbox.message_kind == "reconciliation_reminder"
            )
        ).all()
        assert activity_count == 1
        assert len(outboxes) == 1
        assert outboxes[0].recipient_user_id == verified.id

        # Simulate phase-1 commit followed by a lost Job completion update.
        job = _reminder_job(
            session,
            workspace_id=workspace_id,
            branch_id=branch_id,
        )
        job.status = "queued"
        job.started_at = None
        job.completed_at = None
        job.next_attempt_at = None

    replay = worker.dispatch_once_with_session_factory(
        page_service.session_factory,
        workspace_id=workspace_id,
        job_types={RECONCILIATION_REMINDER_JOB_TYPE},
    )
    assert replay is not None
    assert replay.detail["outcome"] == "already_reminded"
    assert replay.detail["email_outbox_count"] == 0
    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == RECONCILIATION_REMINDER_AUDIT_ACTION)
        ) == 1
        assert session.scalar(
            select(func.count())
            .select_from(EmailOutbox)
            .where(EmailOutbox.message_kind == "reconciliation_reminder")
        ) == 1


@pytest.mark.parametrize("terminal_graph_status", ["superseded", "discarded"])
def test_resolved_or_discarded_branch_is_a_worker_noop(
    page_service,
    terminal_graph_status,
):
    workspace_id, _, branch_id = _create_diverged_branch(
        page_service,
        slug=f"reconciliation-reminder-{terminal_graph_status}",
    )
    with page_service.session_factory.begin() as session:
        branch = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == workspace_id,
                ContextPageVersion.id == branch_id,
            )
        )
        review = session.scalar(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.change_set_id == branch.change_set_id,
                ReviewItem.review_type == "context_reconciliation",
            )
        )
        branch.graph_status = terminal_graph_status
        review.status = "superseded"
        job = _reminder_job(
            session,
            workspace_id=workspace_id,
            branch_id=branch_id,
        )
        job.next_attempt_at = None

    result = OneShotWorker().dispatch_once_with_session_factory(
        page_service.session_factory,
        workspace_id=workspace_id,
        job_types={RECONCILIATION_REMINDER_JOB_TYPE},
    )
    assert result is not None
    assert result.status == "completed"
    assert result.detail == {
        "outcome": "already_resolved",
        "unresolved": False,
        "activity_created": False,
        "activity_event_id": None,
    }
    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == RECONCILIATION_REMINDER_AUDIT_ACTION)
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(EmailOutbox)
            .where(EmailOutbox.message_kind == "reconciliation_reminder")
        ) == 0

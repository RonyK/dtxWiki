from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.audit import AuditEvent
from backend.models.feedback import FeedbackItem
from backend.models.job import Job
from backend.models.workspace import Workspace
from backend.schemas.context_write import SubmitFeedbackRequest, WriteDraftRequest
from backend.services.actor_identities import SYSTEM_FEEDBACK_RESOLVER_ACTOR_ID
from backend.services.context_write_service import ContextWriteService
from backend.services.feedback_errors import FeedbackItemNotFoundError
from backend.services.feedback_resolver_service import (
    INTAKE_ACKNOWLEDGED,
    FeedbackResolverService,
)
from backend.services.feedback_service import FeedbackService
from backend.worker.runtime import OneShotWorker

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _workspace(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


def _context_draft(session, workspace_id):
    return ContextWriteService().write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title="Feedback Resolution Target",
            page_type="overview",
            body_markdown="# Feedback Resolution Target\n\nOriginal body.",
            reason="create feedback resolution target",
        ),
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_role="owner",
    )


def _submit_feedback(session, workspace_id):
    draft = _context_draft(session, workspace_id)
    return FeedbackService().submit_feedback(
        session=session,
        workspace_id=workspace_id,
        request=SubmitFeedbackRequest(
            target_type="context_page",
            target_id=draft.page_id,
            body="Please add source evidence.",
            severity="major",
            suggested_action="add_source",
        ),
        actor_kind="human",
        actor_id=ACTOR_ID,
    )


def test_feedback_resolution_worker_dispatch_succeeds(session_factory):
    with session_factory.begin() as session:
        workspace = _workspace(session)
        feedback_result = _submit_feedback(session, workspace.id)
        feedback_id = feedback_result.feedback_id

    worker = OneShotWorker()
    with session_factory.begin() as session:
        dispatch = worker.dispatch_once(
            session=session,
            job_types={"feedback_resolution"},
        )

    assert dispatch is not None
    assert dispatch.job_type == "feedback_resolution"
    assert dispatch.status == "completed"
    assert dispatch.detail["feedback_item_id"] == str(feedback_id)
    assert dispatch.detail["feedback_status"] == "open"

    with session_factory.begin() as session:
        job = session.scalar(
            select(Job).where(
                Job.idempotency_key == f"feedback_resolution:{feedback_id}"
            )
        )
        feedback = session.get(FeedbackItem, feedback_id)
        audit_rows = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "feedback.resolve",
                AuditEvent.target_id == feedback_id,
            )
        ).all()

    assert job.status == "completed"
    assert job.retry_count == 0
    assert feedback.status == "open"
    assert len(audit_rows) == 1
    assert audit_rows[0].actor_id == str(SYSTEM_FEEDBACK_RESOLVER_ACTOR_ID)
    assert audit_rows[0].actor_kind == "agent"
    assert audit_rows[0].target_kind == "feedback_item"
    assert audit_rows[0].payload["decision"] == INTAKE_ACKNOWLEDGED
    assert audit_rows[0].payload["feedback_status"] == "open"
    assert audit_rows[0].payload["severity"] == "major"
    assert dispatch.detail["audit_event_id"] == str(audit_rows[0].id)


def test_feedback_resolution_worker_queue_drains(session_factory):
    with session_factory.begin() as session:
        workspace = _workspace(session)
        _submit_feedback(session, workspace.id)

    worker = OneShotWorker()
    with session_factory.begin() as session:
        first = worker.dispatch_once(
            session=session,
            job_types={"feedback_resolution"},
        )
        second = worker.dispatch_once(
            session=session,
            job_types={"feedback_resolution"},
        )

    assert first is not None
    assert first.status == "completed"
    assert second is None


def test_feedback_resolution_replay_reuses_the_original_audit_event(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = _workspace(session)
        feedback_result = _submit_feedback(session, workspace.id)
        feedback_id = feedback_result.feedback_id

    worker = OneShotWorker()
    with session_factory.begin() as session:
        first = worker.feedback_resolver.resolve(
            session=session,
            workspace_id=workspace.id,
            feedback_item_id=feedback_id,
            actor_id=str(SYSTEM_FEEDBACK_RESOLVER_ACTOR_ID),
        )

    with session_factory.begin() as session:
        replay = worker.feedback_resolver.resolve(
            session=session,
            workspace_id=workspace.id,
            feedback_item_id=feedback_id,
            actor_id=str(SYSTEM_FEEDBACK_RESOLVER_ACTOR_ID),
        )
        audit_rows = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "feedback.resolve",
                AuditEvent.target_id == feedback_id,
            )
        ).all()

    assert replay.audit_event_id == first.audit_event_id
    assert len(audit_rows) == 1


def test_feedback_resolve_cross_workspace_is_opaque_not_found_without_mutation(
    session_factory,
):
    resolver = FeedbackResolverService()

    with session_factory.begin() as session:
        source_workspace = _workspace(session)
        feedback_result = _submit_feedback(session, source_workspace.id)
        feedback_id = feedback_result.feedback_id
        requested_workspace = Workspace(
            slug="foreign-feedback-resolve",
            title="Foreign Feedback Resolve",
            name="Foreign Feedback Resolve",
        )
        session.add(requested_workspace)
        session.flush()
        requested_workspace_id = requested_workspace.id
        resolve_audits_before = session.scalars(
            select(AuditEvent.id).where(AuditEvent.action == "feedback.resolve")
        ).all()

        missing_id = uuid4()
        with pytest.raises(FeedbackItemNotFoundError) as missing_error:
            resolver.resolve(
                session=session,
                workspace_id=requested_workspace_id,
                feedback_item_id=missing_id,
                actor_id=str(SYSTEM_FEEDBACK_RESOLVER_ACTOR_ID),
            )
        with pytest.raises(FeedbackItemNotFoundError) as foreign_error:
            resolver.resolve(
                session=session,
                workspace_id=requested_workspace_id,
                feedback_item_id=feedback_id,
                actor_id=str(SYSTEM_FEEDBACK_RESOLVER_ACTOR_ID),
            )

        feedback = session.get(FeedbackItem, feedback_id)
        resolve_audits_after = session.scalars(
            select(AuditEvent.id).where(AuditEvent.action == "feedback.resolve")
        ).all()

    assert missing_error.value.workspace_id == str(requested_workspace_id)
    assert foreign_error.value.workspace_id == str(requested_workspace_id)
    assert str(source_workspace.id) not in str(foreign_error.value)
    assert feedback is not None
    assert feedback.status == "open"
    assert feedback.resolved_by_change_set_id is None
    assert resolve_audits_after == resolve_audits_before


def test_feedback_resolution_worker_terminal_fails_on_typed_not_found(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = _workspace(session)
        feedback_result = _submit_feedback(session, workspace.id)
        feedback_id = feedback_result.feedback_id

    with session_factory.begin() as session:
        feedback = session.get(FeedbackItem, feedback_id)
        assert feedback is not None
        session.delete(feedback)

    worker = OneShotWorker()
    with session_factory.begin() as session:
        dispatch = worker.dispatch_once(
            session=session,
            job_types={"feedback_resolution"},
        )

    assert dispatch is not None
    assert dispatch.status == "failed"
    assert dispatch.detail["terminal"] is True
    assert str(feedback_id) in dispatch.detail["error"]

    with session_factory.begin() as session:
        job = session.scalar(
            select(Job).where(
                Job.idempotency_key == f"feedback_resolution:{feedback_id}"
            )
        )

    assert job is not None
    assert job.status == "failed"
    assert job.retry_count == 1


def test_feedback_resolution_worker_terminal_fails_on_cross_workspace_lookup(
    session_factory,
):
    with session_factory.begin() as session:
        source_workspace = _workspace(session)
        feedback_result = _submit_feedback(session, source_workspace.id)
        feedback_id = feedback_result.feedback_id
        other_workspace = Workspace(
            slug="other-feedback-workspace",
            title="Other Feedback Workspace",
            name="Other Feedback Workspace",
        )
        session.add(other_workspace)
        session.flush()
        other_workspace_id = other_workspace.id
        job = Job(
            workspace_id=other_workspace_id,
            job_type="feedback_resolution",
            trigger_type="manual_test",
            idempotency_key=f"feedback_resolution_cross:{feedback_id}",
            target_ref={"target_id": str(feedback_id)},
        )
        session.add(job)
        session.flush()
        job_id = job.id

    worker = OneShotWorker()
    with session_factory.begin() as session:
        dispatch = worker.dispatch_once(
            session=session,
            workspace_id=other_workspace_id,
            job_types={"feedback_resolution"},
        )

    assert dispatch is not None
    assert dispatch.status == "failed"
    assert dispatch.detail["terminal"] is True
    assert str(feedback_id) in dispatch.detail["error"]
    assert str(other_workspace_id) in dispatch.detail["error"]

    with session_factory.begin() as session:
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == "failed"
    assert job.retry_count == 1

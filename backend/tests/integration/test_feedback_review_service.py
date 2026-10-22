from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage
from backend.models.feedback import FeedbackItem
from backend.models.job import Job
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.context_write import (
    ReviewDecisionRequest,
    SubmitFeedbackRequest,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.feedback_service import FeedbackService
from backend.services.read_errors import ReadNotFoundError
from backend.services.review_service import ReviewService

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _audit_actor() -> ActorContext:
    """A system actor may read audit-class content (feedback)."""
    return ActorContext(
        actor_kind="system",
        actor_id="feedback-test",
        scopes=("*",),
        workspace_role="owner",
        role="owner",
        token_kind="system",
    )


def _workspace(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


def _context_draft(session, workspace_id):
    return ContextWriteService().write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title="Feedback Target",
            page_type="overview",
            body_markdown="# Feedback Target\n\nOriginal body.",
            reason="create feedback target",
        ),
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_role="owner",
    )


def _review_for_draft(session, workspace_id):
    draft = _context_draft(session, workspace_id)
    return session.get(ReviewItem, draft.review_item_id)


def _viewer_actor() -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=ACTOR_ID,
        scopes=(),
        workspace_role="viewer",
        role="viewer",
        token_kind="session",
    )


def test_audit_title_redacted_for_actor_without_target_visibility(session_factory):
    # M-01: the Activity audit feed must not surface the title of a target the
    # actor cannot read directly. A draft-only Context page 404s for a viewer,
    # so its audit-event title must be redacted for the viewer and shown to the
    # owner.
    from backend.services.activity_service import (
        _audit_target_visible,
        target_title_for_audit_event,
    )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        draft = _context_draft(session, workspace.id)
        event = AuditEvent(
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id=ACTOR_ID,
            action="context_page.updated",
            target_kind="context_page",
            target_id=draft.page_id,
            payload={"target_title": "Feedback Target"},
        )
        session.add(event)
        session.flush()

        # The draft-only page is not visible to a viewer, so the row's target is
        # gated; an owner sees it and the title resolves.
        assert _audit_target_visible(session, event, _viewer_actor()) is False
        assert _audit_target_visible(session, event, _audit_actor()) is True
        assert target_title_for_audit_event(session, event) == "Feedback Target"


def test_audit_target_hidden_when_archived(session_factory):
    # Codex round-11: _audit_target_visible must reject archived targets. A page
    # or library item archived after the audit event was captured must not keep
    # surfacing its stored summary/title/id (visible_context_version_id alone
    # does not reject archived lifecycle).
    from backend.models.library import LibraryItem
    from backend.services.activity_service import _audit_target_visible

    with session_factory.begin() as session:
        workspace = _workspace(session)
        wsid = workspace.id
        archived_page = ContextPage(
            workspace_id=wsid,
            slug="archived-audit-target",
            title="Archived Target",
            page_type="overview",
            lifecycle_status="archived",
        )
        archived_item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/archived-audit.pdf",
            title="Archived Source",
            trust_level="trusted",
            lifecycle_status="archived",
        )
        live_item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/live-audit.pdf",
            title="Live Source",
            trust_level="trusted",
            lifecycle_status="registered",
        )
        session.add_all([archived_page, archived_item, live_item])
        session.flush()
        ctx_event = AuditEvent(
            workspace_id=wsid,
            actor_kind="human",
            actor_id=ACTOR_ID,
            action="context_page.updated",
            target_kind="context_page",
            target_id=archived_page.id,
            payload={"target_title": "Archived Target"},
        )
        lib_event = AuditEvent(
            workspace_id=wsid,
            actor_kind="human",
            actor_id=ACTOR_ID,
            action="library_item.updated",
            target_kind="library_item",
            target_id=archived_item.id,
            payload={"target_title": "Archived Source"},
        )
        live_event = AuditEvent(
            workspace_id=wsid,
            actor_kind="human",
            actor_id=ACTOR_ID,
            action="library_item.updated",
            target_kind="library_item",
            target_id=live_item.id,
            payload={"target_title": "Live Source"},
        )
        session.add_all([ctx_event, lib_event, live_event])
        session.flush()

        # Archived targets are hidden from everyone, including an audit-capable
        # owner; a live target still resolves (no over-tightening).
        owner = _audit_actor()
        assert _audit_target_visible(session, ctx_event, owner) is False
        assert _audit_target_visible(session, lib_event, owner) is False
        assert _audit_target_visible(session, live_event, owner) is True


def test_list_feedback_drops_feedback_about_hidden_library_target(session_factory):
    # Codex R13: feedback about a restricted-Library item must not re-serve that
    # target (id + body) to a feedback-capable reader who cannot read the item.
    from backend.models.library import (
        LibraryArtifact,
        LibraryArtifactConversionDetail,
        LibraryItem,
        LibraryItemVersion,
    )

    service = FeedbackService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        wsid = workspace.id
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/fb-restricted.pdf",
            title="Restricted FB Source",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()
        version = LibraryItemVersion(
            workspace_id=wsid,
            library_item_id=item.id,
            version_no=1,
            content_hash="h1",
            raw_storage_ref="inline://raw",
        )
        session.add(version)
        session.flush()
        item.current_version_id = version.id
        # session_factory (backend/tests/conftest.py) uses a bare
        # create_engine() with no PRAGMA foreign_keys=ON, so attempt_id/
        # blob_id below need not reference real LibraryAttempt/blob rows --
        # only the NOT NULL/CHECK-satisfying columns matter for this test.
        artifact = LibraryArtifact(
            workspace_id=wsid,
            library_item_version_id=version.id,
            attempt_id=uuid4(),
            stage="conversion",
            blob_workspace_id=wsid,
            blob_id=uuid4(),
            blob_epoch=1,
            role="markdown",
            kind="text",
            media_type="text/plain",
            content_sha256="a" * 64,
            byte_size=len("restricted body"),
        )
        artifact.conversion_detail = LibraryArtifactConversionDetail(
            is_complete=True,
            index_text="restricted body",
        )
        session.add(artifact)
        session.flush()
        artifact_id = artifact.id
        service.submit_feedback(
            session=session,
            workspace_id=wsid,
            request=SubmitFeedbackRequest(
                target_type="library_artifact",
                target_id=artifact_id,
                body="check this restricted source",
                severity="major",
                suggested_action="add_source",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

    editor = ActorContext(
        actor_kind="human",
        actor_id=ACTOR_ID,
        token_kind="session",
        scopes=("read",),
        workspace_id=wsid,
        workspace_role="editor",
        role="editor",
    )
    with session_factory() as session:
        editor_list = service.list_feedback(session, wsid, actor=editor)
        owner_list = service.list_feedback(session, wsid, actor=_audit_actor())

    # The editor cannot read a restricted Library item, so its feedback drops;
    # the audit-capable owner still sees it.
    assert all(entry.target_id != artifact_id for entry in editor_list)
    assert any(entry.target_id == artifact_id for entry in owner_list)


def test_feedback_service_creates_feedback_and_audit(session_factory):
    service = FeedbackService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        draft = _context_draft(session, workspace.id)
        result = service.submit_feedback(
            session=session,
            workspace_id=workspace.id,
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

        feedback = session.get(FeedbackItem, result.feedback_id)
        job = session.scalar(select(Job).where(Job.job_type == "feedback_resolution"))
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "feedback.submit")
        )

    assert result.status == "open"
    assert result.next_action == "agent_resolution_queued"
    assert feedback.target_id == draft.page_id
    assert feedback.severity == "major"
    assert job is not None
    assert job.status == "queued"
    assert job.trigger_type == "feedback.submit"
    assert job.idempotency_key == f"feedback_resolution:{feedback.id}"
    assert job.target_ref == {
        "target_kind": "feedback_item",
        "target_id": str(feedback.id),
    }
    assert audit.target_id == feedback.id


def test_feedback_submit_cross_workspace_target_is_opaque_not_found_without_mutation(
    session_factory,
):
    service = FeedbackService()

    with session_factory.begin() as session:
        requested_workspace = _workspace(session)
        foreign_workspace = Workspace(
            slug="foreign-feedback-submit",
            title="Foreign Feedback Submit",
            name="Foreign Feedback Submit",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_page = ContextPage(
            workspace_id=foreign_workspace.id,
            slug="foreign-feedback-target",
            title="Foreign Feedback Target",
            page_type="overview",
        )
        session.add(foreign_page)
        session.flush()

        counts_before = (
            session.scalar(select(func.count()).select_from(FeedbackItem)),
            session.scalar(select(func.count()).select_from(AuditEvent)),
            session.scalar(select(func.count()).select_from(Job)),
        )
        missing_id = uuid4()

        with pytest.raises(ReadNotFoundError) as missing_error:
            service.submit_feedback(
                session=session,
                workspace_id=requested_workspace.id,
                request=SubmitFeedbackRequest(
                    target_type="context_page",
                    target_id=missing_id,
                    body="Missing target must remain opaque.",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        with pytest.raises(ReadNotFoundError) as foreign_error:
            service.submit_feedback(
                session=session,
                workspace_id=requested_workspace.id,
                request=SubmitFeedbackRequest(
                    target_type="context_page",
                    target_id=foreign_page.id,
                    body="Foreign target must remain opaque.",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

        counts_after = (
            session.scalar(select(func.count()).select_from(FeedbackItem)),
            session.scalar(select(func.count()).select_from(AuditEvent)),
            session.scalar(select(func.count()).select_from(Job)),
        )

    assert missing_error.value.error_code == "not_found"
    assert foreign_error.value.error_code == "not_found"
    assert str(foreign_error.value) == str(missing_error.value)
    assert counts_after == counts_before


def test_feedback_service_lists_by_status_and_target_type(session_factory):
    service = FeedbackService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        draft = _context_draft(session, workspace.id)
        service.submit_feedback(
            session=session,
            workspace_id=workspace.id,
            request=SubmitFeedbackRequest(
                target_type="context_page",
                target_id=draft.page_id,
                body="First feedback.",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        service.submit_feedback(
            session=session,
            workspace_id=workspace.id,
            request=SubmitFeedbackRequest(
                target_type="answer",
                target_id=draft.draft_version_id,
                body="Second feedback.",
                severity="info",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        listed = service.list_feedback(
            session=session,
            workspace_id=workspace.id,
            actor=_audit_actor(),
            status="open",
            target_type="context_page",
        )
        # A viewer session / actor without audit access sees nothing.
        denied = service.list_feedback(
            session=session,
            workspace_id=workspace.id,
            actor=_viewer_actor(),
            status="open",
            target_type="context_page",
        )

    assert len(listed) == 1
    assert listed[0].body == "First feedback."
    assert denied == []


def test_review_service_records_decision_and_updates_change_set(session_factory):
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        draft = _context_draft(session, workspace.id)
        review = session.get(ReviewItem, draft.review_item_id)
        result = review_service.apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=review.id,
            request=ReviewDecisionRequest(
                decision_type="request_changes",
                decision_reason="Need stronger citation.",
                audience_kind="workspace_editors",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )

        change_set = session.get(ChangeSet, draft.change_set_id)
        decision = session.scalar(select(ReviewDecision))
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "review.decision")
        )

    assert result.status == "changes_requested"
    assert result.decisions[0].decision_id == decision.id
    assert decision.resulting_change_set_status == "needs_changes"
    assert change_set.status == "needs_changes"
    assert audit.target_id == review.id
    # workspace_editors broadcast: audience_kind set, user snapshot stays NULL.
    assert decision.audience_kind == "workspace_editors"
    assert decision.audience_user_id is None
    assert result.current_audience_kind == "workspace_editors"


def test_review_decision_reason_is_required_only_for_request_changes():
    approve = ReviewDecisionRequest(decision_type="approve")
    reject = ReviewDecisionRequest(decision_type="reject")
    request_changes = ReviewDecisionRequest(
        decision_type="request_changes",
        audience_kind="workspace_editors",
        decision_reason="Please revise.",
    )

    assert approve.decision_reason is None
    assert reject.decision_reason is None
    assert request_changes.decision_reason == "Please revise."
    with pytest.raises(ValidationError):
        ReviewDecisionRequest(decision_type="request_changes")


def test_review_context_target_metadata_fails_closed_without_actor(session_factory):
    # LEAK-03: the projector used to treat a MISSING actor as fully
    # authorized ("ordinarily_visible = actor is None or context_page_visible
    # (...)"). It must fail CLOSED instead -- a caller that cannot name its
    # actor gets the redacted form, same as an actor who genuinely cannot see
    # the target. Uses the exact page shape (rejected first draft: no head at
    # all) that motivated the "scoped headless preview" branch, since that
    # branch ALSO depends on the actor -- so the None case below is a real
    # fail-closed default, not just "nobody could see this anyway".
    from backend.models.context import ContextPageVersion
    from backend.services.review_service import _review_context_target_metadata

    with session_factory.begin() as session:
        workspace = _workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="leak03-headless",
            title="Headless Rejected Draft",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=1,
            body_md="# Headless\n\nRejected first draft.",
            content_hash="leak03-headless-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="draft",
        )
        session.add(version)
        session.flush()
        # No current_version_id / latest_draft_version_id /
        # current_published_version_id -- the page has NO readable head, as
        # after a first-draft rejection.
        workspace_id = workspace.id

        no_actor = _review_context_target_metadata(
            page=page, version=version, actor=None, workspace_id=workspace_id
        )
        owner = ActorContext(
            actor_kind="human",
            actor_id="owner-user",
            scopes=(),
            workspace_id=workspace_id,
            workspace_role="owner",
            role="owner",
            token_kind="session",
        )
        with_owner = _review_context_target_metadata(
            page=page, version=version, actor=owner, workspace_id=workspace_id
        )

    assert no_actor["target_title"] is None
    assert no_actor["target_slug"] is None
    assert no_actor["target_version_no"] is None

    # An owner (can read drafts) sees the scoped headless preview.
    assert with_owner["target_title"] == "Headless Rejected Draft"
    assert with_owner["target_slug"] == "leak03-headless"
    assert with_owner["target_version_no"] == 1


def test_review_reject_retry_redacts_sensitive_agent_page_target_for_non_owner(
    session_factory,
):
    # LEAK-03: the review target projector's sensitive-AgentPage redaction is
    # keyed on the actor threaded through apply_decision -> _detail ->
    # _evidence. The idempotent reject-retry branch (apply_decision, when
    # review.status is already "rejected") used to call self._detail(...)
    # WITHOUT an actor at all, and the projector treated a MISSING actor as
    # "ordinarily visible" (fail OPEN) -- so a non-owner editor retrying an
    # already-rejected decision saw a sensitive (memory-category) AgentPage's
    # title/slug it could not read directly.
    from backend.models.agent import AgentPage, AgentPageVersion

    review_service = ReviewService()
    owner_id = uuid4()
    editor_id = uuid4()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        agent = AgentPage(
            workspace_id=workspace.id,
            category="memory",
            title="Sensitive Recall Notes",
            slug="leak03-sensitive-recall",
            sensitivity="private",
            lifecycle_status="active",
        )
        session.add(agent)
        session.flush()
        version = AgentPageVersion(
            workspace_id=agent.workspace_id,
            agent_page_id=agent.id,
            version_no=1,
            body_markdown="# Sensitive",
            frontmatter={},
            actor_type="human",
            version_status="draft",
        )
        session.add(version)
        session.flush()
        agent.latest_draft_version_id = version.id

        change_set = ChangeSet(
            workspace_id=workspace.id,
            reason="activate sensitive recall notes",
            trigger_type="agent_page_activation",
            actor_type="human",
            actor_id=owner_id,
            status="needs_review",
            risk_level="high",
        )
        session.add(change_set)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="agent_page",
                target_id=agent.id,
                operation="activate",
                diff_summary="activate draft",
                diff_ref=f"agent_page_version:{version.id}",
            )
        )
        review = ReviewItem(
            workspace_id=workspace.id,
            change_set_id=change_set.id,
            review_type="agent_page_activation",
            status="pending",
            risk_level="high",
        )
        session.add(review)
        session.flush()
        workspace_id = workspace.id
        review_id = review.id

    owner = ActorContext(
        actor_kind="human",
        actor_id=str(owner_id),
        scopes=(),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
        token_kind="session",
        user_id=owner_id,
    )
    # A non-owner editor cannot read a sensitive (memory-category) AgentPage
    # directly -- actor_can_read_sensitive_agent_pages requires owner role.
    editor = ActorContext(
        actor_kind="human",
        actor_id=str(editor_id),
        scopes=(),
        workspace_id=workspace_id,
        workspace_role="editor",
        role="editor",
        token_kind="session",
        user_id=editor_id,
    )

    with session_factory.begin() as session:
        released = review_service.release_review(
            session=session, workspace_id=workspace_id, review_item_id=review_id,
            actor_kind="human", actor_id=str(owner_id), actor=owner,
            actor_role="owner",
        )
    assert released.evidence.operations[0].target_title == "Sensitive Recall Notes"

    with session_factory.begin() as session:
        first = review_service.apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=review_id,
            request=ReviewDecisionRequest(
                decision_type="reject", decision_reason="not yet"
            ),
            actor_kind="human",
            actor_id=str(owner_id),
            actor_provenance="browser_session",
            actor_user_id=owner_id,
            actor_role="owner",
            actor=owner,
        )
    assert first.status == "rejected"
    assert first.evidence.operations[0].target_title == "Sensitive Recall Notes"
    assert first.evidence.operations[0].target_slug == "leak03-sensitive-recall"
    assert first.evidence.operations[0].document.document_id == agent.id
    assert first.evidence.operations[0].document.reader_href == (
        f"/agent?target={agent.id}"
    )
    assert first.evidence.operations[0].content.mode == "unsupported"
    assert first.evidence.operations[0].content.candidate_version_id == version.id

    # Same reject, same terminal result already recorded -> idempotent retry
    # branch, this time by the non-owner editor.
    with session_factory.begin() as session:
        retried = review_service.apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=review_id,
            request=ReviewDecisionRequest(
                decision_type="reject", decision_reason="not yet"
            ),
            actor_kind="human",
            actor_id=str(editor_id),
            actor_provenance="browser_session",
            actor_user_id=editor_id,
            actor_role="editor",
            actor=editor,
        )
    assert retried.status == "rejected"
    assert retried.evidence.operations[0].target_title is None
    assert retried.evidence.operations[0].target_slug is None
    assert retried.evidence.operations[0].document.document_id is None
    assert retried.evidence.operations[0].document.reader_href is None
    assert retried.evidence.operations[0].content.candidate_version_id is None


def test_review_library_artifact_and_version_resolve_to_visible_owning_document(
    session_factory,
):
    from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion

    service = ReviewService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        item = LibraryItem(
            workspace_id=workspace.id,
            connector_type="upload",
            uri="https://example.com/review-source.pdf",
            title="Reviewed source",
            trust_level="trusted",
        )
        session.add(item)
        session.flush()
        version = LibraryItemVersion(
            workspace_id=workspace.id,
            library_item_id=item.id,
            version_no=4,
            content_hash="reviewed-source",
            raw_storage_ref="inline://source",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        item.current_version_id = item.current_ready_version_id = version.id
        artifact = LibraryArtifact(
            workspace_id=workspace.id,
            library_item_version_id=version.id,
            attempt_id=uuid4(),
            stage="conversion",
            blob_workspace_id=workspace.id,
            blob_id=uuid4(),
            blob_epoch=1,
            role="markdown",
            kind="text",
            media_type="text/plain",
            content_sha256="b" * 64,
            byte_size=12,
        )
        session.add(artifact)
        draft = _context_draft(session, workspace.id)
        for target_type, target_id in [
            ("library_item_version", version.id), ("library_artifact", artifact.id),
        ]:
            session.add(ChangeOperation(
                workspace_id=workspace.id,
                change_set_id=draft.change_set_id,
                target_type=target_type,
                target_id=target_id,
                operation="review_source",
            ))
        session.flush()
        detail = service.get_review(
            session, workspace.id, draft.review_item_id,
            actor_role="owner", actor=_audit_actor(),
        )
        sources = [operation for operation in detail.evidence.operations
                   if operation.target_type.startswith("library_")]
        assert len(sources) == 2
        for operation in sources:
            assert operation.document.document_id == item.id
            assert operation.document.reader_href == f"/library?target={item.id}"
            assert operation.document.can_open is True
            assert operation.content.mode == "unsupported"
            assert operation.content.candidate_version_id == version.id
            assert operation.content.candidate_version_no == 4

        item.trust_level = "restricted"
        editor = ActorContext(
            actor_kind="human",
            actor_id=ACTOR_ID,
            token_kind="session",
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        redacted = service.get_review(
            session, workspace.id, draft.review_item_id,
            actor_role="editor", actor=editor,
        )
        for operation in redacted.evidence.operations:
            if operation.target_type.startswith("library_"):
                assert operation.document.document_id is None
                assert operation.document.reader_href is None
                assert operation.content.candidate_version_id is None


def test_review_decision_audience_kind_optional_for_request_changes():
    # audience_kind is OPTIONAL for request_changes; omitting it defaults to the
    # editor broadcast (NULL audience). This keeps the current single
    # "Request changes" button working before the routing picker ships.
    omitted = ReviewDecisionRequest(
        decision_type="request_changes",
        decision_reason="Please revise.",
    )
    assert omitted.audience_kind is None
    explicit = ReviewDecisionRequest(
        decision_type="request_changes",
        audience_kind="chat_owner",
        decision_reason="Please revise.",
    )
    assert explicit.audience_kind == "chat_owner"
    # ...but approve/reject still reject a stray audience_kind.
    with pytest.raises(ValidationError):
        ReviewDecisionRequest(
            decision_type="approve",
            audience_kind="workspace_editors",
        )


def test_review_service_includes_change_evidence(session_factory):
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        draft = _context_draft(session, workspace.id)
        review = session.get(ReviewItem, draft.review_item_id)
        operation = session.scalar(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id == draft.change_set_id
            )
        )
        result = review_service.get_review(
            session=session,
            workspace_id=workspace.id,
            review_item_id=review.id,
            actor_role="owner",
            # LEAK-03: the target-metadata projector now fails CLOSED without
            # an actor, so a caller that wants title/slug in evidence must
            # supply one.
            actor=_audit_actor(),
        )

    assert result.evidence is not None
    assert result.evidence.change_set_status == "draft_created"
    assert result.evidence.trigger_type == "mcp_write_draft"
    assert result.evidence.reason == "create feedback target"
    assert result.evidence.operations[0].operation_id == operation.id
    assert result.evidence.operations[0].target_id == draft.page_id
    assert result.evidence.operations[0].target_title == "Feedback Target"
    assert result.evidence.operations[0].target_slug == "feedback-target"
    assert result.evidence.operations[0].target_version_no == 1
    assert result.evidence.operations[0].diff_ref == (
        f"context_page_version:{draft.draft_version_id}"
    )


def test_review_service_lists_with_risk_filter_and_cursor(session_factory):
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        first = _context_draft(session, workspace.id)
        second = _context_draft(session, workspace.id)
        first_review = session.get(ReviewItem, first.review_item_id)
        second_review = session.get(ReviewItem, second.review_item_id)
        first_review.risk_level = "high"
        second_review.risk_level = "low"
        session.flush()

        high = review_service.list_reviews(
            session=session,
            workspace_id=workspace.id,
            risk_level="high",
            limit=10,
            actor_role="editor",
        )
        page_one = review_service.list_reviews(
            session=session,
            workspace_id=workspace.id,
            status="pending",
            limit=1,
            actor_role="editor",
        )
        page_two = review_service.list_reviews(
            session=session,
            workspace_id=workspace.id,
            status="pending",
            limit=1,
            cursor=page_one.next_cursor,
            actor_role="editor",
        )

    assert [item.id for item in high.items] == [first_review.id]
    assert page_one.next_cursor is not None
    assert page_one.next_cursor != "1"
    assert page_two.next_cursor is None
    assert page_one.items[0].id != page_two.items[0].id


def test_review_service_accepts_csv_status_and_risk_filters(session_factory):
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        first = _context_draft(session, workspace.id)
        second = _context_draft(session, workspace.id)
        first_review = session.get(ReviewItem, first.review_item_id)
        second_review = session.get(ReviewItem, second.review_item_id)
        second_review.status = "changes_requested"
        first_review.risk_level = "high"
        second_review.risk_level = "medium"
        session.flush()

        listed = review_service.list_reviews(
            session=session,
            workspace_id=workspace.id,
            status="pending,changes_requested",
            risk_level="high,medium",
            limit=10,
            actor_role="editor",
        )

    assert {item.id for item in listed.items} == {first_review.id, second_review.id}


def test_review_service_bulk_review_ids_does_not_cap_filter_matches(
    session_factory,
):
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        for index in range(505):
            change_set = ChangeSet(
                workspace_id=workspace.id,
                trigger_type="bulk_filter_test",
                actor_type="human",
                reason=f"Bulk filter target {index}",
                risk_level="high",
            )
            session.add(change_set)
            session.flush()
            session.add(
                ReviewItem(
                    workspace_id=workspace.id,
                    change_set_id=change_set.id,
                    review_type="bulk_filter_test",
                    status="pending",
                    risk_level="high",
                )
            )
        session.flush()
        created_review_ids = set(
            session.scalars(
                select(ReviewItem.id).where(
                    ReviewItem.review_type == "bulk_filter_test"
                )
            ).all()
        )

        target_ids = review_service.bulk_review_ids(
            session=session,
            workspace_id=workspace.id,
            status="pending",
            risk_level="high",
            scope="all",
            actor_user_id=uuid4(),
            actor_role="editor",
        )

    assert len(created_review_ids) == 505
    assert created_review_ids.issubset(set(target_ids))


def test_review_service_resolved_filter_expands_approved_and_rejected(
    session_factory,
):
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        approved = _review_for_draft(session, workspace.id)
        rejected = _review_for_draft(session, workspace.id)
        superseded = _review_for_draft(session, workspace.id)
        approved.status = "approved"
        rejected.status = "rejected"
        superseded.status = "superseded"
        approved.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        rejected.updated_at = datetime(2026, 1, 2, tzinfo=UTC)
        session.flush()

        listed = review_service.list_reviews(
            session=session,
            workspace_id=workspace.id,
            status="resolved",
            limit=10,
            actor_role="editor",
        )

    assert {item.id for item in listed.items} == {approved.id, rejected.id}
    assert [item.id for item in listed.items] == [rejected.id, approved.id]


def test_review_counts_group_statuses_and_respect_risk_filter(session_factory):
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        pending = _review_for_draft(session, workspace.id)
        changes = _review_for_draft(session, workspace.id)
        approved = _review_for_draft(session, workspace.id)
        rejected = _review_for_draft(session, workspace.id)
        superseded = _review_for_draft(session, workspace.id)
        pending.status = "pending"
        changes.status = "changes_requested"
        approved.status = "approved"
        rejected.status = "rejected"
        superseded.status = "superseded"
        pending.risk_level = "high"
        changes.risk_level = "high"
        approved.risk_level = "high"
        rejected.risk_level = "low"
        superseded.risk_level = "high"
        session.flush()

        high = review_service.count_reviews(
            session=session,
            workspace_id=workspace.id,
            risk_level="high",
            actor_role="owner",
        )

    assert high.pending == 1
    assert high.changes_requested == 1
    assert high.resolved == 1
    assert high.risk_counts.model_dump() == {"high": 3, "medium": 0, "low": 0}


def test_context_update_links_feedback_to_draft_change(session_factory):
    feedback_service = FeedbackService()
    write_service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        draft = _context_draft(session, workspace.id)
        feedback = feedback_service.submit_feedback(
            session=session,
            workspace_id=workspace.id,
            request=SubmitFeedbackRequest(
                target_type="context_page",
                target_id=draft.page_id,
                body="Add detail.",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(FeedbackItem, feedback.feedback_id)
        context_page = session.get(ContextPage, draft.page_id)
        base_version = context_page.latest_draft_version
        update = write_service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=draft.page_id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="append_section",
                body_markdown="## Detail\n\nAdded detail.",
                reason="address feedback",
                feedback_id=feedback.feedback_id,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

    assert page.status == "draft_created"
    assert page.resolved_by_change_set_id == update.change_set_id

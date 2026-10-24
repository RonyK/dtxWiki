from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.lint import LintFinding, LintRun
from backend.models.review import ReviewDecision, ReviewItem
from backend.schemas.context_write import ReviewDecisionRequest, UpdateDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.services.read_errors import (
    ReadNotFoundError,
    ReviewDecisionUndoUnavailableError,
)
from backend.services.review_service import ReviewService
from backend.services.temporal_read import resolve_version_as_of, what_changed
from backend.tests.integration.test_context_review_publish import (
    ACTOR,
    ACTOR_UUID,
    _actor,
    _approve,
    _decision_audit,
    _detail,
    _no_history_violations,
    _published_page_with_draft,
    _reject,
    _seed_review,
    _write,
)


def _undo(session, workspace_id, review_id, decision_id, *, user=ACTOR_UUID):
    return ReviewService().undo_decision(
        session,
        workspace_id,
        review_id,
        decision_id,
        actor_kind="human",
        actor_id=str(user),
        actor_user_id=user,
        actor_role="owner" if user == ACTOR_UUID else "editor",
        actor_provenance="browser_session",
    )


def _revise(session, workspace_id, page_id, base_id, body):
    base = session.get(ContextPageVersion, base_id)
    return ContextWriteService().update_draft(
        session=session,
        workspace_id=workspace_id,
        request=UpdateDraftRequest(
            page_id=page_id,
            base_version_id=base.id,
            base_content_hash=base.content_hash,
            update_mode="full_rewrite",
            body_markdown=body,
            reason=body.splitlines()[0],
        ),
        actor_kind="human",
        actor_id=ACTOR,
    )


def _states(session, page_id):
    page = session.get(ContextPage, page_id)
    versions = session.scalars(
        select(ContextPageVersion)
        .where(ContextPageVersion.page_id == page_id)
        .order_by(ContextPageVersion.version_no)
    ).all()
    return (
        page.current_published_version_id,
        page.latest_draft_version_id,
        page.lifecycle_status,
        page.title,
        [(v.version_status, v.graph_status) for v in versions],
    )


def test_undoing_an_approval_restores_what_it_published_and_settled(session_factory):
    with session_factory.begin() as session:
        workspace, published, draft = _published_page_with_draft(session, "Undo A")
        page_id = published.page_id
        # The published content was flagged stale; publishing the draft clears
        # the flag, and undoing the publication must bring it back.
        stale = _stale_finding(session, workspace.id, page_id)
        before = _states(session, page_id)
        decided = _approve(session, workspace.id, draft.review_item_id)
        assert decided.undo is not None and decided.undo.available
        assert _states(session, page_id)[0] == draft.draft_version_id
        assert stale.status == "fixed"
        while_live = utc_now()

        restored = _undo(
            session, workspace.id, draft.review_item_id, decided.undo.decision_id
        )
        assert _states(session, page_id) == before
        assert (stale.status, stale.resolved_at) == ("open", None)
        assert session.get(ContextPage, page_id).stale_candidate is True
        assert restored.status == "pending" and restored.undo is None
        assert [d.decision_type for d in restored.decisions][:2] == ["undo", "approve"]
        page = session.get(ContextPage, page_id)
        assert resolve_version_as_of(session, page, utc_now()).id == (
            published.draft_version_id
        )
        # What was live while the approval stood stays readable as of then.
        assert resolve_version_as_of(session, page, while_live).id == (
            draft.draft_version_id
        )
        assert _no_history_violations(session, page_id)

        # Approving the same draft again publishes it again, for as-of reads too.
        _approve(session, workspace.id, draft.review_item_id)
        assert (
            resolve_version_as_of(session, page, utc_now()).id
            == draft.draft_version_id
        )
        entries, _ = what_changed(
            session,
            workspace.id,
            since=published_at(session, page_id),
            actor=_actor(workspace.id, "owner"),
        )
        assert [(e.change_kind, e.version_id) for e in entries] == [
            ("updated", draft.draft_version_id),
            ("updated", published.draft_version_id),
            ("updated", draft.draft_version_id),
        ]

        # Undoing a first publication withdraws the page again.
        first = _write(session, workspace.id, "Undo first", "# First\n\nBody.")
        first_before = _states(session, first.page_id)
        approved = _approve(session, workspace.id, first.review_item_id)
        _undo(session, workspace.id, first.review_item_id, approved.undo.decision_id)
        assert _states(session, first.page_id) == first_before
        first_page = session.get(ContextPage, first.page_id)
        assert resolve_version_as_of(session, first_page, utc_now()) is None


def _stale_finding(session, workspace_id, page_id):
    run = LintRun(
        workspace_id=workspace_id,
        lint_scope={"kind": "maintenance"},
        trigger_type="publish_propagation",
        status="completed",
        finding_count_by_severity={},
    )
    session.add(run)
    session.flush()
    finding = LintFinding(
        lint_run_id=run.id,
        workspace_id=workspace_id,
        target_type="context_page",
        target_id=page_id,
        rule_id="kb.dependent_maybe_stale",
        severity="info",
        status="open",
        fingerprint=f"kb.dependent_maybe_stale|{page_id}|seed",
    )
    session.add(finding)
    session.get(ContextPage, page_id).stale_candidate = True
    session.flush()
    return finding


def published_at(session, page_id):
    return session.scalar(
        select(AuditEvent.created_at)
        .where(
            AuditEvent.action == "context_page.draft.publish",
            AuditEvent.target_id == page_id,
        )
        .order_by(AuditEvent.created_at.asc())
        .limit(1)
    )


def test_undoing_a_rejection_restores_the_retired_drafts_and_their_reviews(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, published, draft = _published_page_with_draft(session, "Undo R")
        page_id = published.page_id
        later = _write(session, workspace.id, "Undo R", "# Later\n\nOn the candidate.")
        before = _states(session, page_id)
        decided = _reject(session, workspace.id, draft.review_item_id)
        assert session.get(ReviewItem, later.review_item_id).status == "superseded"

        _undo(session, workspace.id, draft.review_item_id, decided.undo.decision_id)
        assert _states(session, page_id) == before
        assert {
            session.get(ReviewItem, review_id).status
            for review_id in (draft.review_item_id, later.review_item_id)
        } == {"pending"}
        assert _no_history_violations(session, page_id)

        # A rejection that reopened the draft below under a new review: undoing
        # it closes the review it opened.
        workspace, published, draft = _published_page_with_draft(session, "Undo O")
        replaced = _revise(
            session,
            workspace.id,
            published.page_id,
            draft.draft_version_id,
            "# Replacement\n\nWritten over the candidate.",
        )
        before = _states(session, published.page_id)
        rejected = _reject(session, workspace.id, replaced.review_item_id)
        opened = _decision_audit(session, replaced.review_item_id).payload[
            "restored_review_item_id"
        ]
        _undo(
            session, workspace.id, replaced.review_item_id, rejected.undo.decision_id
        )
        assert _states(session, published.page_id) == before
        settled = _detail(session, workspace.id, UUID(opened))
        assert (settled.status, settled.settlement.reason_code) == (
            "superseded",
            "superseded_by_decision_undo",
        )


def test_undo_requires_the_unchanged_result_of_a_recorded_decision(session_factory):
    with session_factory.begin() as session:
        workspace, published, draft = _published_page_with_draft(session, "Undo C")
        decided = _approve(session, workspace.id, draft.review_item_id)
        decision_id = decided.undo.decision_id
        # Only the decider (or an owner) is offered the undo.
        other = uuid4()
        assert (
            ReviewService()
            .get_review(
                session,
                workspace.id,
                draft.review_item_id,
                actor_user_id=other,
                actor_role="editor",
                actor=_actor(workspace.id, "editor"),
            )
            .undo
            is None
        )
        _revise(
            session,
            workspace.id,
            published.page_id,
            draft.draft_version_id,
            "# After approval\n\nNewer work.",
        )
        blocked = _detail(session, workspace.id, draft.review_item_id).undo
        assert (blocked.available, blocked.reason_code, blocked.restore_version_no) == (
            False,
            "later_changes",
            1,
        )
        with pytest.raises(ReviewDecisionUndoUnavailableError):
            _undo(session, workspace.id, draft.review_item_id, decision_id)

        # A decision recorded before undo existed is never offered.
        legacy = _seed_review(
            session,
            workspace.id,
            published.page_id,
            version_id=draft.draft_version_id,
            status="approved",
        )
        session.add(
            ReviewDecision(
                workspace_id=workspace.id,
                review_item_id=legacy.id,
                decision_type="approve",
                actor_type="human",
                actor_id=ACTOR_UUID,
                resulting_review_status="approved",
                resulting_change_set_status="approved",
            )
        )
        session.flush()
        assert _detail(session, workspace.id, legacy.id).undo is None

        # Publishing revives an archived page; undo would not re-archive it.
        _, archived, revived = _published_page_with_draft(session, "Undo archived")
        session.get(ContextPage, archived.page_id).lifecycle_status = "archived"
        assert _approve(session, workspace.id, revived.review_item_id).undo is None


def test_undoing_a_change_request_withdraws_it(session_factory):
    reviews = ReviewService()
    with session_factory.begin() as session:
        workspace, published, draft = _published_page_with_draft(session, "Undo Q")
        requested = reviews.apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=draft.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="request_changes",
                decision_reason="tighten the intro",
                audience_kind="requester",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        _undo(session, workspace.id, draft.review_item_id, requested.undo.decision_id)
        review = session.get(ReviewItem, draft.review_item_id)
        assert review.status == "pending"
        assert (
            reviews.list_requested_changes_for_principal(
                session, workspace.id, principal=ACTOR_UUID, principal_role="owner"
            ).items
            == []
        )
        with pytest.raises(ReadNotFoundError, match="withdrawn"):
            reviews.submit_revision_for_review(
                session,
                workspace.id,
                draft.review_item_id,
                principal=ACTOR_UUID,
                actor_kind="human",
                actor_id=ACTOR,
                principal_role="owner",
                context_write_service=ContextWriteService(),
                update=UpdateDraftRequest(
                    page_id=published.page_id,
                    base_version_id=draft.draft_version_id,
                    base_content_hash=session.get(
                        ContextPageVersion, draft.draft_version_id
                    ).content_hash,
                    update_mode="full_rewrite",
                    body_markdown="# Revised\n\nToo late.",
                    reason="revision",
                ),
            )
        assert session.scalar(
            select(ReviewDecision.decision_type)
            .where(ReviewDecision.review_item_id == draft.review_item_id)
            .order_by(ReviewDecision.created_at.desc())
            .limit(1)
        ) == "undo"

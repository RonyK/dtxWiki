from uuid import uuid4

import pytest
from sqlalchemy import event, select

from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.lint import LintFinding, LintRun
from backend.models.review import ReviewDecision, ReviewItem
from backend.schemas.context_write import ReviewDecisionRequest
from backend.schemas.mcp.read_tools import WhatChangedEntryModel
from backend.services.context_version_storage import verify_context_storage
from backend.services.read_errors import (
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
    _published_page_with_draft,
    _reject,
    _seed_review,
    _start_draft,
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
    return _start_draft(
        session, workspace_id, page_id, base_id, body, reason=body.splitlines()[0]
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
        store = session.info["context_snapshot_store"]
        published_oid = session.get(
            ContextPageVersion, published.draft_version_id
        ).commit_oid
        # The published content was flagged stale; publishing the draft clears
        # the flag, and undoing the publication must bring it back.
        stale = _stale_finding(session, workspace.id, page_id)
        before = _states(session, page_id)
        decided = _approve(session, workspace.id, draft.review_item_id)
        assert (
            _decision_audit(session, draft.review_item_id).payload["effect"]["format"]
            == 2
        )
        candidate = session.get(ContextPageVersion, draft.draft_version_id)
        approved_oid = candidate.commit_oid
        assert approved_oid is not None
        assert decided.undo is not None and decided.undo.available
        assert _states(session, page_id)[0] == draft.draft_version_id
        assert stale.status == "fixed"
        while_live = utc_now()

        restored = _undo(
            session, workspace.id, draft.review_item_id, decided.undo.decision_id
        )
        assert _states(session, page_id)[:4] == before[:4]
        assert candidate.version_status == "closed"
        assert (stale.status, stale.resolved_at) == ("open", None)
        assert session.get(ContextPage, page_id).stale_candidate is True
        assert restored.status == "pending" and restored.undo is None
        assert [d.decision_type for d in restored.decisions][:2] == ["undo", "approve"]
        assert (
            candidate.commit_oid == approved_oid
            and candidate.content_byte_size is not None
        )
        assert store.list_heads(workspace.id)[page_id] == published_oid
        verify_context_storage(session, store)
        page = session.get(ContextPage, page_id)
        assert resolve_version_as_of(session, page, utc_now()).id == (
            published.draft_version_id
        )
        # What was live while the approval stood stays readable as of then.
        assert resolve_version_as_of(session, page, while_live).id == (
            draft.draft_version_id
        )
        verify_context_storage(session, store)

        # Approving the same draft again publishes it again, for as-of reads too.
        _approve(session, workspace.id, draft.review_item_id)
        assert candidate.commit_oid == approved_oid
        assert store.list_heads(workspace.id)[page_id] == approved_oid
        verify_context_storage(session, store)
        assert (
            resolve_version_as_of(session, page, utc_now()).id == draft.draft_version_id
        )
        entries, _ = what_changed(
            session,
            workspace.id,
            since=published_at(session, page_id),
            actor=_actor(workspace.id, "owner"),
        )
        # Each entry names the version published right before it.
        assert [
            (e.change_kind, e.version_id, e.previous_version_id) for e in entries
        ] == [
            ("updated", draft.draft_version_id, published.draft_version_id),
            ("updated", published.draft_version_id, draft.draft_version_id),
            ("updated", draft.draft_version_id, published.draft_version_id),
        ]

        # Undoing a first publication withdraws the page again.
        first = _write(
            session,
            workspace.id,
            "Undo first",
            f"# First\n\nSee @cite{{/p/{workspace.id}/{page_id}}}.",
        )
        first_before = _states(session, first.page_id)

        def record_legacy_effect(_mapper, _connection, audit):
            effect = (audit.payload or {}).get("effect")
            if audit.action == "review.decision" and effect:
                effect["format"] = 1
                for fields in effect["rows"].get("context_page_version", {}).values():
                    fields.pop("commit_oid", None)
                    fields.pop("content_byte_size", None)

        # Older stored decisions lack Git fields even after migration has
        # assigned their published versions an OID. Build that fixture at
        # insertion time; existing audit rows remain append-only.
        event.listen(AuditEvent, "before_insert", record_legacy_effect)
        try:
            approved = _approve(session, workspace.id, first.review_item_id)
        finally:
            event.remove(AuditEvent, "before_insert", record_legacy_effect)
        assert (
            "commit_oid"
            not in _decision_audit(session, first.review_item_id).payload["effect"][
                "rows"
            ]["context_page_version"][str(first.draft_version_id)]
        )
        first_version = session.get(ContextPageVersion, first.draft_version_id)
        first_oid = first_version.commit_oid
        assert (
            session.scalar(
                select(EdgeProjection.id).where(
                    EdgeProjection.source_id == first.page_id,
                    EdgeProjection.edge_kind == "citation_page",
                )
            )
            is not None
        )
        since = utc_now()
        _undo(session, workspace.id, first.review_item_id, approved.undo.decision_id)
        assert not session.scalar(
            select(EdgeProjection.id).where(
                EdgeProjection.source_id == first.page_id,
                EdgeProjection.edge_kind == "citation_page",
            )
        )
        assert _states(session, first.page_id)[:4] == first_before[:4]
        assert first_version.version_status == "closed"
        assert first_version.commit_oid == first_oid
        assert first_version.content_byte_size is not None
        assert first.page_id not in store.list_heads(workspace.id)
        verify_context_storage(session, store)
        first_page = session.get(ContextPage, first.page_id)
        assert resolve_version_as_of(session, first_page, utc_now()) is None
        entries, has_more = what_changed(
            session,
            workspace.id,
            since=since,
            actor=_actor(workspace.id, "editor"),
        )
        assert not has_more and len(entries) == 1
        entry = WhatChangedEntryModel(
            **vars(entries[0]),
            link_markdown=f"[Undo first](/p/{workspace.id}/{first.page_id})",
        )
        assert (entry.change_kind, entry.page_id) == ("unpublished", first.page_id)
        assert (entry.version_id, entry.previous_version_id) == (
            None,
            first.draft_version_id,
        )
        # The new delta must not disclose a draft-only page to ordinary readers.
        assert what_changed(
            session,
            workspace.id,
            since=since,
            actor=_actor(workspace.id, "viewer"),
        ) == ([], False)
        # Publishing it again is a fresh entry into the published set.
        _approve(session, workspace.id, first.review_item_id)
        assert first_version.commit_oid == first_oid
        assert store.list_heads(workspace.id)[first.page_id] == first_oid
        verify_context_storage(session, store)
        entries, _ = what_changed(
            session,
            workspace.id,
            since=since,
            actor=_actor(workspace.id, "editor"),
        )
        assert [
            (e.change_kind, e.version_id, e.previous_version_id) for e in entries
        ] == [
            ("unpublished", None, first.draft_version_id),
            ("created", first.draft_version_id, None),
        ]


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
        reviews = ReviewService()
        identity = {"actor_kind": "human", "actor_id": ACTOR, "actor_role": "owner"}
        reviews.apply_decision(
            session,
            workspace.id,
            later.review_item_id,
            ReviewDecisionRequest(
                decision_type="request_changes",
                decision_reason="Please revise",
                audience_kind="workspace_editors",
            ),
            actor_user_id=ACTOR_UUID,
            actor_provenance="browser_session",
            **identity,
        )
        reviews.claim_review(session, workspace.id, later.review_item_id, **identity)
        later_review = session.get(ReviewItem, later.review_item_id)
        claim = (later_review.claimed_by, later_review.claimed_at)
        before = _states(session, page_id)
        decided = _reject(session, workspace.id, draft.review_item_id)
        assert later_review.status == "changes_requested"
        rejected_version = session.get(ContextPageVersion, draft.draft_version_id)
        rejected_oid = rejected_version.commit_oid
        assert rejected_version.version_status == "closed" and rejected_oid

        _undo(session, workspace.id, draft.review_item_id, decided.undo.decision_id)
        assert _states(session, page_id)[:4] == before[:4]
        assert rejected_version.version_status == "closed"
        assert rejected_version.commit_oid == rejected_oid
        session.refresh(later_review)
        assert session.get(ReviewItem, draft.review_item_id).status == "pending"
        assert later_review.status == "changes_requested"
        assert (later_review.claimed_by, later_review.claimed_at) == claim


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

        # A decision recorded before undo existed explains why undo is
        # unavailable, without a substitute recovery path.
        legacy = _seed_review(
            session,
            workspace.id,
            published.page_id,
            version_id=draft.draft_version_id,
            status="rejected",
        )
        session.add(
            ReviewDecision(
                workspace_id=workspace.id,
                review_item_id=legacy.id,
                decision_type="reject",
                actor_type="human",
                actor_id=ACTOR_UUID,
                resulting_review_status="rejected",
                resulting_change_set_status="rejected",
            )
        )
        session.flush()
        unavailable = _detail(session, workspace.id, legacy.id).undo
        assert (
            unavailable.available,
            unavailable.reason_code,
            unavailable.restore_version_id,
        ) == (False, "not_recorded", None)

        # Publishing revives an archived page; undo would not re-archive it.
        _, archived, revived = _published_page_with_draft(session, "Undo archived")
        session.get(ContextPage, archived.page_id).lifecycle_status = "archived"
        unavailable = _approve(session, workspace.id, revived.review_item_id).undo
        assert (unavailable.available, unavailable.reason_code) == (
            False,
            "unsupported_effect",
        )


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
        reviews.claim_review(
            session,
            workspace.id,
            draft.review_item_id,
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        _undo(session, workspace.id, draft.review_item_id, requested.undo.decision_id)
        review = session.get(ReviewItem, draft.review_item_id)
        assert review.status == "pending"
        assert review.claimed_by is review.claimed_at is None
        assert (
            reviews.list_requested_changes_for_principal(
                session, workspace.id, principal=ACTOR_UUID, principal_role="owner"
            ).items
            == []
        )
        assert (
            session.scalar(
                select(ReviewDecision.decision_type)
                .where(ReviewDecision.review_item_id == draft.review_item_id)
                .order_by(ReviewDecision.created_at.desc())
                .limit(1)
            )
            == "undo"
        )

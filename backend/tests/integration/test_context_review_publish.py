from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.context_write import (
    ReviewDecisionRequest,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.services.context_lifecycle import (
    METADATA_SNAPSHOT_OPERATION,
    audit_context_history,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.lint.autofix import safe_normalize_markdown
from backend.services.page_service import (
    PageNotFoundError,
    PageService,
    pending_page_metadata_patch,
    stage_page_metadata_patch,
)
from backend.services.read_errors import (
    ReadValidationError,
    ReviewDecisionImpactChangedError,
    ReviewRejectBlockedError,
    StaleBaseVersionError,
)
from backend.services.review_risk_policy import default_review_policy
from backend.services.review_service import ReviewService

ACTOR = "00000000-0000-0000-0000-000000000010"
ACTOR_UUID = UUID(ACTOR)


def _workspace(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


def _seed_review(
    session,
    workspace_id,
    page_id,
    review_type="context_update",
    *,
    version_id=None,
    status="pending",
    claimed=False,
):
    change_set = ChangeSet(
        workspace_id=workspace_id,
        reason="seed older review",
        trigger_type="test",
        actor_type="human",
        actor_id=ACTOR_UUID,
        status="needs_changes" if status == "changes_requested" else "draft_created",
        risk_level="low",
    )
    session.add(change_set)
    session.flush()
    session.add(
        ChangeOperation(
            workspace_id=change_set.workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page_id,
            operation="test",
            diff_ref=(f"context_page_version:{version_id}" if version_id else None),
        )
    )
    review = ReviewItem(
        workspace_id=workspace_id,
        change_set_id=change_set.id,
        review_type=review_type,
        status=status,
        risk_level="low",
        decision_summary="seed",
        claimed_by=ACTOR_UUID if claimed else None,
        claimed_at=utc_now() if claimed else None,
    )
    session.add(review)
    session.flush()
    return review


def _approve(session, workspace_id, review_id, *, actor_kind="human"):
    return ReviewService().apply_decision(
        session=session,
        workspace_id=workspace_id,
        review_item_id=review_id,
        request=ReviewDecisionRequest(decision_type="approve"),
        actor_kind=actor_kind,
        actor_id=ACTOR if actor_kind == "human" else str(ACTOR_UUID),
        actor_user_id=ACTOR_UUID if actor_kind == "human" else None,
        actor_role="owner",
        actor_provenance="browser_session",
    )


def _reject(session, workspace_id, review_id, *, fingerprint=None):
    return ReviewService().apply_decision(
        session=session,
        workspace_id=workspace_id,
        review_item_id=review_id,
        request=ReviewDecisionRequest(
            decision_type="reject", expected_impact_fingerprint=fingerprint
        ),
        actor_kind="human",
        actor_id=ACTOR,
        actor_user_id=ACTOR_UUID,
        actor_role="owner",
        actor_provenance="browser_session",
    )


def _actor(workspace_id, role):
    return ActorContext(
        actor_kind="human",
        actor_id=f"{role}-reader",
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role=role,
        role=role,
        provenance="browser_session",
    )


def _detail(session, workspace_id, review_id):
    return ReviewService().get_review(
        session,
        workspace_id,
        review_id,
        actor_user_id=ACTOR_UUID,
        actor_role="owner",
        actor=_actor(workspace_id, "owner"),
    )


def _write(session, workspace_id, title, body, page_type="overview"):
    return ContextWriteService().write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title=title,
            page_type=page_type,
            body_markdown=body,
            citations=[],
            reason=body.splitlines()[0],
        ),
        actor_kind="human",
        actor_id=ACTOR,
        actor_role="owner",
    )


def _no_history_violations(session, page_id):
    return not [
        violation
        for violation in audit_context_history(session).violations
        if violation.page_id == page_id
    ]


def _decision_audit(session, review_id):
    return session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.action == "review.decision",
            AuditEvent.target_kind == "review_item",
            AuditEvent.target_id == review_id,
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    )


def _published_page_with_draft(session, title):
    write = ContextWriteService()
    workspace = _workspace(session)
    published = write.write_draft(
        session=session,
        workspace_id=workspace.id,
        request=WriteDraftRequest(
            title=title,
            page_type="overview",
            body_markdown="# Published\n\nStable body.",
            citations=[],
            reason="published base",
        ),
        actor_kind="human",
        actor_id=ACTOR,
        actor_role="owner",
    )
    _approve(session, workspace.id, published.review_item_id)
    base = session.get(ContextPageVersion, published.draft_version_id)
    draft = write.update_draft(
        session=session,
        workspace_id=workspace.id,
        request=UpdateDraftRequest(
            page_id=published.page_id,
            base_version_id=base.id,
            base_content_hash=base.content_hash,
            update_mode="full_rewrite",
            body_markdown="# Rejected draft\n\nCandidate body.",
            reason="candidate",
        ),
        actor_kind="human",
        actor_id=ACTOR,
    )
    return workspace, published, draft


@pytest.mark.parametrize("restart_actor_kind", ["human", "agent"])
def test_rejected_draft_identical_resubmission_forks_actionable_version(
    session_factory, restart_actor_kind
):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Rejected resubmission",
                page_type="overview",
                body_markdown="# Same\n\nBody.",
                citations=[],
                reason="initial",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        rejected = _reject(session, workspace.id, created.review_item_id)
        assert rejected.status == "rejected"
        base = session.get(ContextPageVersion, created.draft_version_id)
        assert session.get(ContextPage, created.page_id).current_version_id is None
        workspace.auto_fix_enabled = True

        resubmitted = write.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=base.id,
                base_content_hash=base.content_hash,
                update_mode="full_rewrite",
                body_markdown=base.body_md,
                reason="resubmit unchanged body",
            ),
            actor_kind=restart_actor_kind,
            actor_id=ACTOR,
        )

        assert resubmitted.draft_version_id != created.draft_version_id
        assert session.get(ContextPage, created.page_id).latest_draft_version_id == (
            resubmitted.draft_version_id
        )
        new_review = session.get(ReviewItem, resubmitted.review_item_id)
        assert new_review.status == "pending"
        restarted_version = session.get(
            ContextPageVersion, resubmitted.draft_version_id
        )
        assert resubmitted.persisted_content_hash == restarted_version.content_hash
        assert resubmitted.persisted_character_count == len(restarted_version.body_md)
        assert resubmitted.outcome == "updated_draft"
        assert resubmitted.body_changed is False
        assert resubmitted.metadata_changed is False
        assert resubmitted.version_status == "draft"
        assert resubmitted.latest_draft_version_id == restarted_version.id
        assert resubmitted.current_published_version_id is None
        assert resubmitted.review_status == "pending"
        # The rejected draft retired; the restart starts on the head it left.
        assert base.graph_status == "discarded"
        assert restarted_version.parent_version_id is None
        approved = _approve(session, workspace.id, new_review.id)
        assert approved.published_version_id == resubmitted.draft_version_id
        new_operation = session.scalar(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id == resubmitted.change_set_id,
                ChangeOperation.target_type == "context_page",
            )
        )
        assert new_operation.diff_ref == (
            f"context_page_version:{resubmitted.draft_version_id}"
        )
        assert session.get(ReviewItem, created.review_item_id).status == "rejected"
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReviewDecision)
                .where(ReviewDecision.review_item_id == created.review_item_id)
            )
            == 1
        )

        # A later draft retired by its ancestor's rejection has a superseded
        # review, rather than a rejected one. Identical normalized bytes must
        # still restart it and create a new pending review.
        first, later = (
            _write(
                session,
                workspace.id,
                "Cascaded identical resubmission",
                f"# {name}\n\nBody.",
            )
            for name in ("First", "Later")
        )
        _reject(session, workspace.id, first.review_item_id)
        assert session.get(ReviewItem, later.review_item_id).status == "superseded"
        retired = session.get(ContextPageVersion, later.draft_version_id)
        unchanged = write.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=first.page_id,
                base_version_id=retired.id,
                base_content_hash=retired.content_hash,
                update_mode="full_rewrite",
                body_markdown=retired.body_md,
                reason="restart identical cascaded draft",
            ),
            actor_kind=restart_actor_kind,
            actor_id=ACTOR,
        )
        successor = session.get(ContextPageVersion, unchanged.draft_version_id)
        assert successor.id != retired.id
        assert successor.parent_version_id is None
        assert unchanged.outcome == "updated_draft"
        assert session.get(ReviewItem, unchanged.review_item_id).status == "pending"
        assert (
            _approve(
                session, workspace.id, unchanged.review_item_id
            ).published_version_id
            == successor.id
        )


def test_reject_latest_draft_restores_published_head_for_both_reader_classes(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, published, draft = _published_page_with_draft(
            session, "Restore published on reject"
        )
        before = ReviewService().get_review(
            session,
            workspace.id,
            draft.review_item_id,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor=_actor(workspace.id, "owner"),
        )
        candidate = before.evidence.operations[0].content
        assert candidate.candidate_version_id == draft.draft_version_id
        assert candidate.baseline_version_id == published.draft_version_id
        assert candidate.baseline_kind == "published"
        assert candidate.can_diff is True
        assert before.reject_effect.effect == "restore_published"
        assert before.reject_effect.published_version_id == published.draft_version_id
        _reject(session, workspace.id, draft.review_item_id)

        page = session.get(ContextPage, published.page_id)
        assert page.latest_draft_version_id is None
        assert page.current_version_id == published.draft_version_id
        assert page.current_published_version_id == published.draft_version_id
        assert page.lifecycle_status == "published"
        assert (
            _decision_audit(session, draft.review_item_id).payload["reject_effect"]
            == "restore_published"
        )
        page_id = page.id
        workspace_id = workspace.id

    pages = PageService(session_factory)
    assert (
        pages.read_page(page_id=page_id, actor=_actor(workspace_id, "editor")).body_md
        == "# Published\n\nStable body."
    )
    assert (
        pages.read_page(page_id=page_id, actor=_actor(workspace_id, "viewer")).body_md
        == "# Published\n\nStable body."
    )


def test_reject_first_draft_hides_page_but_preserves_draft_evidence(
    session_factory,
):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Hide first rejected draft",
                page_type="overview",
                body_markdown="# Rejected evidence\n\nKeep this row.",
                citations=[],
                reason="first draft",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        _reject(session, workspace.id, created.review_item_id)

        page = session.get(ContextPage, created.page_id)
        version = session.get(ContextPageVersion, created.draft_version_id)
        assert page.latest_draft_version_id is None
        assert page.current_version_id is None
        assert page.current_published_version_id is None
        assert page.lifecycle_status == "draft"
        assert version is not None
        assert version.version_status == "draft"
        assert (
            _decision_audit(session, created.review_item_id).payload["reject_effect"]
            == "hide_unpublished"
        )
        detail = ReviewService().get_review(
            session,
            workspace.id,
            created.review_item_id,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor=_actor(workspace.id, "owner"),
        )
        operation = detail.evidence.operations[0]
        assert operation.document.document_id == page.id
        assert operation.document.can_open is False
        assert operation.document.reader_href is None
        assert operation.content.candidate_version_id == version.id
        assert operation.content.can_read is True
        assert detail.actions.approve.allowed is False
        page_id = page.id
        version_id = version.id
        workspace_id = workspace.id

    pages = PageService(session_factory)
    for role in ("editor", "viewer"):
        actor = _actor(workspace_id, role)
        with pytest.raises(PageNotFoundError):
            pages.read_page(page_id=page_id, actor=actor)
        visible_page_ids = {
            row.id for row in pages.list_pages("default", actor=actor).pages
        }
        assert page_id not in visible_page_ids

    history = pages.list_versions(page_id, actor=_actor(workspace_id, "editor"))
    rejected_entry = next(entry for entry in history if entry.version_id == version_id)
    assert rejected_entry.version_status == "draft"
    assert rejected_entry.review_disposition == "rejected"
    assert (
        pages.read_page(
            page_id=page_id,
            version_id=version_id,
            actor=_actor(workspace_id, "editor"),
        ).version_id
        == version_id
    )
    # From history, the rejected bytes start a new draft on the emptied page.
    restored = pages.restore_version(
        page_id=page_id,
        version_id=version_id,
        actor_kind="human",
        actor_id=ACTOR,
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        new_version = session.get(ContextPageVersion, restored.version_id)
        assert new_version.parent_version_id is None
        assert session.get(ContextPage, page_id).latest_draft_version_id == (
            new_version.id
        )


def test_reject_off_chain_draft_closes_review_without_moving_head(session_factory):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        first = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Reject stale candidate",
                page_type="overview",
                body_markdown="# V1\n\nOld.",
                citations=[],
                reason="v1",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        base = session.get(ContextPageVersion, first.draft_version_id)
        latest = write.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=first.page_id,
                base_version_id=base.id,
                base_content_hash=base.content_hash,
                update_mode="full_rewrite",
                body_markdown="# V2\n\nLatest.",
                reason="v2",
            ),
            actor_kind="human",
            actor_id=ACTOR,
        )
        # Publishing v2 takes v1 off the chain; its late reviews are stale.
        _approve(session, workspace.id, latest.review_item_id)
        stale_review, claimed, requested = (
            _seed_review(
                session,
                workspace.id,
                first.page_id,
                version_id=first.draft_version_id,
                **options,
            )
            for options in ({}, {"claimed": True}, {"status": "changes_requested"})
        )
        page = session.get(ContextPage, first.page_id)
        before = (
            page.latest_draft_version_id,
            page.current_version_id,
            page.current_published_version_id,
        )

        _reject(session, workspace.id, stale_review.id)

        assert stale_review.status == "rejected"
        assert (
            page.latest_draft_version_id,
            page.current_version_id,
            page.current_published_version_id,
        ) == before
        assert page.current_published_version_id == latest.draft_version_id
        assert (
            _decision_audit(session, stale_review.id).payload["reject_effect"]
            == "no_head_change"
        )
        # Nothing retires, so a claimed or changes-requested review stays open.
        assert (claimed.status, requested.status) == ("pending", "changes_requested")


def test_rejecting_a_draft_rejects_the_drafts_written_on_it(session_factory):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        title = "Cascading rejection"
        published = _write(session, workspace.id, title, "# P\n\nLive.")
        _approve(session, workspace.id, published.review_item_id)
        page = session.get(ContextPage, published.page_id)
        d1 = _write(session, workspace.id, title, "# D1\n\nOne.")
        v1 = session.get(ContextPageVersion, d1.draft_version_id)
        # Restoring these exact bytes must preserve their own risk evidence,
        # including mandatory flags from lower-risk sibling reviews.
        original = session.get(ReviewItem, d1.review_item_id)
        original.risk_level = "low"
        original.risk_signals = {
            **original.risk_signals,
            "human_review_required": True,
            "caller_needs_review": True,
            "lint_error": False,
        }
        highest = _seed_review(session, workspace.id, page.id, version_id=v1.id)
        highest.risk_level = "high"
        highest.risk_policy_version = original.risk_policy_version
        highest.risk_resolution_summary = "Original high-risk source"
        highest.risk_policy_snapshot = deepcopy(original.risk_policy_snapshot)
        highest.risk_signals = {
            "human_review_required": False,
            "caller_needs_review": False,
            "lint_error": False,
            "reason_codes": ["original_high_risk"],
        }
        lint_source = _seed_review(session, workspace.id, page.id, version_id=v1.id)
        lint_source.risk_level = "medium"
        lint_source.risk_policy_version = original.risk_policy_version
        lint_source.risk_resolution_summary = "Original mandatory lint review"
        lint_source.risk_policy_snapshot = deepcopy(original.risk_policy_snapshot)
        lint_source.risk_signals = {"human_review_required": False, "lint_error": True}
        # Writing d2 over d1 replaces it: d1's own review closes.
        d2 = write.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=v1.id,
                base_content_hash=v1.content_hash,
                update_mode="full_rewrite",
                body_markdown="# D2\n\nTwo.",
                reason="d2",
            ),
            actor_kind="human",
            actor_id=ACTOR,
        )
        d3, d4, d5 = (
            _write(session, workspace.id, title, f"# D{number}\n\nMore.")
            for number in (3, 4, 5)
        )
        v3 = session.get(ContextPageVersion, d3.draft_version_id)
        fork = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            parent_version_id=v3.id,
            version_no=99,
            body_md="# Fork\n\nAlternative.",
            content_hash="9" * 64,
            graph_status="branched",
            actor_type="human",
            actor_id=ACTOR_UUID,
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR,
        )
        session.add(fork)
        session.flush()
        forked = _seed_review(session, workspace.id, page.id, version_id=fork.id)
        spanning = _seed_review(
            session, workspace.id, page.id, version_id=d4.draft_version_id
        )

        def also_review(change_set_id, version_id, **fields):
            session.add(
                ChangeOperation(
                    workspace_id=workspace.id,
                    change_set_id=change_set_id,
                    target_type="context_page",
                    target_id=page.id,
                    operation="test",
                    diff_ref=f"context_page_version:{version_id}",
                    **fields,
                )
            )

        # One review of d3 and d4 together closes with them.
        also_review(spanning.change_set_id, v3.id)
        session.flush()

        # A private edit on top blocks retiring the drafts below it.
        editing = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            parent_version_id=d5.draft_version_id,
            version_no=100,
            body_md="# Editing\n\nPrivate.",
            content_hash="a" * 64,
            graph_status="linear",
            actor_type="human",
            actor_id=ACTOR_UUID,
            version_status="editing",
            created_by_actor_kind="human",
            created_by_actor_id="someone-else",
        )
        session.add(editing)
        session.flush()
        page.latest_draft_version_id = editing.id
        effect = _detail(session, workspace.id, d3.review_item_id).reject_effect
        assert effect.blocked_reason == "private_edit_in_progress"
        with pytest.raises(ReviewRejectBlockedError):
            _reject(session, workspace.id, d3.review_item_id)
        editing.version_status = editing.graph_status = "discarded"
        page.latest_draft_version_id = d5.draft_version_id
        session.flush()

        # A confirmation binds the impact it was shown.
        shown = _detail(session, workspace.id, d3.review_item_id).reject_effect
        assert [item.version_no for item in shown.cascaded_versions] == [
            session.get(ContextPageVersion, draft.draft_version_id).version_no
            for draft in (d4, d5)
        ]
        d6 = _write(session, workspace.id, title, "# D6\n\nNewest.")
        with pytest.raises(ReviewDecisionImpactChangedError):
            _reject(
                session,
                workspace.id,
                d3.review_item_id,
                fingerprint=shown.impact_fingerprint,
            )

        # Rejecting the latest draft rejects only it.
        _reject(session, workspace.id, d6.review_item_id)
        assert page.latest_draft_version_id == d5.draft_version_id
        assert session.get(ReviewItem, d5.review_item_id).status == "pending"

        # Rejecting d3 also rejects d4 and d5, which were written on it.
        shown = _detail(session, workspace.id, d3.review_item_id).reject_effect
        assert (shown.effect, shown.resulting_draft_version_id) == (
            "restore_previous_draft",
            d2.draft_version_id,
        )
        assert shown.cascaded_review_count == 3
        _reject(
            session,
            workspace.id,
            d3.review_item_id,
            fingerprint=shown.impact_fingerprint,
        )
        assert page.latest_draft_version_id == d2.draft_version_id
        assert {
            session.get(ContextPageVersion, draft.draft_version_id).graph_status
            for draft in (d3, d4, d5)
        } == {"discarded"}
        settled = _detail(session, workspace.id, d5.review_item_id)
        assert settled.status == "superseded"
        assert (
            settled.settlement.reason_code,
            settled.settlement.by_review_item_id,
            settled.settlement.by_version_no,
        ) == ("superseded_by_rejected_ancestor", d3.review_item_id, v3.version_no)
        assert session.get(ReviewItem, d2.review_item_id).status == "pending"
        assert spanning.status == "superseded"
        assert (forked.status, fork.graph_status) == ("pending", "branched")
        assert _no_history_violations(session, page.id)

        # d1's review closed when d2 replaced it, so restoring d1 reopens one.
        shown = _detail(session, workspace.id, d2.review_item_id).reject_effect
        assert shown.restores_review is True
        session.get(ReviewItem, d2.review_item_id).risk_level = "low"
        _reject(session, workspace.id, d2.review_item_id)
        assert page.latest_draft_version_id == d1.draft_version_id
        restored = session.scalar(
            select(ReviewItem).where(
                ReviewItem.decision_summary_code
                == "context_draft_restored_after_rejection"
            )
        )
        assert restored.status == "pending"
        assert restored.risk_level == "high"
        assert session.get(ChangeSet, restored.change_set_id).risk_level == "high"
        assert restored.risk_policy_version == highest.risk_policy_version
        assert restored.risk_resolution_summary == highest.risk_resolution_summary
        assert restored.risk_policy_snapshot == highest.risk_policy_snapshot
        assert all(
            restored.risk_signals[flag]
            for flag in ("human_review_required", "caller_needs_review", "lint_error")
        )
        policy = default_review_policy()
        for entry in policy["automation"]["auto_approve_by_risk"].values():
            entry.update({"allowed": True, "delay_days": 0})
        workspace.review_policy = policy
        # This independent alternative stays pending during the sweep too.
        forked.risk_signals = {"human_review_required": True}
        session.flush()
        assert restored.id not in ReviewService().sweep_auto_approve_reviews(
            session, workspace.id, now=utc_now() + timedelta(days=1)
        )
        assert restored.status == "pending"
        assert _approve(session, workspace.id, restored.id).published_version_id == (
            d1.draft_version_id
        )
        assert page.latest_draft_version_id is None
        assert _no_history_violations(session, page.id)

        # Rejecting a review of two drafts rejects from the earlier one, as
        # approving it would publish the later one: no reviewed draft survives.
        e1, e2 = (
            _write(session, workspace.id, title, f"# E{number}\n\nAgain.")
            for number in (1, 2)
        )
        # Recorded first, so the review's own candidate stays e2.
        also_review(
            e2.change_set_id,
            e1.draft_version_id,
            created_at=utc_now() - timedelta(minutes=5),
        )
        session.flush()
        _reject(session, workspace.id, e2.review_item_id)
        assert page.latest_draft_version_id is None
        assert page.current_published_version_id == d1.draft_version_id
        assert session.get(ReviewItem, e1.review_item_id).status == "superseded"

        # Legacy source rows cannot prove automation is safe, whether there
        # is no review record or its risk JSON/snapshot is incomplete.
        for missing in (True, False):
            first, later = (
                _write(
                    session,
                    workspace.id,
                    f"Unknown risk {missing}",
                    f"# {name}\n\nBody.",
                )
                for name in ("Original", "Later")
            )
            source = session.get(ReviewItem, first.review_item_id)
            if missing:
                session.delete(source)
            else:
                source.risk_level = "medium"
                source.status = "superseded"
                source.risk_signals = ["invalid legacy signals"]
                source.risk_policy_snapshot = ["invalid legacy policy"]
                # Legacy publication recognizes the version's own change set
                # even if its exact-version operation was not retained.
                session.execute(
                    delete(ChangeOperation).where(
                        ChangeOperation.change_set_id == source.change_set_id,
                        ChangeOperation.target_type == "context_page",
                    )
                )
            session.flush()
            _reject(session, workspace.id, later.review_item_id)
            reopened = session.scalar(
                select(ReviewItem)
                .join(
                    ChangeOperation,
                    ChangeOperation.change_set_id == ReviewItem.change_set_id,
                )
                .where(
                    ChangeOperation.diff_ref
                    == f"context_page_version:{first.draft_version_id}",
                    ReviewItem.status == "pending",
                )
            )
            assert reopened.risk_level == ("high" if missing else "medium")
            assert reopened.risk_signals["human_review_required"] is True
            assert reopened.risk_policy_snapshot is None
            assert reopened.id not in ReviewService().sweep_auto_approve_reviews(
                session, workspace.id, now=utc_now() + timedelta(days=1)
            )


def test_unpublished_drafts_publish_and_roll_back_with_their_own_metadata(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = _workspace(session)

        def chain(title, page_types):
            return [
                _write(session, workspace.id, title, f"# {kind}\n\nBody.", kind)
                for kind in page_types
            ]

        # Approving drafts oldest first publishes each one's own metadata,
        # including an edit staged on the draft after it was written.
        first, second, third = chain(
            "Metadata approval", ("overview", "guide", "overview")
        )
        page = session.get(ContextPage, first.page_id)
        stage_page_metadata_patch(
            session,
            change_set_id=first.change_set_id,
            page=page,
            version=session.get(ContextPageVersion, first.draft_version_id),
            patch={"title": "Metadata approval renamed"},
        )
        for draft, page_type in (
            (first, "overview"),
            (second, "guide"),
            (third, "overview"),
        ):
            _approve(session, workspace.id, draft.review_item_id)
            assert (page.current_published_version_id, page.page_type) == (
                draft.draft_version_id,
                page_type,
            )
            if draft is first:
                assert page.title == "Metadata approval renamed"

        # Rejecting the newer draft restores the earlier draft's metadata.
        first, second = chain("Metadata rollback", ("overview", "guide"))
        assert (
            _detail(
                session, workspace.id, second.review_item_id
            ).reject_effect.metadata_kept
            is False
        )
        _reject(session, workspace.id, second.review_item_id)
        page = session.get(ContextPage, first.page_id)
        assert (page.latest_draft_version_id, page.page_type) == (
            first.draft_version_id,
            "overview",
        )

        # A draft written before metadata was recorded neither publishes early
        # nor restores its metadata: the current values stay.
        first, second = chain("Legacy metadata", ("overview", "guide"))
        session.execute(
            delete(ChangeOperation).where(
                ChangeOperation.operation == METADATA_SNAPSHOT_OPERATION,
                ChangeOperation.diff_ref
                == f"context_page_version:{first.draft_version_id}",
            )
        )
        assert (
            _detail(
                session, workspace.id, first.review_item_id
            ).actions.approve.reason_code
            == "stale_candidate"
        )
        assert (
            _detail(
                session, workspace.id, second.review_item_id
            ).reject_effect.metadata_kept
            is True
        )
        _reject(session, workspace.id, second.review_item_id)
        assert session.get(ContextPage, first.page_id).page_type == "guide"

    # A page-first submission records its metadata too, so it can publish
    # before a newer context write.
    pages = PageService(session_factory)
    created = pages.create_page_first(
        workspace_slug="default",
        actor_kind="human",
        actor_id=ACTOR,
        actor_provenance="browser_session",
        title="Page-first metadata",
        body_md="# A\n\nFirst.",
        page_type="overview",
    )
    with session_factory() as session:
        workspace_id = _workspace(session).id
    submitted = pages.submit_context_edit_for_review(
        page_id=created.id,
        version_id=created.version_id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id=ACTOR,
        actor_provenance="browser_session",
        expected_editing_revision=created.editing_revision,
        idempotency_key="page-first-metadata-submit",
    )
    with session_factory.begin() as session:
        _write(session, workspace_id, "Page-first metadata", "# B\n\nNext.", "guide")
        assert session.get(ContextPageVersion, created.version_id).change_set_id is None
        approved = _approve(session, workspace_id, submitted.review_item_id)
        assert approved.published_version_id == created.version_id
        assert session.get(ContextPage, created.id).page_type == "overview"

        # A PAT autosave records a full snapshot despite having no metadata
        # delta. Old versions from this writer may have no change_set_id.
        first, second = (
            _write(
                session,
                workspace_id,
                "PAT metadata conversion",
                f"# {kind}\n\nBody.",
                kind,
            )
            for kind in ("overview", "guide")
        )
        base_hash = session.get(
            ContextPageVersion, second.draft_version_id
        ).content_hash
    saved = pages.autosave_page(
        page_id=first.page_id,
        workspace_id=workspace_id,
        title=None,
        body_md="# PAT successor\n\nBody only.",
        actor_kind="human",
        actor_id=ACTOR,
        base_version_id=second.draft_version_id,
        base_content_hash=base_hash,
        actor_provenance="personal_access_token",
        idempotency_key="pat-metadata-conversion",
    )
    with session_factory.begin() as session:
        version = session.get(ContextPageVersion, saved.version_id)
        assert version.change_set_id is None
        assert saved.change_set_id is not None
        assert session.get(ReviewItem, second.review_item_id).status == "superseded"
        assert session.get(ReviewItem, first.review_item_id).status == "pending"
        page = session.get(ContextPage, first.page_id)
        assert (
            pending_page_metadata_patch(session, page=page, version_id=version.id) == {}
        )
        stage_page_metadata_patch(
            session,
            change_set_id=first.change_set_id,
            page=page,
            version=session.get(ContextPageVersion, first.draft_version_id),
            patch={"title": "Earlier draft renamed"},
        )
        _approve(session, workspace_id, first.review_item_id)
        assert (page.title, page.page_type) == ("Earlier draft renamed", "overview")
        carried = pending_page_metadata_patch(session, page=page, version_id=version.id)
        assert (carried["title"], carried["page_type"]) == (
            "PAT metadata conversion",
            "guide",
        )
        assert (
            session.scalar(
                select(ChangeOperation.change_set_id).where(
                    ChangeOperation.operation == "metadata_patch",
                    ChangeOperation.diff_ref == f"context_page_version:{version.id}",
                )
            )
            == saved.change_set_id
        )
        _approve(session, workspace_id, saved.review_item_id)
        assert (page.title, page.page_type) == ("PAT metadata conversion", "guide")


def test_rejected_draft_restarts_on_the_head_the_rejection_left(session_factory):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        title = "Restart after rejection"
        published = _write(session, workspace.id, title, "# P\n\nLive.")
        _approve(session, workspace.id, published.review_item_id)
        d1, d2 = (
            _write(session, workspace.id, title, f"# D{number}\n\nDraft.")
            for number in (1, 2)
        )
        _reject(session, workspace.id, d2.review_item_id)
        rejected = session.get(ContextPageVersion, d2.draft_version_id)

        def restart():
            return write.update_draft(
                session=session,
                workspace_id=workspace.id,
                request=UpdateDraftRequest(
                    page_id=published.page_id,
                    base_version_id=rejected.id,
                    base_content_hash=rejected.content_hash,
                    update_mode="full_rewrite",
                    body_markdown="# D2\n\nDraft, fixed.",
                    reason="fix d2",
                ),
                actor_kind="human",
                actor_id=ACTOR,
            )

        restarted = restart()
        version = session.get(ContextPageVersion, restarted.draft_version_id)
        assert version.parent_version_id == d1.draft_version_id
        page = session.get(ContextPage, published.page_id)
        assert page.latest_draft_version_id == version.id
        # The head moved on, so the rejected bytes no longer restart here.
        with pytest.raises(StaleBaseVersionError):
            restart()

        # A pre-fix restart could attach the live head to rejected bytes that
        # were still linear. Rejecting that head must retire the rejected
        # ancestor too, rather than making it the editable destination.
        rejected.graph_status = "linear"
        version.parent_version_id = rejected.id
        session.flush()
        preview = _detail(session, workspace.id, restarted.review_item_id).reject_effect
        assert preview.resulting_draft_version_id == d1.draft_version_id
        assert preview.cascaded_versions == []
        _reject(session, workspace.id, restarted.review_item_id)
        assert (version.graph_status, rejected.graph_status) == (
            "discarded",
            "discarded",
        )
        assert page.latest_draft_version_id == d1.draft_version_id

        moved = _write(session, workspace.id, title, "# New head\n\nMoved on.")
        for graph_status in ("linear", "discarded"):
            rejected.graph_status = graph_status
            session.flush()
            with pytest.raises(StaleBaseVersionError):
                restart()
            assert page.latest_draft_version_id == moved.draft_version_id

    # Auth channels share autosave, but bearer writes take the reviewed-draft
    # branch. Both an explicit None parent and a surviving head must reach it.
    # Legacy linear rows also exercise the identical-body no-op guard.
    for name, provenance, survives, legacy in (
        ("Browser headless", "browser_session", False, False),
        ("PAT headless", "personal_access_token", False, False),
        ("OAuth surviving", "oauth_client", True, False),
        ("Browser legacy", "browser_session", True, True),
        ("PAT legacy", "personal_access_token", False, True),
    ):
        with session_factory.begin() as session:
            workspace_id = _workspace(session).id
            first = _write(session, workspace_id, name, "# A\n\nDraft.")
            second = (
                first
                if legacy and not survives
                else _write(session, workspace_id, name, "# B\n\nDraft.")
            )
            _reject(
                session,
                workspace_id,
                second.review_item_id if survives or legacy else first.review_item_id,
            )
            source = session.get(ContextPageVersion, second.draft_version_id)
            if legacy:
                source.graph_status = "linear"
            source_body, source_hash = source.body_md, source.content_hash
            expected_parent = first.draft_version_id if survives else None
        resumed = PageService(session_factory).autosave_page(
            page_id=first.page_id,
            workspace_id=workspace_id,
            title=None,
            body_md=source_body,
            actor_kind="human",
            actor_id=ACTOR,
            base_version_id=second.draft_version_id,
            base_content_hash=source_hash,
            actor_provenance=provenance,
            idempotency_key=f"resume-{name}",
            session_version_id=second.draft_version_id,
        )
        with session_factory.begin() as session:
            resumed_version = session.get(ContextPageVersion, resumed.version_id)
            assert resumed_version.id != second.draft_version_id
            assert resumed_version.parent_version_id == expected_parent
            assert session.get(ContextPage, first.page_id).latest_draft_version_id == (
                resumed_version.id
            )
            if legacy:
                assert (
                    session.get(
                        ContextPageVersion, second.draft_version_id
                    ).graph_status
                    == "discarded"
                )
            if provenance != "browser_session":
                reopened = session.get(ReviewItem, resumed.review_item_id)
                assert reopened.status == "pending"
                if legacy and not survives:
                    _reject(session, workspace_id, reopened.id)
                    page = session.get(ContextPage, first.page_id)
                    assert page.latest_draft_version_id is None
                    assert page.current_version_id is None
                    assert resumed_version.graph_status == "discarded"
                    assert _no_history_violations(session, page.id)
                else:
                    assert (
                        _approve(
                            session, workspace_id, reopened.id
                        ).published_version_id
                        == resumed.version_id
                    )


def test_reject_published_snapshot_never_demotes_it(session_factory):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Published snapshot rejection guard",
                page_type="overview",
                body_markdown="# Published\n\nSnapshot.",
                citations=[],
                reason="draft",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        _approve(session, workspace.id, created.review_item_id)
        review = _seed_review(
            session,
            workspace.id,
            created.page_id,
            version_id=created.draft_version_id,
        )
        page = session.get(ContextPage, created.page_id)
        before = (
            page.latest_draft_version_id,
            page.current_version_id,
            page.current_published_version_id,
        )

        _reject(session, workspace.id, review.id)

        assert review.status == "rejected"
        assert (
            page.latest_draft_version_id,
            page.current_version_id,
            page.current_published_version_id,
        ) == before
        assert (
            session.get(ContextPageVersion, created.draft_version_id).version_status
            == "published_snapshot"
        )
        assert (
            _decision_audit(session, review.id).payload["reject_effect"]
            == "no_head_change"
        )


def test_reject_closes_every_open_review_of_the_retired_version_only(
    session_factory,
):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        first = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Exact reject siblings",
                page_type="overview",
                body_markdown="# V1\n\nFirst.",
                citations=[],
                reason="v1",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        base = session.get(ContextPageVersion, first.draft_version_id)
        latest = write.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=first.page_id,
                base_version_id=base.id,
                base_content_hash=base.content_hash,
                update_mode="full_rewrite",
                body_markdown="# V2\n\nSecond.",
                reason="v2",
            ),
            actor_kind="human",
            actor_id=ACTOR,
        )
        target = _seed_review(
            session, workspace.id, first.page_id, version_id=latest.draft_version_id
        )
        same_version = _seed_review(
            session, workspace.id, first.page_id, version_id=latest.draft_version_id
        )
        different_version = _seed_review(
            session, workspace.id, first.page_id, version_id=first.draft_version_id
        )
        changes_requested = _seed_review(
            session,
            workspace.id,
            first.page_id,
            version_id=latest.draft_version_id,
            status="changes_requested",
        )
        claimed_pending = _seed_review(
            session,
            workspace.id,
            first.page_id,
            version_id=latest.draft_version_id,
            claimed=True,
        )

        _reject(session, workspace.id, target.id)

        assert same_version.status == "superseded"
        assert session.get(ChangeSet, same_version.change_set_id).status == "superseded"
        assert changes_requested.status == claimed_pending.status == "superseded"
        # v1 survives as the edit head again, so its review stays open.
        assert different_version.status == "pending"
        page = session.get(ContextPage, first.page_id)
        assert page.latest_draft_version_id == first.draft_version_id


def test_reject_retry_is_idempotent_for_decision_audit_and_pointers(session_factory):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Idempotent rejection",
                page_type="overview",
                body_markdown="# Draft\n\nReject once.",
                citations=[],
                reason="draft",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        first = _reject(session, workspace.id, created.review_item_id)
        page = session.get(ContextPage, created.page_id)
        after_first = (
            page.latest_draft_version_id,
            page.current_version_id,
            page.current_published_version_id,
        )

        second = _reject(session, workspace.id, created.review_item_id)

        assert first.status == second.status == "rejected"
        assert (
            page.latest_draft_version_id,
            page.current_version_id,
            page.current_published_version_id,
        ) == after_first
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReviewDecision)
                .where(ReviewDecision.review_item_id == created.review_item_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "review.decision",
                    AuditEvent.target_kind == "review_item",
                    AuditEvent.target_id == created.review_item_id,
                )
            )
            == 1
        )


def test_preapproval_sibling_count_is_zero_without_siblings(session_factory):
    write = ContextWriteService()
    reviews = ReviewService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="No sibling warning",
                page_type="overview",
                body_markdown="# Only\n\nDraft.",
                citations=[],
                reason="draft",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        before = reviews.get_review(
            session,
            workspace.id,
            created.review_item_id,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
        )
        assert before.superseded_review_count == 0


def test_approving_latest_draft_publishes_and_only_settles_context_reviews(
    session_factory,
):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Publish on approve",
                page_type="overview",
                body_markdown="# Draft\n\nLatest.",
                citations=[],
                reason="draft",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        older = _seed_review(
            session,
            workspace.id,
            created.page_id,
            version_id=created.draft_version_id,
        )
        unrelated_context = _seed_review(
            session,
            workspace.id,
            created.page_id,
        )
        inline = _seed_review(
            session, workspace.id, created.page_id, "page_inline_comment"
        )
        deleted = _seed_review(
            session, workspace.id, created.page_id, "source_deleted_notice"
        )
        before = ReviewService().get_review(
            session,
            workspace.id,
            created.review_item_id,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor=_actor(workspace.id, "owner"),
        )
        detail = _approve(session, workspace.id, created.review_item_id)

        page = session.get(ContextPage, created.page_id)
        assert page.current_published_version_id == created.draft_version_id
        assert page.latest_draft_version_id is None
        assert (
            session.get(ContextPageVersion, created.draft_version_id).version_status
            == "published_snapshot"
        )
        assert older.status == "superseded"
        assert unrelated_context.status == "pending"
        assert inline.status == "pending"
        assert deleted.status == "pending"
        assert detail.published_version_id == created.draft_version_id
        assert before.superseded_review_count == 1
        assert detail.superseded_review_count == before.superseded_review_count
        lineage = session.scalar(
            select(ChangeSet).where(ChangeSet.id == older.change_set_id)
        )
        assert lineage.status == "superseded"


def test_approving_earlier_draft_publishes_up_to_it_not_newer_or_forked(
    session_factory,
):
    write = ContextWriteService()
    reviews = ReviewService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Chain approval",
                page_type="overview",
                body_markdown="# V1\n\nFirst.",
                citations=[],
                reason="v1",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        page_id = created.page_id
        drafts = [session.get(ContextPageVersion, created.draft_version_id)]

        def add_draft(number):
            updated = write.update_draft(
                session=session,
                workspace_id=workspace.id,
                request=UpdateDraftRequest(
                    page_id=page_id,
                    base_version_id=drafts[-1].id,
                    base_content_hash=drafts[-1].content_hash,
                    update_mode="full_rewrite",
                    body_markdown=f"# V{number}\n\nNewer.",
                    reason=f"v{number}",
                ),
                actor_kind="human",
                actor_id=ACTOR,
            )
            drafts.append(session.get(ContextPageVersion, updated.draft_version_id))
            return updated.review_item_id

        def approve_reason(review):
            return reviews.get_review(
                session,
                workspace.id,
                review.id,
                actor_user_id=ACTOR_UUID,
                actor_role="owner",
                actor=_actor(workspace.id, "owner"),
            ).actions.approve.reason_code

        _approve(session, workspace.id, created.review_item_id)
        for number in (2, 3, 4):
            newest = session.get(ReviewItem, add_draft(number))
        published, first, middle, latest = drafts
        fork = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page_id,
            parent_version_id=first.id,
            version_no=5,
            body_md="# Fork\n\nAlternative.",
            content_hash="5" * 64,
            graph_status="branched",
            actor_type="human",
            actor_id=ACTOR_UUID,
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR,
        )
        session.add(fork)
        session.flush()
        older, reviewed, forked, spanning = (
            _seed_review(session, workspace.id, page_id, version_id=version.id)
            for version in (first, middle, fork, latest)
        )
        # One change set reviewing both an earlier and a newer draft.
        session.add(
            ChangeOperation(
                workspace_id=workspace.id,
                change_set_id=spanning.change_set_id,
                target_type="context_page",
                target_id=page_id,
                operation="test",
                diff_ref=f"context_page_version:{first.id}",
            )
        )
        session.flush()
        detail = reviews.get_review(
            session,
            workspace.id,
            reviewed.id,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor=_actor(workspace.id, "owner"),
        )
        assert detail.evidence.operations[0].content.is_current_candidate is True
        assert detail.actions.approve.allowed is True
        assert detail.superseded_review_count == 1
        assert approve_reason(forked) == "stale_candidate"

        approved = _approve(session, workspace.id, reviewed.id)
        page = session.get(ContextPage, page_id)
        assert approved.published_version_id == middle.id
        assert approved.superseded_review_count == 1
        assert (
            page.current_published_version_id,
            page.current_version_id,
            page.latest_draft_version_id,
        ) == (middle.id, middle.id, latest.id)
        assert published.version_status == "superseded_snapshot"
        assert first.graph_status == "discarded"
        assert older.status == "superseded"
        assert newest.status == forked.status == spanning.status == "pending"
        assert not [
            violation
            for violation in audit_context_history(session).violations
            if violation.page_id == page.id
        ]
        # The newer draft builds on the version just published; everything
        # the spanning review covers is now in the approved history.
        _approve(session, workspace.id, newest.id)
        assert page.current_published_version_id == latest.id
        assert page.latest_draft_version_id is None
        assert (forked.status, spanning.status) == ("pending", "superseded")


def test_approving_current_changes_requested_review_publishes_exact_version(
    session_factory,
):
    write = ContextWriteService()
    reviews = ReviewService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Requested changes approved",
                page_type="overview",
                body_markdown="# Exact\n\nReviewed body.",
                citations=[],
                reason="draft",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        version = session.get(ContextPageVersion, created.draft_version_id)
        reviewed_body = version.body_md
        reviewed_hash = version.content_hash
        requested = reviews.apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="request_changes",
                decision_reason="double-check exact bytes",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        assert requested.status == "changes_requested"

        approved = _approve(session, workspace.id, created.review_item_id)

        page = session.get(ContextPage, created.page_id)
        version = session.get(ContextPageVersion, created.draft_version_id)
        assert approved.status == "approved"
        assert approved.published_version_id == created.draft_version_id
        assert page.current_published_version_id == created.draft_version_id
        assert version.body_md == reviewed_body
        assert version.content_hash == reviewed_hash


def test_auto_fix_toggle_after_review_does_not_mutate_published_bytes(
    session_factory,
):
    write = ContextWriteService()
    reviewed_body = "# Exact bytes   \n\nTrailing spaces stay reviewed.  \n"
    assert safe_normalize_markdown(reviewed_body) != reviewed_body
    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Auto fix toggle",
                page_type="overview",
                body_markdown=reviewed_body,
                citations=[],
                reason="review exact authored bytes",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        version = session.get(ContextPageVersion, created.draft_version_id)
        reviewed_hash = version.content_hash
        workspace.auto_fix_enabled = True

        approved = _approve(session, workspace.id, created.review_item_id)

        version = session.get(ContextPageVersion, created.draft_version_id)
        assert approved.published_version_id == created.draft_version_id
        assert version.body_md == reviewed_body
        assert version.content_hash == reviewed_hash


def test_deadline_system_auto_approval_does_not_publish(session_factory):
    write = ContextWriteService()
    reviews = ReviewService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        policy = default_review_policy()
        for risk in ("low", "medium", "high"):
            policy["automation"]["auto_approve_by_risk"][risk] = {
                "allowed": True,
                "delay_days": 0,
            }
        workspace.review_policy = policy
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="System approval",
                page_type="overview",
                body_markdown="# Draft\n\nWaited.",
                citations=[],
                reason="draft",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        review = session.get(ReviewItem, created.review_item_id)
        review.created_at = datetime.now(UTC) - timedelta(days=1)
        approved = reviews.sweep_auto_approve_reviews(
            session, workspace.id, now=datetime.now(UTC)
        )
        assert approved == [review.id]
        assert review.status == "approved"
        assert (
            session.get(ContextPage, created.page_id).current_published_version_id
            is None
        )

        # Any editor may undo an automatic approval, and the next sweep must
        # leave the reopened review to a person.
        editor = uuid4()
        undo = reviews.get_review(
            session,
            workspace.id,
            review.id,
            actor_user_id=editor,
            actor_role="editor",
            actor=_actor(workspace.id, "editor"),
        ).undo
        reviews.undo_decision(
            session,
            workspace.id,
            review.id,
            undo.decision_id,
            actor_kind="human",
            actor_id=str(editor),
            actor_user_id=editor,
            actor_role="editor",
            actor_provenance="browser_session",
        )
        assert review.status == "pending"
        assert reviews.sweep_auto_approve_reviews(session, workspace.id) == []


@pytest.mark.parametrize("decision_type", ["approve", "reject", "request_changes"])
def test_reconciliation_review_requires_reconcile_or_explicit_discard(
    session_factory,
    page_service,
    decision_type,
):
    page = page_service.write_page(
        workspace_slug="default",
        slug=f"reconciliation-review-{decision_type}",
        title="Reconciliation Review",
        body_md="# Draft\n",
        actor_kind="human",
        actor_id=ACTOR,
        actor_provenance="browser_session",
    )
    reviews = ReviewService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        policy = default_review_policy()
        policy["automation"]["auto_approve_by_risk"]["low"] = {
            "allowed": True,
            "delay_days": 0,
        }
        workspace.review_policy = policy
        review = _seed_review(
            session,
            workspace.id,
            page.id,
            review_type="context_reconciliation",
            version_id=page.version_id,
        )
        review.created_at = datetime.now(UTC) - timedelta(days=1)

        assert reviews.count_auto_approve_candidates(session, workspace.id) == 0
        assert (
            reviews.sweep_auto_approve_reviews(
                session, workspace.id, now=datetime.now(UTC)
            )
            == []
        )
        assert (
            reviews.get_review(
                session,
                workspace.id,
                review.id,
                actor_user_id=ACTOR_UUID,
                actor_role="owner",
            ).can_decide
            is False
        )

        with pytest.raises(ReadValidationError, match="cannot decide"):
            reviews.apply_decision(
                session=session,
                workspace_id=workspace.id,
                review_item_id=review.id,
                request=ReviewDecisionRequest(
                    decision_type=decision_type,
                    decision_reason=(
                        "generic routing must stay disabled"
                        if decision_type == "request_changes"
                        else None
                    ),
                ),
                actor_kind="human",
                actor_id=ACTOR,
                actor_user_id=ACTOR_UUID,
                actor_role="owner",
                actor_provenance="browser_session",
            )
        assert review.status == "pending"

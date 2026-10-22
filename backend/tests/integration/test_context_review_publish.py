from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

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
from backend.services.context_lifecycle import audit_context_history
from backend.services.context_write_service import ContextWriteService
from backend.services.lint.autofix import safe_normalize_markdown
from backend.services.page_service import PageNotFoundError, PageService
from backend.services.read_errors import ReadValidationError
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


def _reject(session, workspace_id, review_id):
    return ReviewService().apply_decision(
        session=session,
        workspace_id=workspace_id,
        review_item_id=review_id,
        request=ReviewDecisionRequest(decision_type="reject"),
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
        assert restarted_version.parent_version_id == created.draft_version_id
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
        assert session.scalar(
            select(func.count())
            .select_from(ReviewDecision)
            .where(ReviewDecision.review_item_id == created.review_item_id)
        ) == 1


def test_reject_latest_draft_restores_published_head_for_both_reader_classes(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, published, draft = _published_page_with_draft(
            session, "Restore published on reject"
        )
        before = ReviewService().get_review(
            session, workspace.id, draft.review_item_id,
            actor_user_id=ACTOR_UUID, actor_role="owner",
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
        assert _decision_audit(session, draft.review_item_id).payload[
            "reject_effect"
        ] == "restore_published"
        page_id = page.id
        workspace_id = workspace.id

    pages = PageService(session_factory)
    assert pages.read_page(
        page_id=page_id, actor=_actor(workspace_id, "editor")
    ).body_md == "# Published\n\nStable body."
    assert pages.read_page(
        page_id=page_id, actor=_actor(workspace_id, "viewer")
    ).body_md == "# Published\n\nStable body."


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
        assert _decision_audit(session, created.review_item_id).payload[
            "reject_effect"
        ] == "hide_unpublished"
        detail = ReviewService().get_review(
            session, workspace.id, created.review_item_id,
            actor_user_id=ACTOR_UUID, actor_role="owner",
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

    history = pages.list_versions(
        page_id, actor=_actor(workspace_id, "editor")
    )
    rejected_entry = next(entry for entry in history if entry.version_id == version_id)
    assert rejected_entry.version_status == "draft"
    assert rejected_entry.review_disposition == "rejected"
    assert pages.read_page(
        page_id=page_id,
        version_id=version_id,
        actor=_actor(workspace_id, "editor"),
    ).version_id == version_id


def test_reject_stale_draft_closes_review_without_moving_head(session_factory):
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
        stale_review = _seed_review(
            session,
            workspace.id,
            first.page_id,
            version_id=first.draft_version_id,
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
        assert page.latest_draft_version_id == latest.draft_version_id
        assert _decision_audit(session, stale_review.id).payload[
            "reject_effect"
        ] == "no_head_change"


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
        assert _decision_audit(session, review.id).payload[
            "reject_effect"
        ] == "no_head_change"


def test_reject_supersedes_only_unclaimed_pending_siblings_for_exact_version(
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
        assert different_version.status == "pending"
        assert changes_requested.status == "changes_requested"
        assert claimed_pending.status == "pending"


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
        assert session.scalar(
            select(func.count())
            .select_from(ReviewDecision)
            .where(ReviewDecision.review_item_id == created.review_item_id)
        ) == 1
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "review.decision",
                AuditEvent.target_kind == "review_item",
                AuditEvent.target_id == created.review_item_id,
            )
        ) == 1


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
                session, workspace.id, review.id,
                actor_user_id=ACTOR_UUID, actor_role="owner",
                actor=_actor(workspace.id, "owner"),
            ).actions.approve.reason_code

        # Before a first publication page metadata follows the newest write,
        # so only the latest draft publishes as reviewed; it settles the rest.
        second = add_draft(2)
        unpublished = _seed_review(
            session, workspace.id, page_id, version_id=drafts[0].id
        )
        assert approve_reason(unpublished) == "stale_candidate"
        _approve(session, workspace.id, second)
        assert unpublished.status == "superseded"

        for number in (3, 4, 5):
            newest = session.get(ReviewItem, add_draft(number))
        published, first, middle, latest = drafts[1:]
        fork = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page_id,
            parent_version_id=first.id,
            version_no=6,
            body_md="# Fork\n\nAlternative.",
            content_hash="6" * 64,
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
            session, workspace.id, reviewed.id,
            actor_user_id=ACTOR_UUID, actor_role="owner",
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
        assert reviews.sweep_auto_approve_reviews(
            session, workspace.id, now=datetime.now(UTC)
        ) == []
        assert reviews.get_review(
            session,
            workspace.id,
            review.id,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
        ).can_decide is False

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

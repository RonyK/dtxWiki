from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

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
    WriteDraftRequest,
)
from backend.services.context_draft_workflow import ContextTransactionFactory
from backend.services.context_lifecycle import (
    audit_context_history,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.lint.autofix import safe_normalize_markdown
from backend.services.page_service import (
    PageNotFoundError,
    PageService,
)
from backend.services.read_errors import (
    ReadValidationError,
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


def _start_draft(
    session,
    workspace_id,
    page_id,
    source_id,
    body=None,
    *,
    actor_kind="human",
    reason="candidate",
    submit=True,
    **metadata,
):
    """Join or start a shared draft from `source_id`, save it, request review."""
    provenance = "browser_session" if actor_kind == "human" else "mcp_client"
    actor = {
        "actor_kind": actor_kind,
        "actor_id": ACTOR,
        "actor_provenance": provenance,
    }
    pages = PageService(ContextTransactionFactory(session))
    draft = pages.begin_context_edit(
        page_id,
        workspace_id,
        idempotency_key=str(uuid4()),
        from_version_id=source_id,
        **actor,
    )
    draft = pages.autosave_page(
        page_id=page_id,
        workspace_id=workspace_id,
        title=metadata.pop("title", None),
        body_md=draft.body_md if body is None else body,
        base_version_id=draft.version_id,
        base_content_hash=draft.content_hash,
        idempotency_key=str(uuid4()),
        expected_editing_revision=draft.editing_revision,
        version_id=draft.version_id,
        **metadata,
        **actor,
    )
    if submit:
        draft = pages.submit_context_edit_for_review(
            page_id,
            draft.version_id,
            workspace_id,
            expected_editing_revision=draft.editing_revision,
            idempotency_key=str(uuid4()),
            reason=reason,
            **actor,
        )
    return SimpleNamespace(
        page_id=page_id,
        draft_version_id=draft.version_id,
        review_item_id=draft.review_item_id,
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
    draft = _start_draft(
        session,
        workspace.id,
        published.page_id,
        base.id,
        "# Rejected draft\n\nCandidate body.",
    )
    return workspace, published, draft


@pytest.mark.parametrize("restart_actor_kind", ["human", "agent"])
def test_rejected_commit_can_start_a_new_independent_draft(
    session_factory, restart_actor_kind
):
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = _write(
            session, workspace.id, "Rejected resubmission", "# Same\n\nBody."
        )
        _reject(session, workspace.id, created.review_item_id)
        closed = session.get(ContextPageVersion, created.draft_version_id)
        assert (
            closed.version_status == "closed"
            and closed.commit_oid
            and closed._body_md is None
        )
        restarted = _start_draft(
            session,
            workspace.id,
            created.page_id,
            closed.id,
            closed.body_md,
            actor_kind=restart_actor_kind,
            reason="start from rejected commit",
        )
        version = session.get(ContextPageVersion, restarted.draft_version_id)
        assert version.id != closed.id and version.parent_version_id == closed.id
        assert session.get(ReviewItem, restarted.review_item_id).status == "pending"
        assert (
            _approve(
                session, workspace.id, restarted.review_item_id
            ).published_version_id
            == version.id
        )
        assert session.get(ReviewItem, created.review_item_id).status == "rejected"


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
        assert before.reject_effect.effect == "no_head_change"
        assert before.reject_effect.published_version_id == published.draft_version_id
        _reject(session, workspace.id, draft.review_item_id)

        page = session.get(ContextPage, published.page_id)
        assert page.latest_draft_version_id is None
        assert page.current_version_id == published.draft_version_id
        assert page.current_published_version_id == published.draft_version_id
        assert page.lifecycle_status == "published"
        assert (
            _decision_audit(session, draft.review_item_id).payload["reject_effect"]
            == "no_head_change"
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


def test_reject_first_draft_keeps_editor_readable_evidence(session_factory):
    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = _write(
            session,
            workspace.id,
            "Rejected evidence",
            "# Rejected evidence\n\nKeep this row.",
        )
        _reject(session, workspace.id, created.review_item_id)
        version = session.get(ContextPageVersion, created.draft_version_id)
        assert version.version_status == "closed" and version.commit_oid
        detail = _detail(session, workspace.id, created.review_item_id)
        assert detail.evidence.operations[0].content.can_read
        workspace_id, page_id, version_id = workspace.id, created.page_id, version.id
    pages = PageService(session_factory)
    assert (
        pages.read_page(
            page_id=page_id, version_id=version_id, actor=_actor(workspace_id, "editor")
        ).version_id
        == version_id
    )
    with pytest.raises(PageNotFoundError):
        pages.read_page(
            page_id=page_id, version_id=version_id, actor=_actor(workspace_id, "viewer")
        )
    history = pages.list_versions(page_id, actor=_actor(workspace_id, "editor"))
    assert (
        next(row for row in history if row.version_id == version_id).review_disposition
        == "rejected"
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
        latest = _start_draft(
            session,
            workspace.id,
            first.page_id,
            base.id,
            "# V2\n\nLatest.",
            reason="v2",
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
        # The source decision settles every sibling, even when the published
        # head already points at that source.
        assert (claimed.status, requested.status) == ("superseded", "superseded")
        assert claimed.claimed_by is None and claimed.claimed_at is None


def test_reject_closes_every_open_review_of_the_retired_version_only(session_factory):
    with session_factory.begin() as session:
        workspace, published, first = _published_page_with_draft(
            session, "Exact reject siblings"
        )
        baseline = session.get(ContextPageVersion, published.draft_version_id)
        independent = _start_draft(
            session,
            workspace.id,
            published.page_id,
            baseline.id,
            "# Independent\n\nOther candidate.",
            reason="independent candidate",
        )
        assert first.draft_version_id != independent.draft_version_id
        same_version = _seed_review(
            session, workspace.id, published.page_id, version_id=first.draft_version_id
        )
        changes_requested = _seed_review(
            session,
            workspace.id,
            published.page_id,
            version_id=first.draft_version_id,
            status="changes_requested",
        )
        claimed_pending = _seed_review(
            session,
            workspace.id,
            published.page_id,
            version_id=first.draft_version_id,
            claimed=True,
        )
        unrelated = session.get(ReviewItem, independent.review_item_id)

        _reject(session, workspace.id, first.review_item_id)

        assert (
            same_version.status
            == changes_requested.status
            == claimed_pending.status
            == "superseded"
        )
        assert session.get(ChangeSet, same_version.change_set_id).status == "superseded"
        assert claimed_pending.claimed_by is None and claimed_pending.claimed_at is None
        assert unrelated.status == "pending"
        assert (
            session.get(ContextPageVersion, first.draft_version_id).version_status
            == "closed"
        )
        assert (
            session.get(ContextPageVersion, independent.draft_version_id).version_status
            == "draft"
        )
        assert (
            session.get(ContextPage, published.page_id).current_published_version_id
            == baseline.id
        )


def test_draft_metadata_stays_isolated_until_publication(session_factory):
    with session_factory.begin() as session:
        workspace = _workspace(session)
        first = _write(
            session, workspace.id, "Metadata isolation", "# Original\n\nBody."
        )
        version = session.get(ContextPageVersion, first.draft_version_id)
        _start_draft(
            session,
            workspace.id,
            first.page_id,
            version.id,
            (
                "---\ntitle: Frontmatter title\ntags: [frontmatter-tag]\n"
                "aliases: [Frontmatter alias]\n---\n# Original\n\nBody."
            ),
            submit=False,
        )
        assert version.page_metadata["title"] == "Frontmatter title"
        assert version.page_metadata["tags"] == ["frontmatter-tag"]
        assert version.page_metadata["aliases"] == ["Frontmatter alias"]
        changed = _start_draft(
            session,
            workspace.id,
            first.page_id,
            version.id,
            submit=False,
            title="Renamed draft",
            tags=["draft-tag"],
        )
        page = session.get(ContextPage, first.page_id)
        assert page.title == "Metadata isolation" and page.tags == []
        assert version.page_metadata["title"] == "Renamed draft"
        approved = _approve(session, workspace.id, first.review_item_id)
        assert approved.published_version_id == changed.draft_version_id
        assert page.title == "Renamed draft" and page.tags == ["draft-tag"]


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
        assert version.body_md == safe_normalize_markdown(reviewed_body)
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
                "delay_days": 1,
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
        review.created_at = datetime.now(UTC) - timedelta(days=2)
        # The shared draft was saved after the review opened: its delay restarts.
        draft = session.get(ContextPageVersion, created.draft_version_id)
        draft.last_saved_at = datetime.now(UTC)
        assert reviews.sweep_auto_approve_reviews(session, workspace.id) == []
        draft.last_saved_at = datetime.now(UTC) - timedelta(days=2)
        # Approving the draft settles its other open reviews; one reserved for
        # a person keeps the system from deciding either.
        sibling = ReviewItem(
            workspace_id=workspace.id,
            change_set_id=review.change_set_id,
            review_type="context_update",
            status="pending",
            risk_level="high",
            risk_signals={"caller_needs_review": True},
        )
        session.add(sibling)
        session.flush()
        assert reviews.count_auto_approve_candidates(session, workspace.id) == 0
        assert reviews.sweep_auto_approve_reviews(session, workspace.id) == []
        session.delete(sibling)
        session.flush()
        approved = reviews.sweep_auto_approve_reviews(
            session, workspace.id, now=datetime.now(UTC)
        )
        assert approved == [review.id]
        assert review.status == "approved"
        assert (
            session.get(ContextPage, created.page_id).current_published_version_id
            == created.draft_version_id
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


# The reconciliation gate (_can_decide_review) precedes decision-type dispatch,
# so every generic decision takes one branch; approve is the one that publishes.
@pytest.mark.parametrize("decision_type", ["approve"])
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

from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.models.audit import AuditEvent
from backend.models.base import Base, utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.schemas.context_write import WriteDraftRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_draft_workflow import ContextTransactionFactory
from backend.services.context_write_service import ContextWriteService
from backend.services.page_service import PageService
from backend.services.review_service import ReviewService
from backend.tests.utils.context_git import configure_test_context_storage

ACTOR = "00000000-0000-0000-0000-000000000010"
CLAIMANT = UUID("00000000-0000-0000-0000-000000000099")


def _make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return configure_test_context_storage(
        sessionmaker(bind=engine, expire_on_commit=False)
    )


def _add_page_review(
    session,
    workspace_id,
    page_id,
    review_type,
    status,
    *,
    version_id=None,
):
    change_set = ChangeSet(
        workspace_id=workspace_id,
        reason=f"seed {review_type}",
        trigger_type="test_seed",
        actor_type="human",
        actor_id=CLAIMANT,
        status="needs_changes" if status == "changes_requested" else "draft_created",
        risk_level="low",
    )
    session.add(change_set)
    session.flush()
    session.add(
        ChangeOperation(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page_id,
            operation="test_seed",
            diff_ref=(f"context_page_version:{version_id}" if version_id else None),
        )
    )
    review = ReviewItem(
        workspace_id=workspace_id,
        change_set_id=change_set.id,
        review_type=review_type,
        status=status,
        risk_level="low",
        decision_summary="seed review",
        claimed_by=CLAIMANT,
        claimed_at=utc_now(),
    )
    session.add(review)
    session.flush()
    return review, change_set


def _update(session, workspace_id, page_id, draft_id, body, reason):
    """Save the shared draft, then request review of it again."""
    draft = session.get(ContextPageVersion, draft_id)
    pages = PageService(ContextTransactionFactory(session))
    actor = {
        "actor_kind": "human",
        "actor_id": ACTOR,
        "actor_provenance": "browser_session",
    }
    saved = pages.autosave_page(
        page_id=page_id,
        workspace_id=workspace_id,
        title=None,
        body_md=body,
        base_version_id=draft.id,
        base_content_hash=draft.content_hash,
        idempotency_key=str(uuid4()),
        expected_editing_revision=draft.editing_revision,
        version_id=draft.id,
        **actor,
    )
    submitted = pages.submit_context_edit_for_review(
        page_id,
        saved.version_id,
        workspace_id,
        expected_editing_revision=saved.editing_revision,
        idempotency_key=str(uuid4()),
        reason=reason,
        **actor,
    )
    return SimpleNamespace(
        draft_version_id=submitted.version_id,
        review_item_id=submitted.review_item_id,
    )


def test_shared_draft_edits_reuse_its_request_and_preserve_other_reviews():
    session_factory = _make_session_factory()
    write_service = ContextWriteService()
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        created = write_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Supersession Page",
                page_type="overview",
                body_markdown="# V1\n\nInitial body.",
                citations=[],
                reason="v1",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        original = session.get(ReviewItem, created.review_item_id)
        original.claimed_by = CLAIMANT
        original.claimed_at = utc_now()

        inline, inline_change_set = _add_page_review(
            session, workspace.id, created.page_id, "page_inline_comment", "pending"
        )
        deleted, deleted_change_set = _add_page_review(
            session, workspace.id, created.page_id, "source_deleted_notice", "pending"
        )
        requested, requested_change_set = _add_page_review(
            session,
            workspace.id,
            created.page_id,
            "context_update",
            "changes_requested",
        )
        decision = ReviewDecision(
            workspace_id=requested.workspace_id,
            review_item_id=requested.id,
            decision_type="request_changes",
            actor_type="human",
            actor_id=CLAIMANT,
            decision_reason="keep collaborating",
            audience_kind="requester",
            audience_user_id=CLAIMANT,
            resulting_review_status="changes_requested",
            resulting_change_set_status="needs_changes",
        )
        session.add(decision)
        session.flush()
        decision_snapshot = (
            decision.id,
            decision.review_item_id,
            decision.decision_type,
            decision.resulting_review_status,
            decision.resulting_change_set_status,
        )

        second = _update(
            session,
            workspace.id,
            created.page_id,
            created.draft_version_id,
            "# V2\n\nSecond body.",
            "v2",
        )
        third = _update(
            session,
            workspace.id,
            created.page_id,
            second.draft_version_id,
            "# V3\n\nNewest body.",
            "v3",
        )

        context_reviews = session.scalars(
            select(ReviewItem)
            .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
            .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ReviewItem.workspace_id == workspace.id,
                ReviewItem.review_type == "context_update",
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == created.page_id,
            )
        ).all()
        assert [r.id for r in context_reviews if r.status == "pending"] == [
            third.review_item_id
        ]

        assert (
            created.draft_version_id
            == second.draft_version_id
            == third.draft_version_id
        )
        assert created.review_item_id == second.review_item_id == third.review_item_id
        assert not [r for r in context_reviews if r.status == "superseded"]
        assert original.claimed_by == CLAIMANT
        assert inline.status == "pending"
        assert inline.claimed_by == CLAIMANT
        assert inline_change_set.status == "draft_created"
        assert deleted.status == "pending"
        assert deleted.claimed_by == CLAIMANT
        assert deleted_change_set.status == "draft_created"
        assert requested.status == "changes_requested"
        assert requested.claimed_by == CLAIMANT
        assert requested_change_set.status == "needs_changes"

        decisions = session.scalars(select(ReviewDecision)).all()
        assert len(decisions) == 1
        assert (
            decisions[0].id,
            decisions[0].review_item_id,
            decisions[0].decision_type,
            decisions[0].resulting_review_status,
            decisions[0].resulting_change_set_status,
        ) == decision_snapshot

        lineage = session.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace.id,
                AuditEvent.action == "review.revision_submitted",
            )
        ).all()
        assert lineage == []

        counts = review_service.count_reviews(
            session=session,
            workspace_id=workspace.id,
            actor_role="editor",
        )
        assert counts.pending == 3
        assert counts.changes_requested == 1
        assert counts.resolved == 0
        direct_open_count = session.scalar(
            select(func.count(ReviewItem.id)).where(
                ReviewItem.workspace_id == workspace.id,
                ReviewItem.status.in_(("pending", "changes_requested")),
            )
        )
        assert direct_open_count == counts.pending + counts.changes_requested

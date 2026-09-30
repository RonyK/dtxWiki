from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.scripts import close_ghost_pending_reviews
from backend.scripts.close_ghost_pending_reviews import close_ghost_reviews
from backend.scripts.seed_dev import seed_default_workspace


def _review(session, workspace, page, review_type, *, claimed=False, number=1):
    change_set = ChangeSet(
        workspace_id=workspace.id,
        reason="test",
        trigger_type="mcp_write_draft",
        actor_type="mcp_client",
        actor_id=None,
        status="draft_created",
        risk_level="low",
        cite_call_skipped=False,
    )
    session.add(change_set)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace.id,
        page_id=page.id,
        parent_version_id=None,
        version_no=number,
        body_md=f"# version {number}",
        content_hash=f"hash-{number}",
        graph_status="linear",
        actor_type="mcp_client",
        actor_id=None,
        change_set_id=change_set.id,
        version_status="draft",
        created_by_actor_kind="mcp_client",
        created_by_actor_id="test-agent",
    )
    session.add(version)
    session.flush()
    session.add(
        ChangeOperation(
            workspace_id=workspace.id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page.id,
            operation="create_draft",
            diff_summary="test",
            diff_ref=f"context_page_version:{version.id}",
        )
    )
    review = ReviewItem(
        workspace_id=workspace.id,
        change_set_id=change_set.id,
        review_type=review_type,
        status="pending",
        risk_level="low",
        claimed_by=uuid4() if claimed else None,
    )
    session.add(review)
    session.flush()
    return review, change_set, version


def _fixture_rows(session):
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    page = ContextPage(
        workspace_id=workspace.id,
        slug="ghost-test",
        title="Ghost test",
        page_type="overview",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    ghost, ghost_cs, ghost_version = _review(
        session, workspace, page, "context_update", number=1
    )
    healthy, _, healthy_version = _review(
        session, workspace, page, "context_update", number=2
    )
    claimed, _, _ = _review(
        session, workspace, page, "context_update", claimed=True, number=3
    )
    inline, _, _ = _review(session, workspace, page, "page_inline_comment", number=4)
    deleted, _, _ = _review(session, workspace, page, "source_deleted_notice", number=5)
    page.latest_draft_version_id = healthy_version.id
    page.current_version_id = healthy_version.id
    return locals()


@pytest.fixture
def independent_session_factory(tmp_path):
    database_path = tmp_path / "ghost-review-stale-identity.sqlite3"
    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        seed_default_workspace(session)
    try:
        yield factory
    finally:
        engine.dispose()


def test_dry_run_reports_ghost_and_mutates_nothing(session_factory):
    with session_factory.begin() as session:
        rows = _fixture_rows(session)
        report = close_ghost_reviews(session)
        assert [item.review_id for item in report] == [rows["ghost"].id]
        session.flush()
        assert rows["ghost"].status == "pending"
        assert rows["ghost_cs"].status == "draft_created"
        assert (
            session.scalars(
                select(AuditEvent).where(AuditEvent.action == "review.ghost_closed")
            ).all()
            == []
        )


def test_apply_closes_exactly_ghost_and_emits_one_audit_each(session_factory):
    with session_factory.begin() as session:
        rows = _fixture_rows(session)
        report = close_ghost_reviews(
            session, workspace_id=rows["workspace"].id, apply=True
        )
        assert [item.review_id for item in report] == [rows["ghost"].id]
        assert rows["ghost"].status == "superseded"
        assert rows["ghost_cs"].status == "superseded"
        for key in ("healthy", "claimed", "inline", "deleted"):
            assert rows[key].status == "pending"
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "review.ghost_closed")
        ).all()
        assert len(events) == 1
        assert events[0].target_id == rows["ghost"].id
        assert events[0].payload["reason"] == "target_superseded_or_orphaned"


def test_apply_closes_production_pending_review_change_set(session_factory):
    with session_factory.begin() as session:
        rows = _fixture_rows(session)
        rows["ghost_cs"].status = "pending_review"
        session.flush()

        report = close_ghost_reviews(
            session, workspace_id=rows["workspace"].id, apply=True
        )

        assert [item.review_id for item in report] == [rows["ghost"].id]
        assert rows["ghost"].status == "superseded"
        assert rows["ghost_cs"].status == "superseded"


def test_a_review_that_became_decidable_after_discovery_is_left_alone(
    session_factory, monkeypatch
):
    # Discovery and closure are separate steps, so a restore landing between
    # them would otherwise close a review someone can now actually decide.
    # Stand in for that interleaving with a stale discovery result.
    with session_factory.begin() as session:
        rows = _fixture_rows(session)
        stale = close_ghost_pending_reviews.find_ghost_reviews(
            session, workspace_id=rows["workspace"].id
        )
        assert [item.review_id for item in stale] == [rows["ghost"].id]

        rows["page"].latest_draft_version_id = rows["ghost_version"].id
        session.flush()
        monkeypatch.setattr(
            close_ghost_pending_reviews, "find_ghost_reviews", lambda *a, **k: stale
        )

        closed = close_ghost_reviews(
            session, workspace_id=rows["workspace"].id, apply=True
        )

        assert closed == []
        assert rows["ghost"].status == "pending"
        assert rows["ghost_cs"].status == "draft_created"
        assert (
            session.scalars(
                select(AuditEvent).where(AuditEvent.action == "review.ghost_closed")
            ).all()
            == []
        )


def test_apply_locks_page_before_review(session_factory, monkeypatch):
    with session_factory.begin() as session:
        rows = _fixture_rows(session)
        second_page = ContextPage(
            workspace_id=rows["workspace"].id,
            slug="second-ghost-test",
            title="Second ghost test",
            page_type="overview",
            lifecycle_status="published",
        )
        session.add(second_page)
        session.flush()
        second_ghost, _, _ = _review(
            session,
            rows["workspace"],
            second_page,
            "context_update",
            number=1,
        )
        stale = close_ghost_pending_reviews.find_ghost_reviews(
            session, workspace_id=rows["workspace"].id
        )
        monkeypatch.setattr(
            close_ghost_pending_reviews, "find_ghost_reviews", lambda *a, **k: stale
        )
        lock_order = []
        session_type = type(session)
        original_get = session_type.get

        def tracking_get(self, entity, ident, **kwargs):
            if kwargs.get("with_for_update"):
                lock_order.append(entity)
            return original_get(self, entity, ident, **kwargs)

        monkeypatch.setattr(session_type, "get", tracking_get)

        closed = close_ghost_reviews(
            session, workspace_id=rows["workspace"].id, apply=True
        )

        assert {item.review_id for item in closed} == {
            rows["ghost"].id,
            second_ghost.id,
        }
        assert lock_order == [ContextPage, ContextPage, ReviewItem, ReviewItem]


def test_apply_refuses_a_review_with_multiple_page_targets(
    session_factory, monkeypatch
):
    with session_factory.begin() as session:
        rows = _fixture_rows(session)
        second_page = ContextPage(
            workspace_id=rows["workspace"].id,
            slug="ambiguous-ghost-target",
            title="Ambiguous ghost target",
            page_type="overview",
            lifecycle_status="published",
        )
        session.add(second_page)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=rows["workspace"].id,
                change_set_id=rows["ghost_cs"].id,
                target_type="context_page",
                target_id=second_page.id,
                operation="create_draft",
                diff_summary="ambiguous legacy target",
                diff_ref=f"context_page_version:{rows['ghost_version'].id}",
            )
        )
        session.flush()
        stale = close_ghost_pending_reviews.find_ghost_reviews(
            session, workspace_id=rows["workspace"].id
        )
        assert [item.review_id for item in stale].count(rows["ghost"].id) == 2
        monkeypatch.setattr(
            close_ghost_pending_reviews, "find_ghost_reviews", lambda *a, **k: stale
        )

        closed = close_ghost_reviews(
            session, workspace_id=rows["workspace"].id, apply=True
        )

        assert closed == []
        assert rows["ghost"].status == "pending"
        assert rows["ghost_cs"].status == "draft_created"
        assert (
            session.scalars(
                select(AuditEvent).where(AuditEvent.action == "review.ghost_closed")
            ).all()
            == []
        )


@pytest.mark.parametrize(
    "mutation",
    ["status", "claim", "type", "change_set"],
)
def test_apply_revalidates_review_identity_and_state_after_discovery(
    session_factory, monkeypatch, mutation
):
    with session_factory.begin() as session:
        rows = _fixture_rows(session)
        stale = close_ghost_pending_reviews.find_ghost_reviews(
            session, workspace_id=rows["workspace"].id
        )
        assert [item.review_id for item in stale] == [rows["ghost"].id]
        if mutation == "status":
            rows["ghost"].status = "approved"
        elif mutation == "claim":
            rows["ghost"].claimed_by = uuid4()
        elif mutation == "type":
            rows["ghost"].review_type = "page_inline_comment"
        else:
            rows["ghost"].change_set_id = rows["inline"].change_set_id
        session.flush()
        monkeypatch.setattr(
            close_ghost_pending_reviews, "find_ghost_reviews", lambda *a, **k: stale
        )

        closed = close_ghost_reviews(
            session, workspace_id=rows["workspace"].id, apply=True
        )

        assert closed == []
        assert rows["ghost_cs"].status == "draft_created"
        assert (
            session.scalars(
                select(AuditEvent).where(AuditEvent.action == "review.ghost_closed")
            ).all()
            == []
        )


def test_apply_refreshes_retained_terminal_review_and_change_set_from_other_session(
    independent_session_factory, monkeypatch
):
    factory = independent_session_factory
    with factory.begin() as setup_session:
        rows = _fixture_rows(setup_session)
        workspace_id = rows["workspace"].id
        page_id = rows["page"].id
        review_id = rows["ghost"].id
        change_set_id = rows["ghost_cs"].id
        version_id = rows["ghost_version"].id
        original_pointer = rows["page"].latest_draft_version_id

    with factory() as stale_session:
        stale = close_ghost_pending_reviews.find_ghost_reviews(
            stale_session, workspace_id=workspace_id
        )
        assert [item.review_id for item in stale] == [review_id]
        # Hold strong references so every classification object remains in
        # Session A's identity map while Session B commits newer state.
        retained_page = stale_session.get(ContextPage, page_id)
        retained_review = stale_session.get(ReviewItem, review_id)
        retained_change_set = stale_session.get(ChangeSet, change_set_id)
        retained_version = stale_session.get(ContextPageVersion, version_id)
        assert retained_page is not None
        assert retained_review is not None and retained_review.status == "pending"
        assert retained_change_set is not None
        assert retained_change_set.status == "draft_created"
        assert retained_version is not None

        with factory.begin() as deciding_session:
            decided_review = deciding_session.get(ReviewItem, review_id)
            decided_change_set = deciding_session.get(ChangeSet, change_set_id)
            assert decided_review is not None and decided_change_set is not None
            decided_review.status = "changes_requested"
            decided_change_set.status = "needs_changes"

        monkeypatch.setattr(
            close_ghost_pending_reviews,
            "find_ghost_reviews",
            lambda *args, **kwargs: stale,
        )
        closed = close_ghost_reviews(
            stale_session, workspace_id=workspace_id, apply=True
        )

        assert closed == []
        assert retained_review.status == "changes_requested"
        assert retained_change_set.status == "needs_changes"
        assert retained_page.latest_draft_version_id == original_pointer
        assert retained_version.version_status == "draft"
        assert (
            stale_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "review.ghost_closed",
                    AuditEvent.target_id == review_id,
                )
            ).all()
            == []
        )
        stale_session.commit()

    with factory() as verification_session:
        review = verification_session.get(ReviewItem, review_id)
        change_set = verification_session.get(ChangeSet, change_set_id)
        page = verification_session.get(ContextPage, page_id)
        version = verification_session.get(ContextPageVersion, version_id)
        assert review is not None and review.status == "changes_requested"
        assert change_set is not None and change_set.status == "needs_changes"
        assert page is not None and page.latest_draft_version_id == original_pointer
        assert version is not None and version.version_status == "draft"


@pytest.mark.parametrize(
    "newer_change_set_status",
    [
        "needs_changes",
        "approved",
        "applied",
        "superseded",
        "needs_review",
        "unknown_status",
    ],
)
def test_apply_refuses_retained_terminal_foreign_or_unknown_change_set_status(
    independent_session_factory,
    monkeypatch,
    newer_change_set_status,
):
    factory = independent_session_factory
    with factory.begin() as setup_session:
        rows = _fixture_rows(setup_session)
        workspace_id = rows["workspace"].id
        review_id = rows["ghost"].id
        change_set_id = rows["ghost_cs"].id

    with factory() as stale_session:
        stale = close_ghost_pending_reviews.find_ghost_reviews(
            stale_session, workspace_id=workspace_id
        )
        assert [item.review_id for item in stale] == [review_id]
        retained_review = stale_session.get(ReviewItem, review_id)
        retained_change_set = stale_session.get(ChangeSet, change_set_id)
        assert retained_review is not None and retained_review.status == "pending"
        assert retained_change_set is not None
        assert retained_change_set.status == "draft_created"

        with factory.begin() as newer_session:
            newer_review = newer_session.get(ReviewItem, review_id)
            newer_change_set = newer_session.get(ChangeSet, change_set_id)
            assert newer_review is not None and newer_review.status == "pending"
            assert newer_change_set is not None
            newer_change_set.status = newer_change_set_status

        monkeypatch.setattr(
            close_ghost_pending_reviews,
            "find_ghost_reviews",
            lambda *args, **kwargs: stale,
        )
        closed = close_ghost_reviews(
            stale_session, workspace_id=workspace_id, apply=True
        )

        assert closed == []
        assert retained_review.status == "pending"
        assert retained_change_set.status == newer_change_set_status
        assert (
            stale_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "review.ghost_closed",
                    AuditEvent.target_id == review_id,
                )
            ).all()
            == []
        )
        stale_session.commit()

    with factory() as verification_session:
        review = verification_session.get(ReviewItem, review_id)
        change_set = verification_session.get(ChangeSet, change_set_id)
        assert review is not None and review.status == "pending"
        assert change_set is not None
        assert change_set.status == newer_change_set_status
        assert (
            verification_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "review.ghost_closed",
                    AuditEvent.target_id == review_id,
                )
            ).all()
            == []
        )


def test_apply_refreshes_retained_page_and_version_that_became_decidable(
    independent_session_factory, monkeypatch
):
    factory = independent_session_factory
    with factory.begin() as setup_session:
        rows = _fixture_rows(setup_session)
        rows["ghost_version"].version_status = "superseded_snapshot"
        workspace_id = rows["workspace"].id
        page_id = rows["page"].id
        review_id = rows["ghost"].id
        change_set_id = rows["ghost_cs"].id
        version_id = rows["ghost_version"].id

    with factory() as stale_session:
        stale = close_ghost_pending_reviews.find_ghost_reviews(
            stale_session, workspace_id=workspace_id
        )
        assert [item.review_id for item in stale] == [review_id]
        retained_page = stale_session.get(ContextPage, page_id)
        retained_review = stale_session.get(ReviewItem, review_id)
        retained_change_set = stale_session.get(ChangeSet, change_set_id)
        retained_version = stale_session.get(ContextPageVersion, version_id)
        assert retained_page is not None
        assert retained_page.latest_draft_version_id != version_id
        assert retained_review is not None and retained_review.status == "pending"
        assert retained_change_set is not None
        assert retained_change_set.status == "draft_created"
        assert retained_version is not None
        assert retained_version.version_status == "superseded_snapshot"

        with factory.begin() as restoring_session:
            restored_page = restoring_session.get(ContextPage, page_id)
            restored_version = restoring_session.get(ContextPageVersion, version_id)
            assert restored_page is not None and restored_version is not None
            restored_page.latest_draft_version_id = version_id
            restored_version.version_status = "draft"

        monkeypatch.setattr(
            close_ghost_pending_reviews,
            "find_ghost_reviews",
            lambda *args, **kwargs: stale,
        )
        closed = close_ghost_reviews(
            stale_session, workspace_id=workspace_id, apply=True
        )

        assert closed == []
        assert retained_page.latest_draft_version_id == version_id
        assert retained_version.version_status == "draft"
        assert retained_review.status == "pending"
        assert retained_change_set.status == "draft_created"
        assert (
            stale_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "review.ghost_closed",
                    AuditEvent.target_id == review_id,
                )
            ).all()
            == []
        )
        stale_session.commit()

    with factory() as verification_session:
        review = verification_session.get(ReviewItem, review_id)
        change_set = verification_session.get(ChangeSet, change_set_id)
        page = verification_session.get(ContextPage, page_id)
        version = verification_session.get(ContextPageVersion, version_id)
        assert review is not None and review.status == "pending"
        assert change_set is not None and change_set.status == "draft_created"
        assert page is not None and page.latest_draft_version_id == version_id
        assert version is not None and version.version_status == "draft"


def test_a_review_whose_version_already_published_is_reported_as_published(
    session_factory,
):
    with session_factory.begin() as session:
        rows = _fixture_rows(session)
        rows["ghost_version"].version_status = "published_snapshot"
        session.flush()
        report = close_ghost_reviews(session, workspace_id=rows["workspace"].id)
        reasons = {item.review_id: item.reason for item in report}
        assert reasons == {rows["ghost"].id: "target_already_published"}


def test_a_review_whose_target_version_vanished_is_reported_as_missing(session_factory):
    with session_factory.begin() as session:
        rows = _fixture_rows(session)
        # The ChangeOperation still names the version by diff_ref, but the row
        # itself is gone -- the shape left behind when a version was hard
        # deleted out from under a review that still points at it.
        session.delete(rows["ghost_version"])
        session.flush()
        report = close_ghost_reviews(session, workspace_id=rows["workspace"].id)
        reasons = {item.review_id: item.reason for item in report}
        assert reasons == {rows["ghost"].id: "target_version_missing"}

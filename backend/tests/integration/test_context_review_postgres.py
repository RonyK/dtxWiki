import inspect
import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.models.audit import AuditEvent
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.context_write import ReviewDecisionRequest, WriteDraftRequest
from backend.scripts.close_ghost_pending_reviews import find_ghost_reviews
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_write_service import ContextWriteService
from backend.services.credential_hashing import CredentialHasher
from backend.services.page_service import PageService
from backend.services.read_errors import (
    ReviewDecisionIdConflictError,
    StaleBaseVersionError,
)
from backend.services.review_service import ReviewService
from backend.tests.utils.context_git import configure_test_context_storage
from backend.tests.utils.postgres import (
    create_isolated_postgres_metadata,
    drop_reflected_postgres_metadata,
)


def _postgres_url() -> str | None:
    value = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


def test_review_item_entity_queries_do_not_use_sql_distinct():
    affected_queries = (
        PageService._supersede_unresolved_context_reviews_for_version,
        PageService._context_reviews_for_version,
        ReviewService._approval_supersession_context,
        find_ghost_reviews,
    )
    for query_owner in affected_queries:
        assert ".distinct()" not in inspect.getsource(query_owner)


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires the dedicated PostgreSQL migration test database",
)
@pytest.mark.parametrize(
    "replacement_provenance",
    ["browser_session", "personal_access_token"],
)
def test_postgres_shared_save_serializes_before_stale_review_approval(
    monkeypatch,
    replacement_provenance,
):
    """A save holding the page lock makes an old approval preview conflict."""
    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    create_isolated_postgres_metadata(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    configure_test_context_storage(session_factory)
    save_holds_page = threading.Event()
    release_save = threading.Event()
    saved = []
    save_errors: list[BaseException] = []
    approval_errors: list[BaseException] = []
    threads = []
    try:
        with session_factory.begin() as session:
            workspace = seed_default_workspace(session)
            user = User(
                email="postgres-review-race@example.com",
                password_hash=CredentialHasher().hash_password("correct-password"),
                display_name="Review race",
                status="active",
            )
            session.add(user)
            session.flush()
            grant_workspace_membership(
                session, user_id=user.id, workspace_id=workspace.id, role="owner"
            )
            workspace_id, actor_id = workspace.id, str(user.id)
        pages = PageService(session_factory)
        published = pages.write_page(
            workspace_slug="default",
            slug="postgres-reviewed-bytes",
            title="Reviewed bytes",
            body_md="# Page\n\nPublished.\n",
            actor_kind="human",
            actor_id=actor_id,
            actor_provenance="browser_session",
        )
        edit = pages.begin_context_edit(
            page_id=published.id,
            workspace_id=workspace_id,
            from_version_id=published.version_id,
            actor_kind="human",
            actor_id=actor_id,
            actor_provenance="personal_access_token",
            idempotency_key="postgres-review-begin",
        )
        reviewed = pages.autosave_page(
            page_id=published.id,
            workspace_id=workspace_id,
            version_id=edit.version_id,
            title=published.title,
            body_md="# Page\n\nReviewed PAT bytes.\n",
            actor_kind="human",
            actor_id=actor_id,
            base_version_id=edit.version_id,
            base_content_hash=edit.content_hash,
            expected_editing_revision=edit.editing_revision,
            actor_provenance="personal_access_token",
            idempotency_key="postgres-reviewed-bytes",
        )
        reviewed = pages.submit_context_edit_for_review(
            page_id=published.id,
            workspace_id=workspace_id,
            version_id=reviewed.version_id,
            expected_editing_revision=reviewed.editing_revision,
            actor_kind="human",
            actor_id=actor_id,
            actor_provenance="personal_access_token",
            idempotency_key="postgres-reviewed-request",
        )
        assert reviewed.review_item_id is not None
        preview = pages.preview_context_merge(
            page_id=published.id,
            workspace_id=workspace_id,
            version_id=reviewed.version_id,
        )
        request = ReviewDecisionRequest(
            decision_type="approve",
            expected_editing_revision=preview["editing_revision"],
            expected_published_version_id=preview["published_version_id"],
            result_hash=preview["result"]["result_hash"],
        )
        with session_factory() as session:
            decisions_before = session.scalar(select(func.count(ReviewDecision.id)))
        original_persist = PageService._autosave_persist_editing_row

        def paused_persist(service, **kwargs):
            save_holds_page.set()
            assert release_save.wait(timeout=10)
            return original_persist(service, **kwargs)

        monkeypatch.setattr(
            PageService, "_autosave_persist_editing_row", paused_persist
        )

        def save_shared_draft():
            try:
                saved.append(
                    pages.autosave_page(
                        page_id=published.id,
                        workspace_id=workspace_id,
                        version_id=reviewed.version_id,
                        title=published.title,
                        body_md="# Page\n\nConcurrent successor bytes.\n",
                        actor_kind="human",
                        actor_id=actor_id,
                        base_version_id=reviewed.version_id,
                        base_content_hash=reviewed.content_hash,
                        expected_editing_revision=reviewed.editing_revision,
                        actor_provenance=replacement_provenance,
                        idempotency_key="postgres-review-race-save",
                    )
                )
            except BaseException as exc:
                save_errors.append(exc)

        def approve_review():
            try:
                with session_factory.begin() as session:
                    ReviewService().apply_decision(
                        session=session,
                        workspace_id=workspace_id,
                        review_item_id=reviewed.review_item_id,
                        request=request,
                        actor_kind="human",
                        actor_id=actor_id,
                        actor_role="owner",
                        actor_provenance="browser_session",
                    )
            except BaseException as exc:
                approval_errors.append(exc)

        threads = [
            threading.Thread(target=save_shared_draft),
            threading.Thread(target=approve_review),
        ]
        threads[0].start()
        assert save_holds_page.wait(timeout=10)
        threads[1].start()
        release_save.set()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads)
        assert save_errors == [] and len(saved) == 1
        assert saved[0].version_id == reviewed.version_id
        assert saved[0].editing_revision == reviewed.editing_revision + 1
        assert len(approval_errors) == 1
        assert isinstance(approval_errors[0], StaleBaseVersionError)
        with session_factory() as session:
            version = session.get(ContextPageVersion, reviewed.version_id)
            review = session.get(ReviewItem, reviewed.review_item_id)
            page = session.get(ContextPage, published.id)
            assert version.body_md == "# Page\n\nConcurrent successor bytes.\n"
            assert version.version_status == "draft"
            assert review.status == "pending"
            assert page.current_published_version_id == published.version_id
            assert (
                session.scalar(select(func.count(ReviewDecision.id)))
                == decisions_before
            )
            assert (
                session.scalar(
                    select(func.count(ReviewDecision.id)).where(
                        ReviewDecision.review_item_id == reviewed.review_item_id
                    )
                )
                == 0
            )
    finally:
        release_save.set()
        for thread in threads:
            thread.join(timeout=15)
        drop_reflected_postgres_metadata(engine)
        engine.dispose()


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires the dedicated PostgreSQL migration test database",
)
@pytest.mark.parametrize("same_review", [True, False])
def test_postgres_decision_receipt_serializes_retry_and_cross_review_collision(
    monkeypatch,
    same_review,
):
    """Same-item row locking and cross-item PK reservation both prevent repeats."""
    engine = create_engine(_postgres_url())
    create_isolated_postgres_metadata(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    configure_test_context_storage(factory)
    service = ReviewService()
    start = threading.Barrier(2)
    before_insert = threading.Barrier(2)
    results = []
    errors = []
    threads = []
    try:
        with factory.begin() as session:
            workspace = seed_default_workspace(session)
            owner = User(
                email="postgres-decision-receipt@example.com",
                password_hash="x",
                display_name="Receipt owner",
            )
            session.add(owner)
            session.flush()
            grant_workspace_membership(
                session, user_id=owner.id, workspace_id=workspace.id, role="owner"
            )
            workspace_id, owner_id = workspace.id, owner.id
            other_workspace = seed_default_workspace(
                session, slug="receipt-other-workspace", title="Other workspace"
            )
            grant_workspace_membership(
                session,
                user_id=owner.id,
                workspace_id=other_workspace.id,
                role="owner",
            )
            workspace_ids = [workspace_id, other_workspace.id]
            review_ids = [
                ContextWriteService()
                .write_draft(
                    session=session,
                    workspace_id=workspace_ids[index],
                    request=WriteDraftRequest(
                        title=f"Receipt concurrency {index}",
                        page_type="overview",
                        body_markdown="# Candidate\n\nReviewed content.",
                        reason="receipt concurrency",
                    ),
                    actor_kind="human",
                    actor_id=str(owner_id),
                    actor_role="owner",
                )
                .review_item_id
                for index in range(2)
            ]
        request = ReviewDecisionRequest(
            decision_id=uuid4(),
            decision_type="request_changes",
            decision_reason="One durable routing round.",
            audience_kind="requester",
        )
        original = service._resolve_audience

        def synchronize_before_insert(**kwargs):
            audience = original(**kwargs)
            # Different items hold different page/review locks: ensure both
            # have observed an absent receipt before the PK arbitrates them.
            if not same_review:
                before_insert.wait(timeout=10)
            return audience

        monkeypatch.setattr(service, "_resolve_audience", synchronize_before_insert)

        def decide(target_workspace_id, review_id):
            try:
                start.wait(timeout=10)
                with factory.begin() as session:
                    detail = service.apply_decision(
                        session=session,
                        workspace_id=target_workspace_id,
                        review_item_id=review_id,
                        request=request,
                        actor_kind="human",
                        actor_id=str(owner_id),
                        actor_user_id=owner_id,
                        actor_role="owner",
                        actor_provenance="browser_session",
                    )
                results.append(detail)
            except BaseException as exc:
                errors.append(exc)

        targets = [(workspace_ids[0], review_ids[0])]
        targets.append(targets[0] if same_review else (workspace_ids[1], review_ids[1]))
        threads = [threading.Thread(target=decide, args=target) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads)
        assert len(results) == (2 if same_review else 1)
        assert len(errors) == (0 if same_review else 1)
        assert all(isinstance(error, ReviewDecisionIdConflictError) for error in errors)
        with factory() as session:
            assert session.scalar(select(func.count(ReviewDecision.id))) == 1
            assert (
                session.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.action == "review.decision"
                    )
                )
                == 1
            )
            states = [session.get(ReviewItem, target).status for target in review_ids]
            assert sorted(states) == ["changes_requested", "pending"]
    finally:
        start.abort()
        before_insert.abort()
        for thread in threads:
            thread.join(timeout=15)
        drop_reflected_postgres_metadata(engine)
        engine.dispose()

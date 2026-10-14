import inspect
import os
import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.models.audit import AuditEvent
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.context_write import ReviewDecisionRequest
from backend.scripts.close_ghost_pending_reviews import (
    close_ghost_reviews,
    find_ghost_reviews,
)
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_write_service import ContextWriteService
from backend.services.credential_hashing import CredentialHasher
from backend.services.page_service import PageService
from backend.services.read_errors import ReadValidationError
from backend.services.review_service import ReviewService
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
        ContextWriteService.update_draft,
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
def test_postgres_reviewed_replacement_serializes_before_review_approval(
    monkeypatch,
    replacement_provenance,
):
    """The page -> review lock order prevents stale publish and deadlock."""

    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    create_isolated_postgres_metadata(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    browser_at_review_lookup = threading.Event()
    release_browser = threading.Event()
    browser_result = []
    browser_errors: list[BaseException] = []
    approval_errors: list[BaseException] = []
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
                session,
                user_id=user.id,
                workspace_id=workspace.id,
                role="owner",
            )
            workspace_id = workspace.id
            actor_id = str(user.id)

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
        reviewed = pages.autosave_page(
            page_id=published.id,
            workspace_id=workspace_id,
            title=published.title,
            body_md="# Page\n\nReviewed PAT bytes.\n",
            actor_kind="human",
            actor_id=actor_id,
            base_version_id=published.version_id,
            base_content_hash=published.content_hash,
            actor_provenance="personal_access_token",
            idempotency_key="postgres-reviewed-bytes",
        )
        assert reviewed.review_item_id is not None

        original_lookup = PageService._context_reviews_for_version

        def paused_lookup(*, session, page, version):
            result = original_lookup(session=session, page=page, version=version)
            browser_at_review_lookup.set()
            assert release_browser.wait(timeout=10)
            return result

        monkeypatch.setattr(
            PageService,
            "_context_reviews_for_version",
            staticmethod(paused_lookup),
        )

        def browser_replace() -> None:
            try:
                browser_result.append(
                    pages.autosave_page(
                        page_id=published.id,
                        workspace_id=workspace_id,
                        title=published.title,
                        body_md="# Page\n\nBrowser successor bytes.\n",
                        actor_kind="human",
                        actor_id=actor_id,
                        base_version_id=reviewed.version_id,
                        base_content_hash=reviewed.content_hash,
                        actor_provenance=replacement_provenance,
                        session_version_id=reviewed.version_id,
                        idempotency_key="postgres-review-race-browser-replace",
                    )
                )
            except BaseException as exc:  # asserted below
                browser_errors.append(exc)

        def approve_review() -> None:
            try:
                with session_factory.begin() as session:
                    ReviewService().apply_decision(
                        session=session,
                        workspace_id=workspace_id,
                        review_item_id=reviewed.review_item_id,
                        request=ReviewDecisionRequest(decision_type="approve"),
                        actor_kind="human",
                        actor_id=actor_id,
                        actor_role="owner",
                        actor_provenance="browser_session",
                    )
            except BaseException as exc:  # asserted below
                approval_errors.append(exc)

        browser_thread = threading.Thread(target=browser_replace)
        approval_thread = threading.Thread(target=approve_review)
        browser_thread.start()
        assert browser_at_review_lookup.wait(timeout=10)
        approval_thread.start()
        release_browser.set()
        browser_thread.join(timeout=10)
        approval_thread.join(timeout=10)

        assert not browser_thread.is_alive()
        assert not approval_thread.is_alive()
        assert browser_errors == []
        assert len(browser_result) == 1
        assert len(approval_errors) == 1
        assert isinstance(approval_errors[0], ReadValidationError)
        with session_factory() as session:
            old_version = session.get(ContextPageVersion, reviewed.version_id)
            review = session.get(ReviewItem, reviewed.review_item_id)
            assert old_version is not None
            assert old_version.body_md == "# Page\n\nReviewed PAT bytes.\n"
            assert review is not None and review.status == "superseded"
    finally:
        drop_reflected_postgres_metadata(engine)
        engine.dispose()


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires the dedicated PostgreSQL migration test database",
)
def test_postgres_ghost_close_and_decision_share_page_then_review_lock_order():
    """A decision holding the page cannot deadlock with ghost closure."""

    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    decision_page_locked = threading.Event()
    release_decision = threading.Event()
    close_lock_attempted = threading.Event()
    close_review_locked = threading.Event()

    class TrackingSession(Session):
        def scalar(self, statement, *args, **kwargs):
            descriptions = getattr(statement, "column_descriptions", ())
            entity = descriptions[0].get("entity") if descriptions else None
            locks_rows = getattr(statement, "_for_update_arg", None) is not None
            result = super().scalar(statement, *args, **kwargs)
            if (
                threading.current_thread().name == "ghost-review-decision"
                and locks_rows
                and entity is ContextPage
            ):
                decision_page_locked.set()
                assert release_decision.wait(timeout=10)
            return result

        def get(self, entity, ident, **kwargs):
            closing = threading.current_thread().name == "ghost-review-close"
            if closing and kwargs.get("with_for_update"):
                close_lock_attempted.set()
            result = super().get(entity, ident, **kwargs)
            if closing and kwargs.get("with_for_update") and entity is ReviewItem:
                close_review_locked.set()
            return result

    create_isolated_postgres_metadata(engine)
    session_factory = sessionmaker(
        bind=engine,
        class_=TrackingSession,
        expire_on_commit=False,
    )
    decision_results = []
    close_results = []
    thread_errors: list[BaseException] = []
    decision_thread = None
    close_thread = None
    try:
        with session_factory.begin() as session:
            workspace = seed_default_workspace(session)
            user = User(
                email="postgres-ghost-close-race@example.com",
                password_hash=CredentialHasher().hash_password("correct-password"),
                display_name="Ghost close race",
                status="active",
            )
            session.add(user)
            session.flush()
            grant_workspace_membership(
                session,
                user_id=user.id,
                workspace_id=workspace.id,
                role="owner",
            )
            workspace_id = workspace.id
            actor_id = str(user.id)

        pages = PageService(session_factory)
        published = pages.write_page(
            workspace_slug="default",
            slug="postgres-ghost-close-race",
            title="Ghost close race",
            body_md="# Page\n\nPublished.\n",
            actor_kind="human",
            actor_id=actor_id,
            actor_provenance="browser_session",
        )
        reviewed = pages.autosave_page(
            page_id=published.id,
            workspace_id=workspace_id,
            title=published.title,
            body_md="# Page\n\nReviewed PAT bytes.\n",
            actor_kind="human",
            actor_id=actor_id,
            base_version_id=published.version_id,
            base_content_hash=published.content_hash,
            actor_provenance="personal_access_token",
            idempotency_key="postgres-ghost-close-race",
        )
        assert reviewed.review_item_id is not None
        with session_factory.begin() as session:
            page = session.get(ContextPage, reviewed.id)
            assert page is not None
            page.latest_draft_version_id = None
        with session_factory() as session:
            discovered = find_ghost_reviews(session, workspace_id=workspace_id)
            assert [item.review_id for item in discovered] == [reviewed.review_item_id]

        def decide() -> None:
            try:
                with session_factory.begin() as session:
                    decision_results.append(
                        ReviewService().apply_decision(
                            session=session,
                            workspace_id=workspace_id,
                            review_item_id=reviewed.review_item_id,
                            request=ReviewDecisionRequest(
                                decision_type="request_changes",
                                decision_reason="Needs revision.",
                                audience_kind="workspace_editors",
                            ),
                            actor_kind="human",
                            actor_id=actor_id,
                            actor_role="owner",
                            actor_provenance="browser_session",
                        )
                    )
            except BaseException as exc:  # asserted below
                thread_errors.append(exc)

        def close() -> None:
            try:
                with session_factory.begin() as session:
                    close_results.append(
                        close_ghost_reviews(
                            session, workspace_id=workspace_id, apply=True
                        )
                    )
            except BaseException as exc:  # asserted below
                thread_errors.append(exc)

        decision_thread = threading.Thread(
            target=decide,
            name="ghost-review-decision",
        )
        close_thread = threading.Thread(
            target=close,
            name="ghost-review-close",
        )
        decision_thread.start()
        assert decision_page_locked.wait(timeout=10)
        close_thread.start()
        assert close_lock_attempted.wait(timeout=10)
        review_locked_before_page_release = close_review_locked.wait(timeout=1)
        release_decision.set()
        decision_thread.join(timeout=10)
        close_thread.join(timeout=10)

        assert review_locked_before_page_release is False
        assert not decision_thread.is_alive()
        assert not close_thread.is_alive()
        assert thread_errors == []
        assert len(decision_results) == 1
        assert decision_results[0].status == "changes_requested"
        assert close_results == [[]]
        with session_factory() as session:
            review = session.get(ReviewItem, reviewed.review_item_id)
            decisions = session.scalars(
                select(ReviewDecision).where(
                    ReviewDecision.review_item_id == reviewed.review_item_id
                )
            ).all()
            ghost_audits = session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "review.ghost_closed",
                    AuditEvent.target_id == reviewed.review_item_id,
                )
            ).all()
            assert review is not None and review.status == "changes_requested"
            assert [decision.decision_type for decision in decisions] == [
                "request_changes"
            ]
            assert ghost_audits == []
    finally:
        release_decision.set()
        for thread in (decision_thread, close_thread):
            if thread is not None:
                thread.join(timeout=10)
        drop_reflected_postgres_metadata(engine)
        engine.dispose()

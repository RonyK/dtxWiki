from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.base import Base
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.policy.auth import ActorContext
from backend.schemas.context_write import WriteDraftRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_draft_workflow import ContextTransactionFactory
from backend.services.context_write_service import ContextWriteService
from backend.services.domain_read import DomainReadService
from backend.services.page_service import PageService

ACTOR = "00000000-0000-0000-0000-000000000010"


def _editor_actor(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id=ACTOR,
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="editor",
        role="editor",
    )


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _write_draft(session, write_service, workspace_id):
    return write_service.write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title="Draft Page",
            page_type="overview",
            body_markdown="# Draft\n\nBody.",
            citations=[],
            reason="new draft",
        ),
        actor_kind="human",
        actor_id=ACTOR,
        actor_role="owner",
    )


def test_context_detail_surfaces_pending_review_for_draft():
    # F1: a page with an open review for its draft exposes pending_review +
    # pending_draft_version_id so the Context surface can offer inline approval.
    session_factory = make_session_factory()
    read_service = DomainReadService()
    write_service = ContextWriteService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        result = _write_draft(session, write_service, workspace.id)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=result.page_id,
            actor=_editor_actor(workspace.id),
            include_warnings=False,
        )
        assert detail.pending_review is not None
        assert detail.pending_review.review_item_id == result.review_item_id
        assert detail.pending_review.status in ("pending", "changes_requested")
        assert detail.pending_review.risk_level
        assert detail.pending_draft_version_id == result.draft_version_id
        # Internal existence checks explicitly skip review metadata as well.
        skipped = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=result.page_id,
            actor=_editor_actor(workspace.id),
            include_warnings=False,
            include_pending_review=False,
        )
        assert skipped.pending_review is None


def test_pending_review_metadata_does_not_lint_the_draft_again(monkeypatch):
    from backend.services import domain_read as read_module

    session_factory = make_session_factory()
    read_service = DomainReadService()
    write_service = ContextWriteService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        result = write_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Jumpy Headings",
                page_type="overview",
                body_markdown="# Title\n\n### Skipped a level\n\nBody.\n",
                citations=[],
                reason="draft with a heading jump",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
    real_lint = read_module.page_format_warnings_for_body
    linted_bodies = []

    def track_lint(*args, **kwargs):
        linted_bodies.append(args[3])
        return real_lint(*args, **kwargs)

    monkeypatch.setattr(read_module, "page_format_warnings_for_body", track_lint)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=result.page_id,
            actor=_editor_actor(workspace.id),
        )
        assert detail.pending_review is not None
        assert detail.pending_review.review_item_id == result.review_item_id
        assert detail.pending_draft_version_id == result.draft_version_id
        assert detail.pending_draft_warnings == []
        assert detail.warnings == []
        assert linted_bodies == []


def _write_unformatted(session, write_service, workspace_id):
    return write_service.write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title="Auto Fix Page",
            page_type="overview",
            body_markdown="# Title  \n\n* item\n\n\n\nend   \n",
            citations=[],
            reason="auto-fix draft",
        ),
        actor_kind="human",
        actor_id=ACTOR,
        actor_role="owner",
    )


def test_write_draft_normalizes_body_only_when_auto_fix_enabled():
    # F2c: with auto-fix on, the saved draft body is normalized (markers, trailing
    # whitespace, blank runs); with it off (default) the body is left as authored.
    session_factory = make_session_factory()
    write_service = ContextWriteService()
    with session_factory.begin() as session:
        seed_default_workspace(session)

    # off-case: default workspace (auto_fix off) leaves the body as authored.
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        result_off = _write_unformatted(session, write_service, workspace.id)
        body_off = session.get(ContextPageVersion, result_off.draft_version_id).body_md
        assert "* item" in body_off
        assert "- item" not in body_off

    # on-case: enabling auto-fix normalizes the saved draft body.
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace.auto_fix_enabled = True
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        result_on = write_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Auto Fix Page Two",
                page_type="overview",
                body_markdown="# Title  \n\n* item\n\n\n\nend   \n",
                citations=[],
                reason="auto-fix on",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        body_on = session.get(ContextPageVersion, result_on.draft_version_id).body_md
        assert "- item" in body_on
        assert "* item" not in body_on
        assert "end   " not in body_on


def _seed_noncanonical(session, write_service, workspace_id, title):
    # Auto-fix OFF -> the "*" list marker is stored non-canonically, so a later
    # resubmit is a GENUINE format-only change (normalizes "*" -> "-").
    return write_service.write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title=title,
            page_type="overview",
            body_markdown="# Title\n\n* item\n\nBody.\n",
            citations=[],
            reason="v1",
        ),
        actor_kind="human",
        actor_id=ACTOR,
        actor_role="owner",
    )


def test_format_only_save_keeps_the_same_draft_and_pending_request():
    session_factory = make_session_factory()
    write_service = ContextWriteService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        initial = _seed_noncanonical(session, write_service, workspace.id, "Formatting")
        workspace.auto_fix_enabled = True
        version = session.get(ContextPageVersion, initial.draft_version_id)
        saved = PageService(ContextTransactionFactory(session)).autosave_page(
            page_id=initial.page_id,
            workspace_id=workspace.id,
            title=None,
            body_md=version.body_md,
            actor_kind="human",
            actor_id=ACTOR,
            base_version_id=version.id,
            base_content_hash=version.content_hash,
            actor_provenance="browser_session",
            idempotency_key="format-the-draft",
            expected_editing_revision=version.editing_revision,
            version_id=version.id,
        )
        assert saved.version_id == initial.draft_version_id
        assert "- item" in version.body_md
        assert session.get(ReviewItem, initial.review_item_id).status == "pending"
        assert (
            session.get(ContextPage, initial.page_id).current_published_version_id
            is None
        )


def test_published_draft_does_not_surface_stale_pending_review():
    # P1 regression: once the page has no current draft (latest_draft_version_id
    # cleared on publish), an older draft's still-open review must NOT resurface
    # on the Context detail — otherwise a reviewer could approve a stale review
    # inline. The fix scopes pending_review to the current draft version.
    session_factory = make_session_factory()
    read_service = DomainReadService()
    write_service = ContextWriteService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        result = _write_draft(session, write_service, workspace.id)
    # Simulate the published end-state: the draft pointer is cleared while the
    # original review row lingers as pending.
    with session_factory.begin() as session:
        session.get(ContextPage, result.page_id).latest_draft_version_id = None
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=result.page_id,
            actor=_editor_actor(workspace.id),
        )
        assert detail.pending_review.review_item_id == result.review_item_id
        assert detail.pending_draft_version_id == result.draft_version_id
        # The retired pointer cannot hide the pending request.
        assert session.get(ReviewItem, result.review_item_id).status == "pending"

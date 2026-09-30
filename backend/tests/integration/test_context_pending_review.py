from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.base import Base
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.policy.auth import ActorContext
from backend.schemas.context_write import UpdateDraftRequest, WriteDraftRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_write_service import ContextWriteService
from backend.services.domain_read import DomainReadService

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
        )
        assert detail.pending_review is not None
        assert detail.pending_review.review_item_id == result.review_item_id
        assert detail.pending_review.status in ("pending", "changes_requested")
        assert detail.pending_review.risk_level
        assert detail.pending_draft_version_id == result.draft_version_id
        # the internal existence-check path skips the pending-review query
        skipped = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=result.page_id,
            actor=_editor_actor(workspace.id),
            include_warnings=False,
        )
        assert skipped.pending_review is None


def test_pending_draft_warnings_lint_the_draft_body():
    # Sweep-2: the F1 block carries the DRAFT's lint warnings so a reviewer sees
    # the draft's format issues before approving (not just the published body's).
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
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=result.page_id,
            actor=_editor_actor(workspace.id),
        )
        assert detail.pending_review is not None
        assert any("heading" in w.lower() for w in detail.pending_draft_warnings)


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


def test_format_only_auto_approve_genuine_and_gate_guards():
    # F3/DEC-8 + I3 + I13: a GENUINE pure-formatting edit on a non-canonical base
    # is auto-approved; a true no-op short-circuits; a caller-requested review is
    # NOT force-approved; and a real content change still requires review.
    session_factory = make_session_factory()
    write_service = ContextWriteService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        p1 = _seed_noncanonical(session, write_service, workspace.id, "P1")
        p2 = _seed_noncanonical(session, write_service, workspace.id, "P2")
        p1_hash = session.get(ContextPageVersion, p1.draft_version_id).content_hash
        p2_hash = session.get(ContextPageVersion, p2.draft_version_id).content_hash
    with session_factory.begin() as session:
        seed_default_workspace(session).auto_fix_enabled = True

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)

        # 1) genuine format-only: resubmit the raw "*" body -> normalized "-".
        fmt = write_service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=p1.page_id,
                base_version_id=p1.draft_version_id,
                base_content_hash=p1_hash,
                update_mode="full_rewrite",
                body_markdown="# Title\n\n* item\n\nBody.\n",
                citations=[],
                reason="reformat",
            ),
            actor_kind="human",
            actor_id=ACTOR,
        )
        assert fmt.review_item_id is not None
        assert fmt.draft_version_id != p1.draft_version_id  # a new version
        fmt_version = session.get(ContextPageVersion, fmt.draft_version_id)
        assert fmt.persisted_content_hash == fmt_version.content_hash
        assert fmt.persisted_character_count == len(fmt_version.body_md)
        assert fmt.outcome == "updated_draft"
        assert fmt.body_changed is True
        assert fmt.metadata_changed is False
        assert fmt.version_status == "draft"
        assert fmt.latest_draft_version_id == fmt.draft_version_id
        assert fmt.current_published_version_id is None
        assert fmt.review_status == "approved"
        assert "- item" in fmt_version.body_md
        assert "* item" not in fmt_version.body_md
        assert session.get(ReviewItem, fmt.review_item_id).status == "approved"
        assert session.get(ContextPage, p1.page_id).current_published_version_id is None

        # 2) a true no-op (canonical body byte-identical) short-circuits under
        #    Auto-fix: no new version, no change_set, no review (avoids spurious
        #    auto-approved churn). submit_revision_for_review handles the null
        #    ids explicitly (re-review r2 findings 2-7).
        noop = write_service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=p1.page_id,
                base_version_id=fmt.draft_version_id,
                base_content_hash=fmt_version.content_hash,
                update_mode="full_rewrite",
                body_markdown=fmt_version.body_md,
                citations=[],
                reason="no-op",
            ),
            actor_kind="human",
            actor_id=ACTOR,
        )
        assert noop.review_item_id is None
        assert noop.change_set_id is None
        assert noop.draft_version_id == fmt.draft_version_id
        assert noop.persisted_content_hash == fmt_version.content_hash
        assert noop.persisted_character_count == len(fmt_version.body_md)
        assert noop.outcome == "no_change"
        assert noop.body_changed is False
        assert noop.metadata_changed is False
        assert noop.version_status == "draft"
        assert noop.latest_draft_version_id == fmt.draft_version_id
        assert noop.current_published_version_id is None
        assert noop.review_status is None

        # 3) content change: NOT auto-approved.
        changed = write_service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=p1.page_id,
                base_version_id=fmt.draft_version_id,
                base_content_hash=fmt_version.content_hash,
                update_mode="full_rewrite",
                body_markdown="# Title\n\n- item\n\nDifferent body now.\n",
                citations=[],
                reason="content change",
            ),
            actor_kind="human",
            actor_id=ACTOR,
        )
        assert session.get(ReviewItem, changed.review_item_id).status == "pending"

        # 4) I3: format-only-eligible BUT the caller asked for review (a gate
        #    force signal) -> must NOT be auto-approved.
        guarded = write_service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=p2.page_id,
                base_version_id=p2.draft_version_id,
                base_content_hash=p2_hash,
                update_mode="full_rewrite",
                body_markdown="# Title\n\n* item\n\nBody.\n",
                citations=[],
                reason="reformat but please review",
                needs_review=True,
            ),
            actor_kind="human",
            actor_id=ACTOR,
        )
        assert session.get(ReviewItem, guarded.review_item_id).status == "pending"


def test_noop_with_needs_review_creates_a_pending_review():
    # re-review r3 #334: a no-op save (canonical body unchanged) that explicitly
    # sets needs_review=True must NOT be silently short-circuited — it creates a
    # pending review so the explicit request is honored.
    session_factory = make_session_factory()
    write_service = ContextWriteService()
    with session_factory.begin() as session:
        ws = seed_default_workspace(session)
        ws.auto_fix_enabled = True
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        created = write_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="NeedsReview Noop",
                page_type="overview",
                body_markdown="# Title\n\n- item\n\nBody.\n",
                citations=[],
                reason="v1",
            ),
            actor_kind="human",
            actor_id=ACTOR,
            actor_role="owner",
        )
        base = session.get(ContextPageVersion, created.draft_version_id)
        res = write_service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=created.draft_version_id,
                base_content_hash=base.content_hash,
                update_mode="full_rewrite",
                body_markdown="# Title\n\n- item\n\nBody.\n",  # identical, canonical
                citations=[],
                reason="please review",
                needs_review=True,
            ),
            actor_kind="human",
            actor_id=ACTOR,
        )
        assert res.review_item_id is not None
        assert res.draft_version_id == created.draft_version_id
        assert res.persisted_content_hash == base.content_hash
        assert res.persisted_character_count == len(base.body_md)
        assert res.outcome == "review_only"
        assert res.body_changed is False
        assert res.metadata_changed is False
        assert res.version_status == "draft"
        assert res.latest_draft_version_id == created.draft_version_id
        assert res.current_published_version_id is None
        assert res.review_status == "pending"
        assert session.get(ReviewItem, res.review_item_id).status == "pending"


def test_format_only_not_auto_approved_under_conservative_preset():
    # Re-review finding 1: a strict workspace (conservative preset, review_from
    # "low") reviews EVERYTHING — a format-only change must NOT be force-approved
    # there, even though it is auto-approved under the balanced default.
    from backend.services.review_risk_policy import _preset_policy

    session_factory = make_session_factory()
    write_service = ContextWriteService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = _seed_noncanonical(session, write_service, workspace.id, "Strict")
        base_hash = session.get(ContextPageVersion, page.draft_version_id).content_hash
    with session_factory.begin() as session:
        ws = seed_default_workspace(session)
        ws.auto_fix_enabled = True
        ws.review_policy = _preset_policy("conservative")
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        fmt = write_service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.page_id,
                base_version_id=page.draft_version_id,
                base_content_hash=base_hash,
                update_mode="full_rewrite",
                body_markdown="# Title\n\n* item\n\nBody.\n",
                citations=[],
                reason="reformat under conservative",
            ),
            actor_kind="human",
            actor_id=ACTOR,
        )
        assert session.get(ReviewItem, fmt.review_item_id).status == "pending"


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
        assert detail.pending_review is None
        assert detail.pending_draft_version_id is None
        # The gate hides it; the review row itself is untouched (still pending).
        assert session.get(ReviewItem, result.review_item_id).status == "pending"

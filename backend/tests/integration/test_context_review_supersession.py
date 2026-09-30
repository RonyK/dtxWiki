from uuid import UUID

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.models.audit import AuditEvent
from backend.models.base import Base, utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.policy.auth import ActorContext
from backend.schemas.context_write import UpdateDraftRequest, WriteDraftRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_write_service import ContextWriteService
from backend.services.domain_read import DomainReadService
from backend.services.page_service import PageService
from backend.services.review_service import ReviewService

ACTOR = "00000000-0000-0000-0000-000000000010"
CLAIMANT = UUID("00000000-0000-0000-0000-000000000099")


def _make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


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


def _update(session, service, workspace_id, page_id, base_id, body, reason):
    base = session.get(ContextPageVersion, base_id)
    return service.update_draft(
        session=session,
        workspace_id=workspace_id,
        request=UpdateDraftRequest(
            page_id=page_id,
            base_version_id=base.id,
            base_content_hash=base.content_hash,
            update_mode="full_rewrite",
            body_markdown=body,
            citations=[],
            reason=reason,
        ),
        actor_kind="human",
        actor_id=ACTOR,
        allow_changes_requested_revision=True,
    )


def test_update_draft_supersedes_only_prior_pending_context_reviews():
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
            write_service,
            workspace.id,
            created.page_id,
            created.draft_version_id,
            "# V2\n\nSecond body.",
            "v2",
        )
        third = _update(
            session,
            write_service,
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

        superseded = {r.id: r for r in context_reviews if r.status == "superseded"}
        assert set(superseded) == {created.review_item_id, second.review_item_id}
        for stale in superseded.values():
            assert stale.claimed_by is None
            assert stale.claimed_at is None
            assert session.get(ChangeSet, stale.change_set_id).status == "superseded"

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
        assert {
            (
                event.payload["original_review_item_id"],
                event.payload["new_review_item_id"],
            )
            for event in lineage
        } == {
            (str(created.review_item_id), str(second.review_item_id)),
            (str(second.review_item_id), str(third.review_item_id)),
        }

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


def _agent_draft_chain(session, workspace_id, title):
    service = ContextWriteService()
    created = service.write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title=title,
            page_type="overview",
            body_markdown="# V1\n\nAgent body one.",
            citations=[],
            reason="agent v1",
        ),
        actor_kind="agent",
        actor_id="agent-session",
        actor_role="owner",
    )
    second_base = session.get(ContextPageVersion, created.draft_version_id)
    second = service.update_draft(
        session=session,
        workspace_id=workspace_id,
        request=UpdateDraftRequest(
            page_id=created.page_id,
            base_version_id=second_base.id,
            base_content_hash=second_base.content_hash,
            update_mode="full_rewrite",
            body_markdown="# V2\n\nAgent body two.",
            citations=[],
            reason="agent v2",
        ),
        actor_kind="agent",
        actor_id="agent-session",
    )
    third_base = session.get(ContextPageVersion, second.draft_version_id)
    third = service.update_draft(
        session=session,
        workspace_id=workspace_id,
        request=UpdateDraftRequest(
            page_id=created.page_id,
            base_version_id=third_base.id,
            base_content_hash=third_base.content_hash,
            update_mode="full_rewrite",
            body_markdown="# V3\n\nAgent body three.",
            citations=[],
            reason="agent v3",
        ),
        actor_kind="agent",
        actor_id="agent-session",
    )
    return created, third


def test_rest_patch_publishes_but_autosave_stages_over_latest_mcp_draft():
    session_factory = _make_session_factory()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        patch_created, patch_third = _agent_draft_chain(
            session, workspace.id, "REST Patch Agent Draft"
        )
        autosave_created, autosave_third = _agent_draft_chain(
            session, workspace.id, "Autosave Agent Draft"
        )
        patch_base = session.get(ContextPageVersion, patch_third.draft_version_id)
        autosave_base = session.get(ContextPageVersion, autosave_third.draft_version_id)
        workspace_id = workspace.id

    page_service = PageService(session_factory)
    patched = page_service.update_page(
        page_id=patch_created.page_id,
        title="REST Patch Agent Draft",
        body_md="# Human PATCH\n\nPublished over the reviewed agent draft.",
        actor_kind="human",
        actor_id=ACTOR,
        base_version_id=patch_base.id,
        base_content_hash=patch_base.content_hash,
        actor_provenance="browser_session",
    )
    autosaved = page_service.autosave_page(
        page_id=autosave_created.page_id,
        workspace_id=workspace_id,
        title="Autosave Agent Draft",
        body_md="# Human autosave\n\nStaged without mutating the agent row.",
        actor_kind="human",
        actor_id=ACTOR,
        base_version_id=autosave_base.id,
        base_content_hash=autosave_base.content_hash,
        session_version_id=autosave_base.id,
        actor_provenance="browser_session",
        idempotency_key="rest-patch-vs-autosave",
    )
    assert patched.superseded_review_count == 1

    with session_factory() as session:
        patch_page = session.get(ContextPage, patch_created.page_id)
        autosave_page = session.get(ContextPage, autosave_created.page_id)
        original_agent_row = session.get(ContextPageVersion, autosave_base.id)
        assert patch_page.current_version_id == patched.version_id
        assert patch_page.current_published_version_id == patched.version_id
        assert patch_page.latest_draft_version_id is None
        assert autosave_page.current_version_id == autosaved.version_id
        assert autosave_page.current_published_version_id is None
        assert autosave_page.latest_draft_version_id == autosaved.version_id
        assert autosaved.version_id != autosave_base.id
        assert original_agent_row.version_status == "draft"
        assert session.get(
            ContextPageVersion, autosaved.version_id
        ).version_status == "draft"
        assert original_agent_row.actor_type == "agent"
        assert original_agent_row.body_md == autosave_base.body_md
        assert original_agent_row.content_hash == autosave_base.content_hash
        assert patch_page.workspace_id == workspace_id


def test_human_patch_supersedes_pending_agent_review_but_keeps_inline_review():
    session_factory = _make_session_factory()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        created, third = _agent_draft_chain(session, workspace.id, "Review Handoff")
        base = session.get(ContextPageVersion, third.draft_version_id)
        inline, inline_change_set = _add_page_review(
            session,
            workspace.id,
            created.page_id,
            "page_inline_comment",
            "pending",
        )
        pending_agent_review = session.get(ReviewItem, third.review_item_id)
        unrelated, unrelated_change_set = _add_page_review(
            session,
            workspace.id,
            created.page_id,
            "context_update",
            "pending",
            version_id=created.draft_version_id,
        )

    page_service = PageService(session_factory)
    patched = page_service.update_page(
        page_id=created.page_id,
        title="Review Handoff",
        body_md="# Human decision\n\nThis direct edit publishes.",
        actor_kind="human",
        actor_id=ACTOR,
        base_version_id=base.id,
        base_content_hash=base.content_hash,
        actor_provenance="browser_session",
    )

    with session_factory() as session:
        page = session.get(ContextPage, created.page_id)
        agent_review = session.get(ReviewItem, pending_agent_review.id)
        surviving_inline = session.get(ReviewItem, inline.id)
        surviving_unrelated = session.get(ReviewItem, unrelated.id)
        lineage = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == page.workspace_id,
                AuditEvent.action == "review.superseded_by_human_page_write",
            )
        )
        assert page.current_version_id == patched.version_id
        assert page.current_published_version_id == patched.version_id
        assert page.latest_draft_version_id is None
        assert agent_review.status == "superseded"
        assert session.get(ChangeSet, agent_review.change_set_id).status == "superseded"
        assert surviving_inline.status == "pending"
        assert session.get(ChangeSet, inline_change_set.id).status == "draft_created"
        assert surviving_unrelated.status == "pending"
        assert (
            session.get(ChangeSet, unrelated_change_set.id).status == "draft_created"
        )
        assert lineage.payload["original_review_item_id"] == str(agent_review.id)
        assert lineage.payload["new_change_set_id"]
        assert lineage.payload["new_review_item_id"]


def test_context_history_current_row_matches_restore_noop_pointer():
    session_factory = _make_session_factory()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        created, third = _agent_draft_chain(session, workspace.id, "Restore Pointer")
        page = session.get(ContextPage, created.page_id)
        assert page.latest_draft_version_id == third.draft_version_id
        assert page.current_version_id != page.latest_draft_version_id
        backend_current_id = page.current_version_id
        actor = ActorContext(
            actor_kind="human",
            actor_id=ACTOR,
            workspace_role="owner",
            role="owner",
            scopes=("*",),
            workspace_id=workspace.id,
        )
        history = DomainReadService().list_changes(
            session=session,
            workspace_id=workspace.id,
            target_type="context_page",
            target_id=page.id,
            actor=actor,
        )
        marked_current = [entry for entry in history.versions if entry.is_current]
        version_count = session.scalar(
            select(func.count(ContextPageVersion.id)).where(
                ContextPageVersion.page_id == page.id
            )
        )

    assert [entry.version_id for entry in marked_current] == [backend_current_id]
    restored = PageService(session_factory).restore_version(
        page_id=created.page_id,
        version_id=marked_current[0].version_id,
        actor_kind="human",
        actor_id=ACTOR,
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        after_count = session.scalar(
            select(func.count(ContextPageVersion.id)).where(
                ContextPageVersion.page_id == created.page_id
            )
        )
    assert restored.version_id == backend_current_id
    assert after_count == version_count


def test_restore_version_supersedes_pending_review_targeting_replaced_draft():
    """CTX-03: `restore_version` moves `current_version_id`/`latest_draft_
    version_id` the same way `write_page`/`update_page` do (a browser session
    keeps the two pointers together, unlike the MCP-only chain above), but
    unlike those two it never called `_supersede_unresolved_context_reviews_
    for_version`. A pending context_update review pinned to the staged draft
    it forks away from was left permanently pending: unapprovable
    (`_approval_supersession_context`'s pointer check requires `latest_draft_
    version_id` to still name the reviewed version, and restore_version just
    moved it elsewhere) and un-superseded, so it could neither be approved
    nor be cleared by the new edit the way every other write path clears a
    stale review.
    """

    session_factory = _make_session_factory()
    with session_factory.begin() as session:
        seed_default_workspace(session)
    page_service = PageService(session_factory)
    published = page_service.write_page(
        workspace_slug="default",
        slug="restore-supersession",
        title="Restore Supersession",
        body_md="# Restore Supersession\n\npublished body",
        actor_kind="human",
        actor_id=ACTOR,
        actor_provenance="browser_session",
    )
    staged = page_service.autosave_page(
        page_id=published.id,
        workspace_id=published.workspace_id,
        title=published.title,
        body_md=f"{published.body_md}\n\nstaged edit",
        actor_kind="human",
        actor_id=ACTOR,
        actor_provenance="browser_session",
        base_version_id=published.version_id,
        base_content_hash=published.content_hash,
        session_version_id=published.version_id,
        idempotency_key="restore-supersession-staged",
    )
    with session_factory.begin() as session:
        page = session.get(ContextPage, published.id)
        assert page.current_version_id == staged.version_id
        assert page.latest_draft_version_id == staged.version_id
        review, change_set = _add_page_review(
            session,
            page.workspace_id,
            published.id,
            "context_update",
            "pending",
            version_id=staged.version_id,
        )
        review_id, change_set_id = review.id, change_set.id

    restored = page_service.restore_version(
        page_id=published.id,
        version_id=published.version_id,
        actor_kind="human",
        actor_id=ACTOR,
        actor_provenance="browser_session",
    )

    assert restored.superseded_review_count == 1
    with session_factory() as session:
        review = session.get(ReviewItem, review_id)
        change_set = session.get(ChangeSet, change_set_id)
        page = session.get(ContextPage, published.id)
        assert review.status == "superseded"
        assert change_set.status == "superseded"
        assert page.latest_draft_version_id == restored.version_id

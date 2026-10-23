from importlib import import_module
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories import context_page_repo
from backend.schemas.context_write import (
    PublishContextPageRequest,
    ReviewDecisionRequest,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.services.context_lifecycle import (
    audit_context_history,
    project_context_version,
    repair_context_history,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.domain_read import DomainReadService
from backend.services.read_errors import StaleContextDraftError
from backend.services.review_service import ReviewService
from backend.tests.utils.prepared_body import prepare_context_body

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
ACTOR_UUID = UUID(ACTOR_ID)


def _workspace(session) -> Workspace:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None
    return workspace


def _actor(workspace_id: UUID, role: str) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=f"{role}-user",
        token_kind="session",
        scopes=("read", "audit-read"),
        workspace_id=workspace_id,
        workspace_role=role,
        role=role,
    )


def _published_page(page_service, *, slug: str, body: str = "v1"):
    title = slug.replace("-", " ").title()
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=f"# {title}\n\n{body}",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )


def _draft_from_published(page_service, published, *, suffix: str = "draft"):
    return page_service.autosave_page(
        page_id=published.id,
        workspace_id=published.workspace_id,
        title=published.title,
        body_md=f"{published.body_md}\n\n{suffix}",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=published.version_id,
        base_content_hash=published.content_hash,
        session_version_id=published.version_id,
        idempotency_key=f"draft-from-published-{uuid4()}",
    )


def _status_snapshot(session, page_id: UUID) -> tuple[tuple, tuple]:
    page = session.get(ContextPage, page_id)
    assert page is not None
    versions = session.scalars(
        select(ContextPageVersion)
        .where(ContextPageVersion.page_id == page.id)
        .order_by(ContextPageVersion.version_no)
    ).all()
    return (
        (
            page.current_version_id,
            page.current_published_version_id,
            page.latest_draft_version_id,
            page.lifecycle_status,
        ),
        tuple(
            (version.id, version.version_status, version.graph_status)
            for version in versions
        ),
    )


def test_direct_publication_supersedes_v1_and_normalizes_all_published_rows(
    page_service,
):
    v1 = _published_page(page_service, slug="direct-lifecycle")
    v2 = page_service.write_page(
        workspace_slug="default",
        slug="direct-lifecycle",
        title="Direct Lifecycle",
        body_md="# Direct Lifecycle\n\nv2",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=v1.version_id,
        base_content_hash=v1.content_hash,
    )

    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, v1.id)
        old = session.get(ContextPageVersion, v1.version_id)
        current = session.get(ContextPageVersion, v2.version_id)
        assert page is not None and old is not None and current is not None
        assert old.version_status == "superseded_snapshot"
        assert current.version_status == "published_snapshot"
        assert page.current_version_id == current.id
        assert page.current_published_version_id == current.id
        assert page.latest_draft_version_id is None

        # Simulate a legacy multi-published page.  The next successful direct
        # publish must normalize every old row, not only the pointer target.
        # A migrated database prevents this shape, so remove the invariant
        # briefly to reproduce a pre-migration row set.
        session.execute(text("DROP INDEX uq_cpv_current_published_per_page"))
        rogue = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=old.id,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md="# Direct Lifecycle\n\nrogue",
                actor_kind="human",
                actor_id=ACTOR_ID,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            version_status="published_snapshot",
        )
        rogue_id = rogue.id

    v3 = page_service.write_page(
        workspace_slug="default",
        slug="direct-lifecycle",
        title="Direct Lifecycle",
        body_md="# Direct Lifecycle\n\nv3",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=v2.version_id,
        base_content_hash=v2.content_hash,
    )
    with page_service.session_factory() as session:
        rows = {
            version.id: version
            for version in session.scalars(
                select(ContextPageVersion).where(ContextPageVersion.page_id == v1.id)
            )
        }
        page = session.get(ContextPage, v1.id)
        assert page is not None
        assert rows[v2.version_id].version_status == "superseded_snapshot"
        assert rows[rogue_id].version_status == "superseded_snapshot"
        assert rows[v3.version_id].version_status == "published_snapshot"
        session.execute(
            text(
                "CREATE UNIQUE INDEX uq_cpv_current_published_per_page "
                "ON context_page_version (page_id) "
                "WHERE version_status = 'published_snapshot'"
            )
        )
        assert page.current_published_version_id == v3.version_id


def test_core_schema_migration_normalizes_only_same_page_published_pointer_lineage(
    page_service,
):
    first = _published_page(page_service, slug="migration-lifecycle")
    current = page_service.write_page(
        workspace_slug="default",
        slug="migration-lifecycle",
        title="Migration Lifecycle",
        body_md="# Migration Lifecycle\n\ncurrent",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=first.version_id,
        base_content_hash=first.content_hash,
    )
    orphan = _published_page(page_service, slug="migration-orphan")
    migration = import_module(
        "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
    )

    with page_service.session_factory.begin() as session:
        old_row = session.get(ContextPageVersion, first.version_id)
        current_row = session.get(ContextPageVersion, current.version_id)
        orphan_page = session.get(ContextPage, orphan.id)
        orphan_row = session.get(ContextPageVersion, orphan.version_id)
        assert old_row and current_row and orphan_page and orphan_row
        session.execute(text("DROP INDEX uq_cpv_current_published_per_page"))
        old_row.version_status = "published_snapshot"
        old_row.graph_status = "branched"
        current_row.version_status = "superseded_snapshot"
        current_row.graph_status = "branched"
        current_page = session.get(ContextPage, current.id)
        assert current_page is not None
        current_page.latest_draft_version_id = current_row.id
        orphan_page.current_published_version_id = None
        orphan_page.current_version_id = None
        orphan_row.version_status = "published_snapshot"
        session.flush()

        counts = migration._context_history_preflight_counts(session.connection())
        assert counts == {
            "cross_page_or_missing_head": 0,
            "published_without_head": 1,
            "latest_draft_overlap_masks_live_draft": 0,
            # The orphan page above had its current pointer unset while it still
            # holds a version row (line 210). Before this class existed nothing
            # flagged it, and the upgrade proceeded over an unreachable head.
            "current_version_unset_with_versions": 1,
            "latest_draft_unset_with_live_draft": 0,
            "current_version_missing_or_cross_page": 0,
            "current_version_status_contradiction": 0,
            "latest_draft_missing_or_cross_page": 0,
            "latest_draft_status_contradiction": 0,
        }
        with pytest.raises(RuntimeError, match="published_without_head=1"):
            migration._assert_context_history_preflight(session.connection())

        migration._normalize_context_history(session.connection())
        session.expire_all()

        assert old_row.version_status == "superseded_snapshot"
        assert old_row.graph_status == "linear"
        assert current_row.version_status == "published_snapshot"
        assert current_row.graph_status == "linear"
        assert current_page.latest_draft_version_id is None
        # A missing pointer is intentionally diagnostic-only: migration must
        # not guess which version is canonical.
        assert orphan_row.version_status == "published_snapshot"


def test_migration_preserves_rejected_restartable_draft_after_unrelated_publish(
    page_service,
):
    """CTX-02 completion (post-3f40a6c7 adversarial-review finding).

    ``_demote_pre_publish_orphan_drafts``'s ``version_no`` heuristic cannot
    tell a true pre-fix orphan from a REJECTED-and-not-yet-restarted review
    draft: rejecting a review deliberately clears ``latest_draft_version_id``
    while leaving the rejected version ``draft``/``linear``
    (``review_service.py``'s ``decide_review``, so ``update_draft``'s
    ``restart_after_reject`` path can fork a fresh draft from it later) --
    exactly the pointer-unset-plus-linear-draft shape this function sweeps
    up. If ANY unrelated edit on the same page gets published before the
    rejected draft is restarted -- ordinary, frequent activity, nothing
    exotic -- the rejected draft's ``version_no`` falls behind the new head
    and becomes indistinguishable from true dead history by ``version_no``
    alone.

    Demoting it anyway would flip ``graph_status`` to ``discarded``. Restart
    would still nominally succeed (``_is_restartable_rejected_version`` only
    checks ``version_status``), but the discarded ancestor would then
    permanently refuse EVERY future publish built on it
    (``assert_context_publish_candidate`` walks the parent chain and
    requires every ancestor to stay ``linear``) -- silently and
    irreversibly breaking the documented reject-then-restart recovery path,
    not just this one publish attempt.

    Also proves a second, even more common failure this same gap caused:
    without the fix, the bare EXISTENCE of an unrestarted rejected draft --
    with no second publish needed at all -- already fails
    ``alembic upgrade head`` (``latest_draft_unset_with_live_draft``), since
    that preflight count has no ``version_no`` floor.
    """

    published = _published_page(
        page_service, slug="ctx02-reject-then-unrelated-publish"
    )
    write = ContextWriteService()
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=published.title,
                page_type="overview",
                body_markdown=f"{published.body_md}\n\nrejected proposal",
                reason="initial MCP draft",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
            actor_role="editor",
        )
        rejected_draft_id = created.draft_version_id
        rejected = ReviewService().apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="reject",
                decision_reason="not ready",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        assert rejected.status == "rejected"

    with page_service.session_factory() as session:
        page = session.get(ContextPage, published.id)
        rejected_row = session.get(ContextPageVersion, rejected_draft_id)
        assert page is not None and rejected_row is not None
        assert page.latest_draft_version_id is None
        assert rejected_row.version_status == "draft"
        assert rejected_row.graph_status == "linear"

    # Ordinary, UNRELATED later edit -- not a restart of the rejected draft
    # -- forks straight from the still-published head and gets published,
    # advancing version_no past the rejected draft.
    later = page_service.write_page(
        workspace_slug="default",
        slug="ctx02-reject-then-unrelated-publish",
        title=published.title,
        body_md=f"{published.body_md}\n\nunrelated later edit",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=published.version_id,
        base_content_hash=published.content_hash,
    )
    with page_service.session_factory() as session:
        rejected_row = session.get(ContextPageVersion, rejected_draft_id)
        later_row = session.get(ContextPageVersion, later.version_id)
        assert rejected_row is not None and later_row is not None
        assert rejected_row.version_no < later_row.version_no
        # Write-side CTX-01 fix correctly leaves it alone: the rejected
        # draft is not on the published chain's ancestor path.
        assert rejected_row.graph_status == "linear"

    migration = import_module(
        "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
    )
    with page_service.session_factory.begin() as session:
        connection = session.connection()
        counts_before = migration._context_history_preflight_counts(connection)
        # The bare existence of the unrestarted rejected draft must not
        # block the upgrade, with or without the unrelated publish above.
        assert counts_before["latest_draft_unset_with_live_draft"] == 0

        migration._demote_pre_publish_orphan_drafts(connection)
        migration._assert_context_history_preflight(connection)  # must not raise

    with page_service.session_factory() as session:
        rejected_row = session.get(ContextPageVersion, rejected_draft_id)
        assert rejected_row is not None
        # The decisive assertion: still `draft`/`linear`, not `discarded`.
        assert rejected_row.version_status == "draft"
        assert rejected_row.graph_status == "linear"

    # The submitter restarts the rejected draft with revised content -- the
    # documented recovery path (`update_draft`'s `restart_after_reject`).
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        rejected_row = session.get(ContextPageVersion, rejected_draft_id)
        restarted = write.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=published.id,
                base_version_id=rejected_draft_id,
                base_content_hash=rejected_row.content_hash,
                update_mode="full_rewrite",
                body_markdown=f"{published.body_md}\n\nrevised after rejection",
                reason="restart after rejection",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
        )
        restarted_draft_id = restarted.draft_version_id

    with page_service.session_factory() as session:
        restarted_row = session.get(ContextPageVersion, restarted_draft_id)
        assert restarted_row is not None
        assert restarted_row.parent_version_id == rejected_draft_id

    # `_restart_rejected_draft` always parents the new draft on the
    # rejected version itself, not the current published head, so when an
    # unrelated publish happened in between (as staged above) the restarted
    # draft is genuinely stale relative to the new head -- the same
    # ordinary, resolvable conflict any draft based on old content would
    # hit, and a SEPARATE, pre-existing property of `_restart_rejected_
    # draft` (it does not rebase), not something this fix touches. This is
    # NOT the corruption this test guards against: a wrongly-`discarded`
    # ancestor would refuse to publish no matter how the divergence was
    # resolved, whereas this failure is purely "my base (v1) is not the
    # current head (v3)" -- ordinary optimistic-concurrency staleness, the
    # same error `test_invalid_final_publish_is_stable_and_does_not_mutate`
    # exercises elsewhere in this file. The decisive proof already ran
    # above (`graph_status == "linear"` survived both the demotion call and
    # the restart); this closing assertion only confirms the test setup
    # itself reached a genuinely-stale-not-corrupted state, not a silent
    # overwrite of the unrelated edit.
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        with pytest.raises(StaleContextDraftError):
            write.publish_draft(
                session=session,
                workspace_id=workspace.id,
                page_id=published.id,
                request=PublishContextPageRequest(
                    version_id=restarted_draft_id,
                    reason="publish the restarted-after-rejection draft",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

    with page_service.session_factory() as session:
        page = session.get(ContextPage, published.id)
        assert page is not None
        # The failed publish attempt must not have mutated any pointer, and
        # the page must still audit clean -- the rejected draft's survival
        # is not, itself, a violation.
        assert page.current_published_version_id == later.version_id
        report = audit_context_history(session)
        assert report.violations == ()


def test_restart_after_reject_publishes_cleanly_with_no_intervening_publish(
    page_service,
):
    """Companion to the test above, isolating the ordinary case: reject then
    restart immediately, with nothing else published on the page in
    between. `_restart_rejected_draft` parents the new draft on the
    rejected version, whose own parent is still the current published head
    here (nothing moved it), so the restarted draft's ancestor chain
    reaches the head directly and publish succeeds -- proving the
    reject-then-restart recovery path works end to end, not merely that the
    row keeps a `linear` label.
    """

    published = _published_page(page_service, slug="ctx03-restart-then-publish")
    write = ContextWriteService()
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=published.title,
                page_type="overview",
                body_markdown=f"{published.body_md}\n\nrejected proposal",
                reason="initial MCP draft",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
            actor_role="editor",
        )
        rejected_draft_id = created.draft_version_id
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="reject",
                decision_reason="not ready",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor_provenance="browser_session",
        )

    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        rejected_row = session.get(ContextPageVersion, rejected_draft_id)
        restarted = write.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=published.id,
                base_version_id=rejected_draft_id,
                base_content_hash=rejected_row.content_hash,
                update_mode="full_rewrite",
                body_markdown=f"{published.body_md}\n\nrevised after rejection",
                reason="restart after rejection",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
        )
        restarted_draft_id = restarted.draft_version_id

        decided = ReviewService().apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=restarted.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="approve",
                decision_reason="looks good now",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        assert decided.published_version_id == restarted_draft_id

    with page_service.session_factory() as session:
        page = session.get(ContextPage, published.id)
        assert page is not None
        assert page.current_published_version_id == restarted_draft_id
        report = audit_context_history(session)
        assert report.violations == ()


def test_publish_demotes_a_reviewed_autosave_step_after_a_later_fork(page_service):
    """CTX-01, the common trigger: staged autosave (commit 60593953) means a
    page routinely accumulates a multi-step draft chain -- but not from plain
    repeated typing. A same-actor, unreviewed autosave draft is rewritten in
    place (`autosave_page`'s `same_actor_draft` branch), so no chain forms
    from that alone. The chain forms the moment a review attaches to an
    autosaved draft: the next autosave then MUST fork away from the reviewed
    row rather than mutate bytes a reviewer is looking at (`targeting_
    reviews` / `created_session_draft` in `autosave_page`), leaving the
    reviewed step behind as an ordinary `draft`/`linear` mid-chain ancestor.
    Publishing the tip must still demote it.
    """

    published = _published_page(page_service, slug="ctx01-reviewed-autosave-chain")
    step1 = page_service.autosave_page(
        page_id=published.id,
        workspace_id=published.workspace_id,
        title=published.title,
        body_md=f"{published.body_md}\n\nstep 1",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=published.version_id,
        base_content_hash=published.content_hash,
        session_version_id=published.version_id,
        idempotency_key=f"chain-step1-{uuid4()}",
    )
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, published.id)
        step1_row = session.get(ContextPageVersion, step1.version_id)
        assert page is not None and step1_row is not None
        # A review now targets step1 -- the exact condition that forces the
        # next autosave to fork instead of mutating it in place.
        change_set = ChangeSet(
            workspace_id=page.workspace_id,
            reason="pending review",
            trigger_type="test",
            actor_type="human",
            actor_id=ACTOR_UUID,
            status="draft_created",
            risk_level="low",
        )
        session.add(change_set)
        session.flush()
        session.add_all(
            [
                ChangeOperation(
                    workspace_id=change_set.workspace_id,
                    change_set_id=change_set.id,
                    target_type="context_page",
                    target_id=page.id,
                    operation="update",
                    diff_ref=f"context_page_version:{step1_row.id}",
                ),
                ReviewItem(
                    workspace_id=page.workspace_id,
                    change_set_id=change_set.id,
                    review_type="context_update",
                    status="pending",
                    risk_level="low",
                    decision_summary="pending",
                ),
            ]
        )

    step2 = page_service.autosave_page(
        page_id=published.id,
        workspace_id=published.workspace_id,
        title=published.title,
        body_md=f"{published.body_md}\n\nstep 2",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=step1.version_id,
        base_content_hash=step1.content_hash,
        session_version_id=step1.version_id,
        idempotency_key=f"chain-step2-{uuid4()}",
    )
    # The reviewed row forced a fork, not an in-place rewrite, and superseded
    # the review that made it happen.
    assert step2.version_id != step1.version_id
    assert step2.superseded_review_count == 1

    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, published.id)
        assert page is not None
        assert page.latest_draft_version_id == step2.version_id
        step1_row = session.get(ContextPageVersion, step1.version_id)
        assert step1_row is not None
        # Still the CTX-01 precondition: the fork left it `linear`.
        assert step1_row.graph_status == "linear"

        ContextWriteService().publish_draft(
            session=session,
            workspace_id=page.workspace_id,
            page_id=page.id,
            request=PublishContextPageRequest(
                version_id=step2.version_id,
                reason="publish the staged chain tip",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        session.flush()

        report = audit_context_history(session)
        assert report.violations == ()
        session.refresh(step1_row)
        assert step1_row.version_status == "draft"
        assert step1_row.graph_status == "discarded"
        assert page.latest_draft_version_id is None


def test_publish_demotes_superseded_linear_draft_ancestor(page_service):
    """CTX-01, a lower-level variant of the staged-autosave-chain test above:
    a fork leaves a parent draft `linear` while the child becomes `latest_
    draft_version_id` (a supported shape per `assert_context_publish_
    candidate`'s docstring). Publishing the child must demote the orphaned
    parent, or the audit reports `latest_draft_unset_with_live_draft` for
    every ordinary publish that follows a fork, and repair resurrects the
    parent as the edit head (context_lifecycle.py `_repair_context_head_
    pointers`).
    """

    published = _published_page(page_service, slug="ctx01-direct-fork")
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, published.id)
        assert page is not None
        v1 = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=published.version_id,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=f"{published.body_md}\n\nfork v1",
                actor_kind="human",
                actor_id=ACTOR_ID,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            version_status="draft",
        )
        session.flush()
        v2 = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=v1.id,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=f"{published.body_md}\n\nfork v2",
                actor_kind="human",
                actor_id=ACTOR_ID,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            version_status="draft",
        )
        session.flush()
        page.current_version_id = v2.id
        page.latest_draft_version_id = v2.id
        session.flush()
        v1_id, v2_id = v1.id, v2.id

        ContextWriteService().publish_draft(
            session=session,
            workspace_id=page.workspace_id,
            page_id=page.id,
            request=PublishContextPageRequest(
                version_id=v2_id,
                reason="publish the fork tip",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        session.flush()

        report = audit_context_history(session)
        assert report.violations == ()
        v1_row = session.get(ContextPageVersion, v1_id)
        v2_row = session.get(ContextPageVersion, v2_id)
        assert v1_row is not None and v2_row is not None
        assert v1_row.version_status == "draft"
        assert v1_row.graph_status == "discarded"
        assert v2_row.version_status == "published_snapshot"
        assert page.latest_draft_version_id is None


def test_mcp_revision_fork_publish_leaves_audit_clean(page_service):
    """CTX-01 trigger (b): a real MCP write_draft -> changes_requested ->
    update_draft -> approve loop. `update_draft` forks a new linear draft
    without demoting the one under review (context_write_service.py
    `_version_for_update`), so approving the revision must not leave the
    reviewed draft an audit-visible orphan.
    """

    write = ContextWriteService()
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="CTX-01 MCP revision loop",
                page_type="overview",
                body_markdown="# CTX-01 MCP revision loop\n\nv1",
                reason="initial MCP draft",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
            actor_role="editor",
        )
        requested_changes = ReviewService().apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="request_changes",
                decision_reason="needs another pass",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        assert requested_changes.status == "changes_requested"

        base = session.get(ContextPageVersion, created.draft_version_id)
        assert base is not None
        revised = write.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=base.id,
                base_content_hash=base.content_hash,
                update_mode="full_rewrite",
                body_markdown="# CTX-01 MCP revision loop\n\nv2 revised",
                reason="address the review",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
            allow_changes_requested_revision=True,
        )
        page = session.get(ContextPage, created.page_id)
        assert page is not None
        # The rewrite loop forks a new linear draft without demoting the
        # reviewed one -- exactly CTX-01's precondition.
        assert base.graph_status == "linear"
        assert page.latest_draft_version_id == revised.draft_version_id

        decided = ReviewService().apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=revised.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="approve",
                decision_reason="looks good now",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        assert decided.published_version_id == revised.draft_version_id
        session.flush()

        report = audit_context_history(session)
        assert report.violations == ()
        session.refresh(base)
        assert base.graph_status == "discarded"
        assert base.version_status == "draft"


def test_restore_version_over_unpublished_draft_then_publish_leaves_audit_clean(
    page_service,
):
    """CTX-01: `restore_version` correctly does NOT demote the draft it
    forks over (page_service.py `restore_version` inserts with
    `parent_version_id=current.id` and never touches `current`'s `graph_
    status`) -- doing so at restore time would break `assert_context_
    publish_candidate`'s own chain walk, which requires every ancestor
    between the draft tip and the published head to still be `draft`/
    `linear`. The staged draft stays a normal, valid mid-chain ancestor
    right up until publish, at which point the general publish-time walk in
    `apply_context_publication` must still demote it like any other
    superseded step in the chain -- this test exercises that general
    mechanism against a restore_version-created chain specifically, not a
    restore_version-specific fix.
    """

    published = _published_page(page_service, slug="ctx01-restore-over-draft")
    staged = _draft_from_published(page_service, published, suffix="staged draft")
    assert staged.version_status == "draft"

    restored = page_service.restore_version(
        page_id=published.id,
        version_id=published.version_id,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )

    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, published.id)
        assert page is not None
        assert page.latest_draft_version_id == restored.version_id
        staged_row = session.get(ContextPageVersion, staged.version_id)
        assert staged_row is not None
        # Still the CTX-01 precondition: restore_version left it `linear`.
        assert staged_row.graph_status == "linear"

        ContextWriteService().publish_draft(
            session=session,
            workspace_id=page.workspace_id,
            page_id=page.id,
            request=PublishContextPageRequest(
                version_id=restored.version_id,
                reason="publish the restored version",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        session.flush()

        report = audit_context_history(session)
        assert report.violations == ()
        session.refresh(staged_row)
        assert staged_row.graph_status == "discarded"


def test_repair_does_not_resurrect_a_demoted_fork_ancestor(page_service):
    """Reverse guard for CTX-01: once publication demotes a superseded fork
    ancestor, `repair_context_history` must find nothing to repair on that
    page.

    Before the fix, the ancestor stayed `linear`, so `_proven_linear_draft`
    treated it as the page's sole linear draft and `_repair_context_head_
    pointers` restored `latest_draft_version_id` to it -- a version whose
    parent chain points at the OLD published head, not the new one, and can
    never publish again (`assert_context_publish_candidate` walks parent
    links until it reaches `page.current_published_version_id`).
    """

    published = _published_page(page_service, slug="ctx01-repair-guard")
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, published.id)
        assert page is not None
        v1 = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=published.version_id,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=f"{published.body_md}\n\nfork v1",
                actor_kind="human",
                actor_id=ACTOR_ID,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            version_status="draft",
        )
        session.flush()
        v2 = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=v1.id,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=f"{published.body_md}\n\nfork v2",
                actor_kind="human",
                actor_id=ACTOR_ID,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            version_status="draft",
        )
        session.flush()
        page.current_version_id = v2.id
        page.latest_draft_version_id = v2.id
        session.flush()
        v1_id = v1.id

        ContextWriteService().publish_draft(
            session=session,
            workspace_id=page.workspace_id,
            page_id=page.id,
            request=PublishContextPageRequest(
                version_id=v2.id,
                reason="publish the fork tip",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        session.flush()

    with page_service.session_factory.begin() as session:
        result = repair_context_history(session)
        page = session.get(ContextPage, published.id)
        assert page is not None
        assert result.repaired_pages == 0
        # The demoted ancestor's parent chain still points at the OLD
        # published head -- restoring it here would name an edit head that
        # can never publish again.
        assert page.latest_draft_version_id is None
        v1_row = session.get(ContextPageVersion, v1_id)
        assert v1_row is not None
        assert v1_row.graph_status == "discarded"


def test_history_audit_accepts_supported_current_and_draft_pointer_shapes(
    page_service,
):
    published = _published_page(page_service, slug="audit-published-current")
    current_draft_base = _published_page(
        page_service,
        slug="audit-current-draft",
    )
    current_draft = _draft_from_published(page_service, current_draft_base)
    coexist = _published_page(page_service, slug="audit-published-with-draft")

    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        coexist_draft = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=coexist.title,
                page_type="overview",
                body_markdown=f"{coexist.body_md}\n\ncoexisting draft",
                reason="keep the published current pointer while drafting",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        published_page = session.get(ContextPage, published.id)
        current_draft_page = session.get(ContextPage, current_draft_base.id)
        coexist_page = session.get(ContextPage, coexist.id)
        assert published_page is not None
        assert current_draft_page is not None
        assert coexist_page is not None
        assert published_page.current_version_id == published.version_id
        assert current_draft_page.current_version_id == current_draft.version_id
        assert current_draft_page.latest_draft_version_id == current_draft.version_id
        assert coexist_page.current_version_id == coexist.version_id
        assert coexist_page.current_published_version_id == coexist.version_id
        assert coexist_page.latest_draft_version_id == coexist_draft.draft_version_id

        report = audit_context_history(session)
        migration_counts = import_module(
            "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
        )._context_history_preflight_counts(session.connection())

    assert report.violations == ()
    assert all(count == 0 for count in migration_counts.values())


def test_history_audit_reports_edit_head_pointer_integrity_violations(page_service):
    anchor = _published_page(page_service, slug="audit-pointer-anchor")
    missing_current = _published_page(page_service, slug="audit-current-missing")
    cross_current = _published_page(page_service, slug="audit-current-cross")
    current_v1 = _published_page(page_service, slug="audit-current-status")
    current_v2 = page_service.write_page(
        workspace_slug="default",
        slug="audit-current-status",
        title="Audit Current Status",
        body_md="# Audit Current Status\n\nv2",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=current_v1.version_id,
        base_content_hash=current_v1.content_hash,
    )
    missing_latest = _published_page(page_service, slug="audit-latest-missing")
    cross_latest = _published_page(page_service, slug="audit-latest-cross")
    latest_v1 = _published_page(page_service, slug="audit-latest-status")
    latest_v2 = page_service.write_page(
        workspace_slug="default",
        slug="audit-latest-status",
        title="Audit Latest Status",
        body_md="# Audit Latest Status\n\nv2",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=latest_v1.version_id,
        base_content_hash=latest_v1.content_hash,
    )

    with page_service.session_factory.begin() as session:
        missing_current_page = session.get(ContextPage, missing_current.id)
        cross_current_page = session.get(ContextPage, cross_current.id)
        current_status_page = session.get(ContextPage, current_v2.id)
        missing_latest_page = session.get(ContextPage, missing_latest.id)
        cross_latest_page = session.get(ContextPage, cross_latest.id)
        latest_status_page = session.get(ContextPage, latest_v2.id)
        assert missing_current_page is not None
        assert cross_current_page is not None
        assert current_status_page is not None
        assert missing_latest_page is not None
        assert cross_latest_page is not None
        assert latest_status_page is not None

        missing_current_page.current_version_id = uuid4()
        cross_current_page.current_version_id = anchor.version_id
        current_status_page.current_version_id = current_v1.version_id
        missing_latest_page.latest_draft_version_id = uuid4()
        cross_latest_page.latest_draft_version_id = anchor.version_id
        latest_status_page.latest_draft_version_id = latest_v1.version_id
        session.flush()

        report = audit_context_history(session)
        migration = import_module(
            "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
        )
        migration_counts = migration._context_history_preflight_counts(
            session.connection()
        )
        with pytest.raises(
            RuntimeError,
            match="current_version_missing_or_cross_page=2",
        ):
            migration._assert_context_history_preflight(session.connection())

    # Repairability is per code, not blanket. A cross-page pointer is never
    # repairable because no stored pointer proves which page owns the version.
    # Same-page breakage is repairable here for two different reasons: the
    # published head proves the current pointer, and these pages hold no linear
    # draft row at all, which proves the draft pointer belongs empty. A page
    # that does hold a live draft is covered by the coexist regressions in
    # tests/cli/test_context_history_repair.py.
    assert len(report.violations) == 6
    assert {
        violation.code: violation.repairable for violation in report.violations
    } == {
        "current_version_missing": True,
        "current_version_cross_page": False,
        "current_version_status_contradiction": True,
        "latest_draft_missing": True,
        "latest_draft_cross_page": False,
        "latest_draft_status_contradiction": True,
    }
    assert migration_counts == {
        "cross_page_or_missing_head": 0,
        "published_without_head": 0,
        "latest_draft_overlap_masks_live_draft": 0,
        "current_version_unset_with_versions": 0,
        "latest_draft_unset_with_live_draft": 0,
        "current_version_missing_or_cross_page": 2,
        "current_version_status_contradiction": 1,
        "latest_draft_missing_or_cross_page": 2,
        "latest_draft_status_contradiction": 1,
    }


def test_review_approval_and_context_write_share_publication_transition(page_service):
    v1 = _published_page(page_service, slug="review-lifecycle")
    write = ContextWriteService()
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        draft = write.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Review Lifecycle",
                page_type="overview",
                body_markdown="# Review Lifecycle\n\nreviewed v2",
                reason="MCP-authored revision",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
            actor_role="editor",
        )
        decided = ReviewService().apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=draft.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="approve",
                decision_reason="approved for publication",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        assert decided.published_version_id == draft.draft_version_id

    with page_service.session_factory() as session:
        page = session.get(ContextPage, v1.id)
        previous = session.get(ContextPageVersion, v1.version_id)
        published = session.get(ContextPageVersion, draft.draft_version_id)
        assert page is not None and previous is not None and published is not None
        assert previous.version_status == "superseded_snapshot"
        assert published.version_status == "published_snapshot"
        assert page.current_published_version_id == published.id
        assert page.current_version_id == published.id
        assert page.latest_draft_version_id is None


@pytest.mark.parametrize(
    "stale_shape",
    ["non_latest", "non_linear", "branched", "discarded", "unapproved"],
)
def test_invalid_final_publish_is_stable_and_does_not_mutate(
    page_service,
    stale_shape: str,
):
    published = _published_page(
        page_service,
        slug=f"stale-{stale_shape.replace('_', '-')}",
    )
    draft = _draft_from_published(page_service, published)

    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, published.id)
        version = session.get(ContextPageVersion, draft.version_id)
        assert page is not None and version is not None
        target_id = version.id
        if stale_shape == "non_latest":
            later = context_page_repo.insert_version(
                session,
                page_id=page.id,
                parent_version_id=version.id,
                body=prepare_context_body(
                    session,
                    page_id=page.id,
                    body_md=version.body_md + "\n\nlater",
                    actor_kind="human",
                    actor_id=ACTOR_ID,
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                version_status="draft",
            )
            page.latest_draft_version_id = later.id
        elif stale_shape == "non_linear":
            version.parent_version_id = None
        elif stale_shape in {"branched", "discarded"}:
            version.graph_status = stale_shape
        else:
            change_set = ChangeSet(
                workspace_id=page.workspace_id,
                reason="pending review",
                trigger_type="test",
                actor_type="human",
                actor_id=ACTOR_UUID,
                status="draft_created",
                risk_level="low",
            )
            session.add(change_set)
            session.flush()
            session.add_all(
                [
                    ChangeOperation(
                        workspace_id=change_set.workspace_id,
                        change_set_id=change_set.id,
                        target_type="context_page",
                        target_id=page.id,
                        operation="update",
                        diff_ref=f"context_page_version:{version.id}",
                    ),
                    ReviewItem(
                        workspace_id=page.workspace_id,
                        change_set_id=change_set.id,
                        review_type="context_update",
                        status="pending",
                        risk_level="low",
                        decision_summary="pending",
                    ),
                ]
            )
        session.flush()
        before = _status_snapshot(session, page.id)

        with pytest.raises(StaleContextDraftError) as excinfo:
            ContextWriteService().publish_draft(
                session=session,
                workspace_id=page.workspace_id,
                page_id=page.id,
                request=PublishContextPageRequest(
                    version_id=target_id,
                    reason="must not publish",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        assert excinfo.value.error_code == "stale_context_draft"
        assert _status_snapshot(session, page.id) == before


def test_projector_precedence_covers_all_history_states():
    page_id = uuid4()
    published_id = uuid4()
    draft_id = uuid4()
    page = ContextPage(
        id=page_id,
        workspace_id=uuid4(),
        slug="projection",
        title="Projection",
        page_type="overview",
        tags=[],
        aliases=[],
        lifecycle_status="published",
        current_version_id=draft_id,
        current_published_version_id=published_id,
        latest_draft_version_id=draft_id,
    )

    def version(
        version_id: UUID,
        *,
        status: str,
        graph: str,
    ) -> ContextPageVersion:
        return ContextPageVersion(
            id=version_id,
            workspace_id=page.workspace_id,
            page_id=page_id,
            version_no=1,
            body_md="# body",
            content_hash="c" * 64,
            graph_status=graph,
            actor_type="human",
            version_status=status,
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
        )

    cases = [
        (draft_id, "draft", "linear", "current_draft"),
        (published_id, "published_snapshot", "linear", "published_snapshot"),
        (uuid4(), "draft", "branched", "alternative_draft"),
        (uuid4(), "draft", "superseded", "superseded_draft"),
        (uuid4(), "draft", "discarded", "discarded_draft"),
        (uuid4(), "superseded_snapshot", "linear", "superseded_snapshot"),
        # A legacy extra published row projects as superseded until publication
        # normalizes its raw status.
        (uuid4(), "published_snapshot", "linear", "superseded_snapshot"),
        (uuid4(), "draft", "linear", "superseded_draft"),
    ]
    for version_id, status, graph, expected in cases:
        projected = project_context_version(
            page,
            version(version_id, status=status, graph=graph),
            visible_version_id=draft_id,
            expose_draft_state=True,
        )
        assert projected.history_status == expected

    page.latest_draft_version_id = published_id
    inconsistent = project_context_version(
        page,
        version(published_id, status="published_snapshot", graph="linear"),
        visible_version_id=published_id,
        expose_draft_state=True,
    )
    assert inconsistent.history_status == "state_inconsistent"


def test_context_detail_history_and_mcp_changes_hide_draft_metadata_from_viewer(
    page_service,
):
    published = _published_page(page_service, slug="viewer-history")
    draft = _draft_from_published(page_service, published)
    with page_service.session_factory() as session:
        page = session.get(ContextPage, published.id)
        assert page is not None
        workspace_id = page.workspace_id
        viewer = _actor(workspace_id, "viewer")
        editor = _actor(workspace_id, "editor")

        viewer_detail = DomainReadService().read_context_page(
            session=session,
            workspace_id=workspace_id,
            page_id=page.id,
            actor=viewer,
        )
        editor_detail = DomainReadService().read_context_page(
            session=session,
            workspace_id=workspace_id,
            page_id=page.id,
            actor=editor,
        )
        viewer_changes = DomainReadService().list_changes(
            session=session,
            workspace_id=workspace_id,
            target_type="context_page",
            target_id=page.id,
            actor=viewer,
        )
        editor_changes = DomainReadService().list_changes(
            session=session,
            workspace_id=workspace_id,
            target_type="context_page",
            target_id=page.id,
            actor=editor,
        )

    assert viewer_detail.current_version_id == published.version_id
    assert viewer_detail.latest_draft_version_id is None
    assert viewer_detail.version_info is not None
    assert viewer_detail.version_info.history_status == "published_snapshot"
    assert viewer_detail.version_info.is_visible_head is True
    assert viewer_detail.version_info.is_published_head is True
    assert viewer_detail.version_info.is_draft_head is False
    assert viewer_detail.published_version_info is not None
    assert viewer_detail.published_version_info.version_id == published.version_id
    assert viewer_detail.published_version_info.version_no == 1

    assert editor_detail.latest_draft_version_id == draft.version_id
    assert editor_detail.version_info is not None
    assert editor_detail.version_info.history_status == "current_draft"
    assert editor_detail.version_info.is_draft_head is True
    assert editor_detail.published_version_info is not None
    assert editor_detail.published_version_info.version_id == published.version_id

    assert [row.version_id for row in viewer_changes.versions] == [published.version_id]
    viewer_wire = viewer_changes.model_dump(mode="json")["versions"][0]
    assert viewer_wire["history_status"] == "published_snapshot"
    assert viewer_wire["graph_status"] == "linear"
    assert viewer_wire["is_visible_head"] is True
    assert viewer_wire["is_published_head"] is True
    assert viewer_wire["is_draft_head"] is False
    assert viewer_wire["is_alternative"] is False
    assert viewer_wire["is_discarded"] is False
    assert viewer_wire["prior_relation"] == "root"
    assert viewer_wire["prior_version_id"] is None
    assert {row.version_id for row in editor_changes.versions} == {
        published.version_id,
        draft.version_id,
    }
    draft_entry = next(
        row for row in editor_changes.versions if row.version_id == draft.version_id
    )
    assert draft_entry.history_status == "current_draft"
    assert draft_entry.is_visible_head is True
    assert draft_entry.is_draft_head is True
    assert draft_entry.prior_relation == "visible"
    assert draft_entry.prior_version_id == published.version_id

    viewer_page_versions = page_service.list_versions(
        published.id,
        actor=_actor(workspace_id, "viewer"),
    )
    assert [row.version_id for row in viewer_page_versions] == [published.version_id]
    assert viewer_page_versions[0].history_status == "published_snapshot"
    assert viewer_page_versions[0].is_visible_head is True
    assert viewer_page_versions[0].is_draft_head is False

    editor_page_versions = page_service.list_versions(
        published.id,
        actor=_actor(workspace_id, "editor"),
    )
    editor_version_by_id = {row.version_id: row for row in editor_page_versions}
    assert editor_version_by_id[draft.version_id].history_status == "current_draft"
    assert editor_version_by_id[draft.version_id].is_visible_head is True
    assert editor_version_by_id[draft.version_id].is_draft_head is True
    assert (
        editor_version_by_id[published.version_id].history_status
        == "published_snapshot"
    )
    assert editor_version_by_id[published.version_id].is_visible_head is False
    assert editor_version_by_id[published.version_id].is_published_head is True


def test_context_history_redacts_an_unreadable_parent_id_from_viewer(page_service):
    published = _published_page(page_service, slug="hidden-history-parent")
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, published.id)
        first = session.get(ContextPageVersion, published.version_id)
        assert page is not None and first is not None
        workspace_id = page.workspace_id

        first.version_status = "superseded_snapshot"
        session.flush()
        hidden_parent = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id,
            parent_version_id=first.id,
            version_no=2,
            body_md="# Hidden History Parent\n\nConfidential draft.",
            content_hash="2" * 64,
            graph_status="linear",
            actor_type="human",
            actor_display_name="Draft editor",
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
        )
        session.add(hidden_parent)
        session.flush()
        visible_head = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id,
            parent_version_id=hidden_parent.id,
            version_no=3,
            body_md="# Hidden History Parent\n\nPublished result.",
            content_hash="3" * 64,
            graph_status="linear",
            actor_type="human",
            actor_display_name="Publishing editor",
            version_status="published_snapshot",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
        )
        session.add(visible_head)
        session.flush()
        page.current_version_id = visible_head.id
        page.current_published_version_id = visible_head.id
        page.latest_draft_version_id = None
        hidden_parent_id = hidden_parent.id
        visible_head_id = visible_head.id

    with page_service.session_factory() as session:
        read_service = DomainReadService()
        viewer = _actor(workspace_id, "viewer")
        editor = _actor(workspace_id, "editor")
        viewer_detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace_id,
            page_id=published.id,
            actor=viewer,
        )
        viewer_changes = read_service.list_changes(
            session=session,
            workspace_id=workspace_id,
            target_type="context_page",
            target_id=published.id,
            actor=viewer,
        )
        editor_changes = read_service.list_changes(
            session=session,
            workspace_id=workspace_id,
            target_type="context_page",
            target_id=published.id,
            actor=editor,
        )

    assert viewer_detail.version_info is not None
    assert viewer_detail.version_info.version_id == visible_head_id
    assert viewer_detail.version_info.parent_version_id is None
    viewer_page_detail = page_service.read_page(
        page_id=published.id,
        actor=viewer,
    )
    viewer_page_versions = page_service.list_versions(
        published.id,
        actor=viewer,
    )
    editor_page_detail = page_service.read_page(
        page_id=published.id,
        actor=editor,
    )
    editor_page_versions = page_service.list_versions(
        published.id,
        actor=editor,
    )
    assert viewer_page_detail.parent_version_id is None
    viewer_legacy_head = next(
        row for row in viewer_page_versions if row.version_id == visible_head_id
    )
    assert viewer_legacy_head.parent_version_id is None
    assert str(hidden_parent_id) not in viewer_page_detail.model_dump_json()
    assert str(hidden_parent_id) not in viewer_page_versions[0].model_dump_json()
    assert editor_page_detail.parent_version_id == hidden_parent_id
    editor_legacy_head = next(
        row for row in editor_page_versions if row.version_id == visible_head_id
    )
    assert editor_legacy_head.parent_version_id == hidden_parent_id
    viewer_head = next(
        row for row in viewer_changes.versions if row.version_id == visible_head_id
    )
    assert viewer_head.prior_relation == "hidden"
    assert viewer_head.parent_version_id is None
    assert viewer_head.prior_version_id is None
    assert str(hidden_parent_id) not in viewer_changes.model_dump_json()

    editor_head = next(
        row for row in editor_changes.versions if row.version_id == visible_head_id
    )
    assert editor_head.prior_relation == "visible"
    assert editor_head.parent_version_id == hidden_parent_id
    assert editor_head.prior_version_id == hidden_parent_id


def test_an_open_edit_as_draft_head_is_normal_but_a_broken_one_is_not():
    """Every in-place save points latest_draft_version_id at the author's open
    editing row, so that is not an inconsistency. An editing row that is not
    linear still is, and says why."""

    from types import SimpleNamespace
    from uuid import uuid4

    page_id = uuid4()
    editing_id = uuid4()
    page = SimpleNamespace(
        id=page_id,
        current_published_version_id=None,
        latest_draft_version_id=editing_id,
    )

    def version(graph_status):
        return SimpleNamespace(
            id=editing_id,
            page_id=page_id,
            version_status="editing",
            graph_status=graph_status,
        )

    normal = project_context_version(
        page, version("linear"), visible_version_id=editing_id, expose_draft_state=True
    )
    assert normal.history_status == "current_editing"
    assert normal.inconsistency_reasons == ()

    broken = project_context_version(
        page,
        version("branched"),
        visible_version_id=editing_id,
        expose_draft_state=True,
    )
    assert broken.history_status == "state_inconsistent"
    assert "editing_row_not_linear" in broken.inconsistency_reasons
    assert "draft_head_not_a_linear_draft" in broken.inconsistency_reasons

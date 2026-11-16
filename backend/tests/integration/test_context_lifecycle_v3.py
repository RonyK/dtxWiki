from importlib import import_module
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories import context_page_repo
from backend.schemas.context_write import (
    ReviewDecisionRequest,
    WriteDraftRequest,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.domain_read import DomainReadService
from backend.services.review_service import ReviewService
from backend.tests.utils.context_git import seed_published_snapshot
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
    draft = page_service.begin_context_edit(
        page_id=published.id,
        workspace_id=published.workspace_id,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        idempotency_key=f"begin-{uuid4()}",
        from_version_id=published.version_id,
    )
    return page_service.autosave_page(
        page_id=published.id,
        workspace_id=published.workspace_id,
        title=published.title,
        body_md=f"{published.body_md}\n\n{suffix}",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=draft.version_id,
        base_content_hash=draft.content_hash,
        expected_editing_revision=draft.editing_revision,
        session_version_id=draft.version_id,
        idempotency_key=f"save-{uuid4()}",
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
            parent_version_id=current.id,
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
        seed_published_snapshot(session, rogue, parents=(current.commit_oid,))
        session.info["context_version_writer"].align_head(
            page.workspace_id, page.id, current.commit_oid
        )

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


# "branched" and "discarded" share one guard (graph_status != "linear").


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

    assert editor_detail.open_drafts.total == 1
    assert editor_detail.latest_draft_version_id == draft.version_id
    assert editor_detail.version_info is not None
    assert editor_detail.version_info.history_status == "published_snapshot"
    assert editor_detail.version_info.is_draft_head is False
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
    assert draft_entry.is_visible_head is False
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
    assert editor_version_by_id[draft.version_id].is_visible_head is False
    assert editor_version_by_id[draft.version_id].is_draft_head is True
    assert (
        editor_version_by_id[published.version_id].history_status
        == "published_snapshot"
    )
    assert editor_version_by_id[published.version_id].is_visible_head is True
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
            version_status="closed",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
        )
        session.add(hidden_parent)
        session.flush()
        seed_published_snapshot(session, hidden_parent, parents=(first.commit_oid,))
        hidden_parent.body_md = None
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

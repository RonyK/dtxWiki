from uuid import UUID

import pytest
from sqlalchemy import select

from backend.api.context_pages import status_code_for_error
from backend.api.deps import status_code_for_read_error
from backend.app.container import DatabaseProvider
from backend.mcp.envelope import error_envelope
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.context_lifecycle import repair_context_history
from backend.services.context_media_legacy_cutover import (
    LegacyMediaCutoverConflictError,
)
from backend.services.llms_txt import render_scoped_llms_txt
from backend.services.page_service import PageContextMediaError
from backend.services.tree_service import TreeService, ensure_primary_tree_placement

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
LEGACY_FILENAME = f"{'a' * 64}.png"


def _workspace(session) -> Workspace:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None
    return workspace


def _owner(workspace_id: UUID) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=ACTOR_ID,
        token_kind="session",
        scopes=(),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )


def _published(page_service, *, slug: str):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=slug.replace("-", " ").title(),
        body_md=f"# {slug}\n\nClean body.",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )


def _legacy_url(workspace_id: UUID) -> str:
    return f"/api/v1/uploads/{workspace_id}/{LEGACY_FILENAME}"


def test_publish_conflict_is_409_and_rolls_back_every_publication_pointer(
    page_service,
):
    published = _published(page_service, slug="cutover-publish")
    draft = page_service.autosave_page(
        page_id=published.id,
        workspace_id=published.workspace_id,
        title=published.title,
        body_md=f"{published.body_md}\n\nDraft.",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=published.version_id,
        base_content_hash=published.content_hash,
        session_version_id=published.version_id,
        idempotency_key="cutover-publish-draft",
    )
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        version = session.get(ContextPageVersion, draft.version_id)
        assert version is not None
        version.body_md = f"# Cutover publish\n\n![legacy]({_legacy_url(workspace.id)})"

    with pytest.raises(PageContextMediaError) as excinfo:
        page_service.publish_draft(
            page_id=published.id,
            version_id=draft.version_id,
            reason="must remain blocked after cutover",
            publish_despite_findings=False,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )

    assert excinfo.value.error_code == "legacy_media_cutover_conflict"
    assert excinfo.value.page_id == published.id
    assert status_code_for_error(excinfo.value) == 409
    with page_service.session_factory() as session:
        page = session.get(ContextPage, published.id)
        old = session.get(ContextPageVersion, published.version_id)
        rejected = session.get(ContextPageVersion, draft.version_id)
        assert page is not None and old is not None and rejected is not None
        assert page.current_published_version_id == old.id
        assert page.current_version_id == rejected.id
        assert page.latest_draft_version_id == rejected.id
        assert old.version_status == "published_snapshot"
        assert rejected.version_status == "draft"


def test_read_service_and_mcp_mapping_keep_cutover_conflict_nonretryable(
    session_factory,
):
    error = LegacyMediaCutoverConflictError()
    assert status_code_for_read_error(error) == 409

    with session_factory() as session:
        actor = _owner(_workspace(session).id)
    envelope = error_envelope(actor, error.error_code, message=str(error))
    assert envelope.result_status == "conflict"
    assert envelope.retryable is False


def test_pointer_repair_refuses_to_restore_a_legacy_visible_head(page_service):
    published = _published(page_service, slug="cutover-pointer-repair")
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        page = session.get(ContextPage, published.id)
        version = session.get(ContextPageVersion, published.version_id)
        assert page is not None and version is not None
        version.body_md = (
            f"# Cutover pointer repair\n\n![legacy]({_legacy_url(workspace.id)})"
        )
        page.current_version_id = None
        page.current_published_version_id = None

    with pytest.raises(LegacyMediaCutoverConflictError):
        with page_service.session_factory.begin() as session:
            repair_context_history(session)

    with page_service.session_factory() as session:
        page = session.get(ContextPage, published.id)
        assert page is not None
        assert page.current_version_id is None
        assert page.current_published_version_id is None


def test_archived_revival_refuses_legacy_heads_before_mutating_tree(page_service):
    published = _published(page_service, slug="cutover-revive")
    tree = TreeService()
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == published.id,
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
        assert node is not None
        tree.archive_node(
            session=session,
            workspace_id=workspace.id,
            node_id=node.id,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_owner(workspace.id),
        )
        node_id = node.id

    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        version = session.get(ContextPageVersion, published.version_id)
        assert version is not None
        version.body_md = f"# Cutover revive\n\n![legacy]({_legacy_url(workspace.id)})"

    with pytest.raises(LegacyMediaCutoverConflictError):
        with page_service.session_factory.begin() as session:
            workspace = _workspace(session)
            ensure_primary_tree_placement(
                session=session,
                workspace_id=workspace.id,
                target_kind="context_page",
                target_id=published.id,
                title="Cutover Revive",
                slug="cutover-revive",
                section="context",
                parent_id=None,
                default_parent_slug="inbox",
                actor_kind="human",
                actor_id=ACTOR_ID,
                can_have_children=True,
            )

    with page_service.session_factory() as session:
        node = session.get(WorkspaceTreeNode, node_id)
        page = session.get(ContextPage, published.id)
        assert node is not None and page is not None
        assert node.visibility_status == "archived"
        assert page.lifecycle_status == "archived"


def test_public_exposure_refuses_legacy_snapshot_before_mutating_grant(page_service):
    published = _published(page_service, slug="cutover-public-exposure")
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        version = session.get(ContextPageVersion, published.version_id)
        assert version is not None
        version.body_md = (
            f"# Cutover public exposure\n\n![legacy]({_legacy_url(workspace.id)})"
        )

    with pytest.raises(PageContextMediaError) as excinfo:
        page_service.set_page_public(
            published.id,
            True,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
            acknowledge_public_exposure=True,
            expected_published_version_id=published.version_id,
        )

    assert excinfo.value.error_code == "legacy_media_cutover_conflict"
    assert excinfo.value.page_id == published.id
    assert status_code_for_error(excinfo.value) == 409
    with page_service.session_factory() as session:
        page = session.get(ContextPage, published.id)
        assert page is not None
        assert page.is_public is False
        assert page.public_exposure_scope is None
        assert page.public_exposed_version_id is None


def test_llms_txt_excludes_cutover_legacy_snapshot_without_hiding_clean_pages(
    page_service,
):
    legacy = _published(page_service, slug="cutover-llms-legacy")
    clean = _published(page_service, slug="cutover-llms-clean")
    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.visibility = "public"

    for page in (legacy, clean):
        page_service.set_page_public(
            page.id,
            True,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
            acknowledge_public_exposure=True,
            expected_published_version_id=page.version_id,
        )

    with page_service.session_factory.begin() as session:
        workspace = _workspace(session)
        version = session.get(ContextPageVersion, legacy.version_id)
        assert version is not None
        version.body_md = f"![legacy]({_legacy_url(workspace.id)})"

    # render_scoped_llms_txt takes a DatabaseProvider (it opens a control
    # session to resolve Workspace, then a workspace session for
    # ContextPage/ContextPageVersion). This is a single-DB test, so both
    # sessions resolve to the same factory/engine.
    db = DatabaseProvider(None, session_factory=page_service.session_factory)
    rendered = render_scoped_llms_txt(
        db,
        workspace_slug="default",
        base_url="https://public.example",
    )

    assert rendered is not None
    assert "cutover-llms-clean" in rendered
    assert "cutover-llms-legacy" not in rendered
    assert "/api/v1/uploads/" not in rendered

    page_service.read_public_page("default", "cutover-llms-clean")

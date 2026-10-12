"""Delegated tasks, workspace bundles and public read surfaces."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.container import DatabaseProvider
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.workspace import Workspace
from backend.services.delegated_tasks import (
    IllegalTaskTransitionError,
    create_task,
    pending_summary,
    produce_folder_summary_tasks,
    transition_task,
)
from backend.services.indexing import Fts5Indexer, reindex_workspace
from backend.services.okf import render_workspace_bundle_markdown
from backend.services.public_page_exposure import grant_public_page_exposure
from backend.services.workspace_read import WorkspaceReadService
from backend.tests.conftest import authenticate_test_client

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


# --- M0 -----------------------------------------------------------------------


def test_create_task_dedup_matrix(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        target = uuid4()

        def make():
            return create_task(
                session,
                workspace_id=workspace_id,
                task_type="page_meta_refresh",
                target_kind="context_page",
                target_id=target,
                source="publish",
                fingerprint="v1",
            )

        first = make()
        assert first is not None
        # pending blocks
        assert make() is None
        # dismissed blocks (suppression persists)
        transition_task(session, first, "dismissed")
        assert make() is None
        # staged allows re-creation
        first.status = "answered"
        transition_task(session, first, "staged")
        assert make() is not None
        # different fingerprint is a fresh task regardless
        assert (
            create_task(
                session,
                workspace_id=workspace_id,
                task_type="page_meta_refresh",
                target_kind="context_page",
                target_id=target,
                source="publish",
                fingerprint="v2",
            )
            is not None
        )


def test_illegal_transition_raises(session_factory):
    with session_factory.begin() as session:
        task = create_task(
            session,
            workspace_id=_workspace_id(session),
            task_type="folder_nav_summary",
            target_kind="workspace_tree_node",
            target_id=uuid4(),
            source="lint",
            fingerprint="missing_nav_summary",
        )
        with pytest.raises(IllegalTaskTransitionError):
            transition_task(session, task, "staged")  # pending -> staged


def test_publish_hook_produces_page_meta_refresh(page_service, session_factory):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="m0-publish",
        title="M0 Publish",
        body_md="# M0\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    # REST direct write auto-publishes without publish_draft; the Q-2 hook
    # lives on the MCP publish path — exercise it end to end.
    from backend.schemas.context_write import (
        PublishContextPageRequest,
        ReviewDecisionRequest,
        WriteDraftRequest,
    )
    from backend.services.context_write_service import ContextWriteService
    from backend.services.review_service import ReviewService

    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="M0 MCP Publish",
                page_type="concept",
                body_markdown="# M0 MCP\n\nBody.",
                reason="m0 test",
            ),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
        )
        if created.review_item_id is not None:
            ReviewService().apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=created.review_item_id,
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id="tester",
                actor_role="owner",
                actor_provenance="browser_session",
            )
        write.publish_draft(
            session=session,
            workspace_id=workspace_id,
            page_id=created.page_id,
            request=PublishContextPageRequest(
                version_id=created.draft_version_id, reason="publish m0"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        tasks = session.scalars(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.target_id == created.page_id
            )
        ).all()
        assert len(tasks) == 1
        assert tasks[0].task_type == "page_meta_refresh"
        assert tasks[0].fingerprint == str(created.draft_version_id)
        assert pending_summary(session, workspace_id)["page_meta_refresh"] >= 1

    del payload


def test_folder_summary_gap_producer_at_reindex(session_factory, page_service):
    from backend.models.tree import WorkspaceTreeNode
    from backend.schemas.tree import TreeFolderCreateRequest
    from backend.services.tree_service import TreeService, create_tree_node

    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "context",
            )
        )
        big = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Big Folder"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        small = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Small Folder"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        for i in range(12):
            create_tree_node(
                session,
                workspace_id=workspace_id,
                section="context",
                node_kind="folder",
                parent_id=big.id,
                title=f"Child {i}",
                slug=f"gap-child-{i}",
                actor_kind="human",
                actor_id="tester",
            )
        big_id, small_id = big.id, small.id

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = produce_folder_summary_tasks(session, workspace_id)
        assert created == 1
        task = session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.target_id == big_id
            )
        )
        assert task is not None and task.task_type == "folder_nav_summary"
        assert (
            session.scalar(
                select(ClientDelegatedTask).where(
                    ClientDelegatedTask.target_id == small_id
                )
            )
            is None
        )
        # dismissed is not recreated while the gap persists
        transition_task(session, task, "dismissed")
        assert produce_folder_summary_tasks(session, workspace_id) == 0
    # reindex runs the producer and stays idempotent
    provider = DatabaseProvider(None, session_factory=session_factory)
    counts = reindex_workspace(provider, Fts5Indexer(provider), workspace_id)
    assert counts["delegated_tasks"] == 0


# --- CAG bundle -----------------------------------------------------------------


def test_bundle_hash_changes_on_publish(page_service, session_factory):
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        _, hash_before, _ = render_workspace_bundle_markdown(
            session, workspace_id, actor=None, workspace_slug="default"
        )
        _, hash_again, _ = render_workspace_bundle_markdown(
            session, workspace_id, actor=None, workspace_slug="default"
        )
    assert hash_before == hash_again  # stable without changes

    page_service.write_page(
        workspace_slug="default",
        slug="bundle-invalidate",
        title="Bundle Invalidate",
        body_md="# BI\n\nNew content.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        markdown, hash_after, _ = render_workspace_bundle_markdown(
            session, workspace_id, actor=None, workspace_slug="default"
        )
    assert hash_after != hash_before  # the CAG invalidation signal
    assert "<!-- dtxwiki:file context/bundle-invalidate.md -->" in markdown


# --- guide routing (E-3) ---------------------------------------------------------


def test_guide_emits_scale_aware_routing_line(session_factory, monkeypatch):
    service = WorkspaceReadService()
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        payload = service.guide(session, workspace_id)
        flow = payload.recommended_agent_guidance_flow
        assert any("bundle" in line and "what_changed" in line for line in flow)
        bundle_uri = f"dtxwiki://workspaces/{workspace_id}/bundle"
        assert any(bundle_uri in line for line in flow)

    import backend.services.workspace_read as wr

    monkeypatch.setattr(wr, "CAG_BUNDLE_PAGE_LIMIT", -1)
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        payload = service.guide(session, workspace_id)
        flow = payload.recommended_agent_guidance_flow
        assert any("descend get_index" in line for line in flow)
    assert "get_index" in payload.available_tools
    assert "get_index" in wr.AVAILABLE_READ_TOOLS
    assert "read_concept" in wr.AVAILABLE_READ_TOOLS
    assert "what_changed" in wr.AVAILABLE_READ_TOOLS


# --- /llms.txt --------------------------------------------------------------------


def _llms_client(page_service):
    return TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )


def test_llms_txt_serves_single_public_workspace(page_service, session_factory):
    from backend.models.context import ContextPage

    payload = page_service.write_page(
        workspace_slug="default",
        slug="llms-public",
        title="LLMS Public",
        body_md="# LP\n\nPublic body.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    page_service.write_page(
        workspace_slug="default",
        slug="llms-private",
        title="LLMS Private",
        body_md="# LPr\n\nPrivate body.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    client = _llms_client(page_service)
    # zero public workspaces -> 404
    assert client.get("/llms.txt").status_code == 404

    with session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        workspace.visibility = "public"
        page = session.get(ContextPage, payload.id)
        grant_public_page_exposure(
            page,
            scope="indexed_web",
            version_id=page.current_published_version_id,
        )

    response = client.get("/llms.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "X-Robots-Tag" not in response.headers
    body = response.text
    assert "- [LP](" in body
    assert "LLMS Public" not in body
    assert "/api/v1/public/w/default/pages/llms-public" in body
    # non-public and draft pages never leak
    assert "LLMS Private" not in body


def test_llms_txt_404_when_multiple_public_workspaces(
    page_service, session_factory
):
    with session_factory.begin() as session:
        session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).visibility = "public"
        second = Workspace(
            slug="second-public",
            title="Second",
            name="Second",
            visibility="public",
        )
        session.add(second)
    client = _llms_client(page_service)
    response = client.get("/llms.txt")
    assert response.status_code == 404
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert response.headers["Cache-Control"] == "no-store"


# --- index preview endpoint (C-6) ---------------------------------------------------


def test_index_preview_endpoint_renders_node(page_service, session_factory):
    from backend.models.tree import WorkspaceTreeNode
    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership
    from backend.schemas.tree import TreeFolderCreateRequest
    from backend.services.tree_service import TreeService

    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "context",
            )
        )
        folder = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Preview Folder"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        folder_id = folder.id

    client = _llms_client(page_service)
    authenticate_test_client(client, email="preview@example.com")
    with session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        user = session.scalar(
            select(User).where(User.email == "preview@example.com")
        )
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="viewer",
        )
        workspace_id = workspace.id
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes/{folder_id}/index-preview"
    )
    assert response.status_code == 200, response.text
    markdown = response.json()["data"]["markdown"]
    assert "(node: /context/preview-folder)" in markdown

    missing = client.get(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes/{uuid4()}/index-preview"
    )
    assert missing.status_code == 404

def test_llms_txt_percent_encodes_korean_page_slug(page_service, session_factory):
    from backend.models.context import ContextPage

    payload = page_service.write_page(
        workspace_slug="default",
        slug="한국어-페이지",
        title="한국어 페이지",
        body_md="# 한국어 페이지",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        workspace.visibility = "public"
        page = session.get(ContextPage, payload.id)
        grant_public_page_exposure(
            page,
            scope="indexed_web",
            version_id=page.current_published_version_id,
        )

    body = _llms_client(page_service).get("/llms.txt").text

    assert (
        "/pages/%ED%95%9C%EA%B5%AD%EC%96%B4-%ED%8E%98%EC%9D%B4%EC%A7%80"
        in body
    )

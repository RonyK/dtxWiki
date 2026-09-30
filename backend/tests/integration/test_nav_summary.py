"""nav_summary column + PATCH authoring (Sprint-3 C1-T3)."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.tree import TreeFolderCreateRequest, TreeNodeUpdate
from backend.services.read_errors import RejectedByPolicyError
from backend.services.tree_service import TreeService

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _context_root(session, workspace_id):
    return session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.section == "context",
            WorkspaceTreeNode.slug == "context",
        )
    )


def test_patch_sets_returns_and_clears_nav_summary(session_factory):
    service = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        folder = service.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Payments"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        updated = service.update_node(
            session=session,
            workspace_id=workspace_id,
            node_id=folder.id,
            request=TreeNodeUpdate(
                display_name="Payments",
                nav_summary="결제 승인·정산·환불. 정책은 /policy 참조.",
            ),
            actor_kind="human",
            actor_id="tester",
        )
        assert updated.nav_summary == "결제 승인·정산·환불. 정책은 /policy 참조."

        cleared = service.update_node(
            session=session,
            workspace_id=workspace_id,
            node_id=folder.id,
            request=TreeNodeUpdate(display_name="Payments", nav_summary=None),
            actor_kind="human",
            actor_id="tester",
        )
        assert cleared.nav_summary is None

        # Omitted field (not in model_fields_set) must NOT clear the value.
        service.update_node(
            session=session,
            workspace_id=workspace_id,
            node_id=folder.id,
            request=TreeNodeUpdate(
                display_name="Payments", nav_summary="다시 저장"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        kept = service.update_node(
            session=session,
            workspace_id=workspace_id,
            node_id=folder.id,
            request=TreeNodeUpdate(display_name="Payments renamed"),
            actor_kind="human",
            actor_id="tester",
        )
        assert kept.nav_summary == "다시 저장"


def test_patch_rejects_nav_summary_on_non_folder(session_factory, page_service):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="nav-summary-page",
        title="Nav Summary Page",
        body_md="# Page\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    service = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == payload.id,
            )
        )
        with pytest.raises(RejectedByPolicyError):
            service.update_node(
                session=session,
                workspace_id=workspace_id,
                node_id=node.id,
                request=TreeNodeUpdate(
                    display_name="Nav Summary Page", nav_summary="불가"
                ),
                actor_kind="human",
                actor_id="tester",
            )


def test_db_check_rejects_nav_summary_on_non_folder(session_factory, page_service):
    """The CHECK is live in SQLite: a raw write bypassing the service fails."""

    payload = page_service.write_page(
        workspace_slug="default",
        slug="nav-summary-raw",
        title="Nav Summary Raw",
        body_md="# Page\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            node = session.scalar(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.target_kind == "context_page",
                    WorkspaceTreeNode.target_id == payload.id,
                )
            )
            node.nav_summary = "raw write"
            session.flush()


def test_nav_summary_length_validation():
    with pytest.raises(ValueError):
        TreeNodeUpdate(display_name="X", nav_summary="가" * 161)

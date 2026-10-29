from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.ask import AskSession
from backend.models.tree import TreePolicy, WorkspaceTreeNode


def test_ask_sessions_create_a_missing_ask_root_once(page_service):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": "personal-ask@example.com", "password": "owner-password-123"},
    )
    assert signup.status_code == 201, signup.text
    workspace_id = UUID(
        client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    )
    # Starter workspaces created by 0.1.4 and earlier have no Ask root or policy;
    # the first Ask session must create them, and later sessions reuse them.
    with page_service.session_factory.begin() as session:
        session.execute(
            delete(TreePolicy).where(
                TreePolicy.workspace_id == workspace_id, TreePolicy.section == "ask"
            )
        )
        session.execute(
            delete(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "ask",
            )
        )

    for title in ("First Personal Ask", "Second Personal Ask"):
        created = client.post(
            f"/api/v1/workspaces/{workspace_id}/ask/sessions",
            json={"title": title},
        )
        assert created.status_code == 201, created.text

    with page_service.session_factory() as session:
        roots = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "ask",
                WorkspaceTreeNode.parent_id.is_(None),
                WorkspaceTreeNode.slug == "ask",
            )
        ).all()
        assert len(roots) == 1
        root = roots[0]
        assert root.is_system_node is True
        policy = session.get(TreePolicy, (workspace_id, "ask"))
        assert policy is not None
        assert policy.allow_user_folder_create is False
        assert policy.allow_page_as_parent is False
        assert policy.fixed_root_node_ids == [str(root.id)]
        assert session.scalar(
            select(func.count())
            .select_from(AskSession)
            .where(AskSession.workspace_id == workspace_id)
        ) == 2

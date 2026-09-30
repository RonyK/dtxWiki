from uuid import UUID

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.policy.auth import ActorContext, get_rest_actor
from backend.policy.rbac import grant_workspace_membership
from backend.repositories.workspace_repo import insert_workspace
from backend.tests.conftest import authenticate_test_client


def make_client(page_service) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(client)
    return client


def add_workspace_for_current_user(client: TestClient, page_service, slug: str):
    user_id = UUID(client.get("/api/v1/auth/me").json()["data"]["user_id"])
    with page_service.session_factory.begin() as session:
        workspace = insert_workspace(session, slug=slug, title=slug.title())
        grant_workspace_membership(
            session,
            user_id=user_id,
            workspace_id=workspace.id,
            role="owner",
        )
        return workspace.id


def test_user_default_workspace_is_selected(page_service):
    client = make_client(page_service)
    beta_id = add_workspace_for_current_user(client, page_service, "beta")
    user_id = UUID(client.get("/api/v1/auth/me").json()["data"]["user_id"])

    with page_service.session_factory.begin() as session:
        user = session.get(User, user_id)
        user.default_workspace_id = beta_id

    response = client.get("/api/v1/workspaces")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["current_workspace_id"] == str(beta_id)
    assert payload["default_workspace_id"] == str(beta_id)


def test_user_fallback_prefers_live_personal_workspace(page_service):
    client = make_client(page_service)
    add_workspace_for_current_user(client, page_service, "alpha")
    user_id = UUID(client.get("/api/v1/auth/me").json()["data"]["user_id"])
    with page_service.session_factory.begin() as session:
        user = session.get(User, user_id)
        assert user is not None
        personal_id = user.personal_workspace_id
        assert personal_id is not None
        user.default_workspace_id = None
    # Bypass the normal request-auth repair so this assertion specifically
    # exercises the GET /workspaces transaction and WorkspaceReadService.
    client.app.dependency_overrides[get_rest_actor] = lambda: ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        workspace_id=personal_id,
        workspace_role="owner",
        role="owner",
        token_kind="session",
        provenance="browser_session",
    )
    response = client.get("/api/v1/workspaces")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["current_workspace_id"] == str(personal_id)
    assert payload["default_workspace_id"] == str(personal_id)
    with page_service.session_factory() as session:
        persisted_user = session.get(User, user_id)
        assert persisted_user is not None
        assert persisted_user.default_workspace_id == personal_id


def test_current_workspace_falls_back_to_sole_accessible_workspace(page_service):
    client = make_client(page_service)
    user_id = UUID(client.get("/api/v1/auth/me").json()["data"]["user_id"])
    with page_service.session_factory.begin() as session:
        user = session.get(User, user_id)
        assert user is not None
        personal_id = user.personal_workspace_id
        assert personal_id is not None
        personal_membership = session.get(
            WorkspaceMembership,
            (user_id, personal_id),
        )
        assert personal_membership is not None
        session.delete(personal_membership)
        user.personal_workspace_id = None
        user.default_workspace_id = None
    only_id = add_workspace_for_current_user(client, page_service, "only-accessible")
    response = client.get("/api/v1/workspaces")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["current_workspace_id"] == str(only_id)
    assert payload["default_workspace_id"] is None


def test_multiple_memberships_without_live_anchor_require_picker(page_service):
    client = make_client(page_service)
    add_workspace_for_current_user(client, page_service, "picker-target")
    user_id = UUID(client.get("/api/v1/auth/me").json()["data"]["user_id"])
    with page_service.session_factory.begin() as session:
        user = session.get(User, user_id)
        assert user is not None
        user.default_workspace_id = None
        user.personal_workspace_id = None

    response = client.get("/api/v1/workspaces")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert len(payload["workspaces"]) == 2
    assert payload["current_workspace_id"] is None
    assert payload["default_workspace_id"] is None


def test_zero_membership_without_anchor_enters_recovery_onboarding(page_service):
    client = make_client(page_service)
    user_id = UUID(client.get("/api/v1/auth/me").json()["data"]["user_id"])
    with page_service.session_factory.begin() as session:
        user = session.get(User, user_id)
        assert user is not None
        personal_id = user.personal_workspace_id
        assert personal_id is not None
        membership = session.get(WorkspaceMembership, (user_id, personal_id))
        assert membership is not None
        session.delete(membership)
        user.default_workspace_id = None
        user.personal_workspace_id = None

    response = client.get("/api/v1/workspaces")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["workspaces"] == []
    assert payload["current_workspace_id"] is None
    assert payload["default_workspace_id"] is None


def test_set_default_workspace_requires_membership(page_service):
    client = make_client(page_service)
    with page_service.session_factory.begin() as session:
        foreign = insert_workspace(session, slug="foreign", title="Foreign")

    response = client.patch(
        "/api/v1/users/me/default-workspace",
        json={"workspace_id": str(foreign.id)},
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "workspace_membership_missing"


def test_set_default_workspace_updates_login_landing_workspace(page_service):
    client = make_client(page_service)
    research_id = add_workspace_for_current_user(client, page_service, "research")

    update = client.patch(
        "/api/v1/users/me/default-workspace",
        json={"workspace_id": str(research_id)},
    )
    listed = client.get("/api/v1/workspaces")

    assert update.status_code == 200
    assert update.json()["data"]["default_workspace_id"] == str(research_id)
    assert listed.json()["data"]["current_workspace_id"] == str(research_id)

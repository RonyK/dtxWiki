from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.rest_scopes import REST_REQUIRED_SCOPES
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration


def _client(
    page_service,
    *,
    enabled,
    service_available=True,
    email="workspace-graph@example.com",
    role="viewer",
):
    """A client whose workspace map switch is the owner's Settings choice."""

    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="workspace-graph-api-secret",
                graph_service_available=service_available,
            ),
            page_service=page_service,
        )
    )
    workspace_id = authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email=email,
        workspace_slug="default",
        role=role,
    )
    with page_service.session_factory.begin() as session:
        session.get(
            Workspace, UUID(str(workspace_id))
        ).workspace_graph_enabled = enabled
    return client, workspace_id


def _capability(client, workspace_id):
    response = client.get(f"/api/v1/workspaces/{workspace_id}/graph/capability")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _published_page(page_service, workspace_id) -> UUID:
    with page_service.session_factory.begin() as session:
        page = ContextPage(
            id=uuid4(),
            workspace_id=UUID(str(workspace_id)),
            slug=f"capability-{uuid4().hex[:8]}",
            title="Capability seed",
            page_type="concept",
            lifecycle_status="published",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=page.workspace_id,
            id=uuid4(),
            page_id=page.id,
            version_no=1,
            body_md="# Capability seed",
            content_hash=f"capability-{page.id}",
            actor_type="human",
            actor_id=None,
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        page.current_published_version_id = version.id
        return page.id


def test_workspace_graph_route_is_non_discoverable_when_flag_disabled(page_service):
    client, workspace_id = _client(page_service, enabled=False)
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/graph/overview/query",
        json={},
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "not_found"


def test_workspace_graph_route_returns_versioned_scene_when_enabled(page_service):
    client, workspace_id = _client(page_service, enabled=True)
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/graph/overview/query",
        json={"lens": "links"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["schema_version"] == "workspace-graph-scene.v1"
    assert body["data"]["workspace_scope_considered"] is True
    assert body["data"]["head_mode"] == "published"
    assert body["data"]["capabilities"]["export"] is True


def test_workspace_graph_route_negotiates_gzip_without_changing_visible_json(
    page_service,
):
    client, workspace_id = _client(page_service, enabled=True)
    page_id = _published_page(page_service, workspace_id)
    route = f"/api/v1/workspaces/{workspace_id}/graph/overview/query"
    identity = client.post(
        route,
        json={"lens": "links"},
        headers={"Accept-Encoding": "gzip;q=0, identity"},
    )
    compressed = client.post(
        route,
        json={"lens": "links"},
        headers={"Accept-Encoding": "gzip"},
    )

    assert identity.status_code == compressed.status_code == 200
    assert len(identity.content) >= 1000
    assert "content-encoding" not in identity.headers
    assert compressed.headers["content-encoding"] == "gzip"
    # These are two real requests, each with its own envelope request UUID.
    # Compare the complete decoded JSON after removing only that audit field.
    # The middleware unit tests separately verify exact upstream byte equality.
    identity_json = identity.json()
    compressed_json = compressed.json()
    identity_request_id = UUID(identity_json["envelope"].pop("request_id"))
    compressed_request_id = UUID(compressed_json["envelope"].pop("request_id"))
    assert identity_request_id != compressed_request_id
    assert compressed_json == identity_json
    assert int(compressed.headers["content-length"]) < len(identity.content)
    for response in (identity, compressed):
        assert "accept-encoding" in response.headers["vary"].lower()
        scene = response.json()["data"]
        assert any(node["entity_id"] == str(page_id) for node in scene["nodes"])


def test_workspace_graph_route_preserves_default_and_accepts_explicit_10k_budget(
    page_service,
):
    client, workspace_id = _client(page_service, enabled=True)
    route = f"/api/v1/workspaces/{workspace_id}/graph/overview/query"

    response = client.post(route, json={})
    assert response.status_code == 200, response.text

    for budget in (
        {"documents": 250, "relations": 750},
        {"documents": 2_000, "relations": 6_000},
        {"documents": 2_001, "relations": 6_000},
        {"documents": 2_000, "relations": 6_001},
        {"documents": 10_000, "relations": 100_000},
    ):
        response = client.post(route, json={"budget": budget})
        assert response.status_code == 200, response.text

    for budget in (
        {"documents": 10_001, "relations": 100_000},
        {"documents": 10_000, "relations": 100_001},
    ):
        response = client.post(route, json={"budget": budget})
        assert response.status_code == 422, response.text


def test_workspace_graph_rechecks_membership_after_revocation(page_service):
    email = "workspace-graph-revoked@example.com"
    client, workspace_id = _client(page_service, enabled=True, email=email)
    route = f"/api/v1/workspaces/{workspace_id}/graph/overview/query"
    assert client.post(route, json={}).status_code == 200

    with page_service.session_factory.begin() as session:
        user_id = session.scalar(select(User.id).where(User.email == email))
        workspace_uuid = UUID(workspace_id)
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.workspace_id == workspace_uuid,
            )
        )

    revoked = client.post(route, json={})
    assert revoked.status_code == 404
    assert revoked.json()["error_code"] == "not_found"
    assert "data" not in revoked.json()


def test_workspace_graph_post_requires_general_read_scope():
    assert REST_REQUIRED_SCOPES[
        (
            "POST",
            "/api/v1/workspaces/{workspace_id}/graph/overview/query",
        )
    ] == ("read",)


@pytest.mark.parametrize(
    ("enabled", "service_available", "reason"),
    (
        (True, True, "enabled"),
        (False, True, "disabled_by_workspace"),
        (True, False, "service_unavailable"),
        # The operator break-glass wins over the owner's choice.
        (False, False, "service_unavailable"),
    ),
)
def test_capability_reports_the_effective_map_state(
    page_service,
    enabled,
    service_available,
    reason,
):
    client, workspace_id = _client(
        page_service,
        enabled=enabled,
        service_available=service_available,
    )

    capability = _capability(client, workspace_id)

    assert capability == {
        "workspace_map": {
            "effective_enabled": reason == "enabled",
            "workspace_enabled": enabled,
            "service_available": service_available,
            "reason": reason,
        },
        "usage_relation_signals_enabled": True,
        "can_manage": False,
    }
    overview = client.post(
        f"/api/v1/workspaces/{workspace_id}/graph/overview/query",
        json={},
    )
    assert overview.status_code == (200 if reason == "enabled" else 404)


@pytest.mark.parametrize(
    ("enabled", "service_available"),
    ((False, True), (True, False)),
)
def test_local_graph_does_not_depend_on_the_workspace_map_switch(
    page_service,
    enabled,
    service_available,
):
    client, workspace_id = _client(
        page_service,
        enabled=enabled,
        service_available=service_available,
    )
    page_id = _published_page(page_service, workspace_id)

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/graph/query",
        json={"seeds": [{"kind": "context_page", "id": str(page_id)}]},
    )

    assert response.status_code == 200, response.text


def test_only_owners_may_manage_the_workspace_map(page_service):
    owner, workspace_id = _client(
        page_service,
        enabled=False,
        email="capability-owner@example.com",
        role="owner",
    )

    assert _capability(owner, workspace_id)["can_manage"] is True


def test_capability_get_requires_general_read_scope():
    assert REST_REQUIRED_SCOPES[
        ("GET", "/api/v1/workspaces/{workspace_id}/graph/capability")
    ] == ("read",)


def test_map_api_search_respects_visible_titles_and_primary_paths_not_navigation(
    page_service,
):
    from backend.tests.integration import test_workspace_graph_service as helpers

    client, workspace_text = _client(page_service, enabled=True)
    workspace_id = UUID(workspace_text)
    with page_service.session_factory.begin() as session:
        folder = helpers._folder(session, workspace_id, "Research Delta")
        page, _head = helpers._page(session, workspace_id, "alpha")
        helpers._page(session, workspace_id, "beta")
        helpers._placement(session, workspace_id, page, folder)
        page_id = page.id
    route = f"/api/v1/workspaces/{workspace_id}/graph/overview/query"
    for search, expected in (
        ("Alpha", {str(page_id)}),
        ("research delta", {str(page_id)}),
        ("target", set()),
        ("context", set()),
        (str(page_id), set()),
    ):
        response = client.post(route, json={"search": search})
        assert response.status_code == 200, response.text
        scene = response.json()["data"]
        assert {node["entity_id"] for node in scene["nodes"]} == expected, search
        assert scene["counts"]["candidate_documents"] == len(expected)
        assert sum(facet["count"] for facet in scene["facet_counts"]["folders"]) == len(
            expected
        )


@pytest.mark.parametrize("mode", ["include", "exclude", "only"])
def test_map_api_applies_its_unplaced_facet_without_422(page_service, mode):
    from backend.tests.integration import test_workspace_graph_service as helpers

    client, workspace_text = _client(page_service, enabled=True)
    workspace_id = UUID(workspace_text)
    with page_service.session_factory.begin() as session:
        fixture = helpers._unplaced_folder_fixture(session, workspace_id)
    folder_page = str(fixture["pages"]["folder"])
    unplaced = {str(page_id) for page_id in fixture["pages"].values()} - {folder_page}
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/graph/overview/query",
        json={"folders": {mode: ["unplaced"]}},
    )
    assert response.status_code == 200, response.text
    scene = response.json()["data"]
    assert {node["entity_id"] for node in scene["nodes"]} == (
        {folder_page} if mode == "exclude" else unplaced
    )
    assert {
        facet["value"]: facet["count"] for facet in scene["facet_counts"]["folders"]
    } == {
        str(fixture["nearest"]): 1,
        "unplaced": 3,
    }


def test_map_api_accepts_mixed_uuid_and_unplaced_and_rejects_invalid_sentinels(
    page_service,
):
    from backend.tests.integration import test_workspace_graph_service as helpers

    client, workspace_text = _client(page_service, enabled=True)
    workspace_id = UUID(workspace_text)
    with page_service.session_factory.begin() as session:
        fixture = helpers._unplaced_folder_fixture(session, workspace_id)
    route = f"/api/v1/workspaces/{workspace_id}/graph/overview/query"
    for mode in ("include", "only"):
        response = client.post(
            route, json={"folders": {mode: [str(fixture["ancestor"]), "unplaced"]}}
        )
        assert response.status_code == 200, response.text
        assert {node["entity_id"] for node in response.json()["data"]["nodes"]} == {
            str(page_id) for page_id in fixture["pages"].values()
        }
    for folders in (
        {"include": ["UNPLACED"]},
        {"include": ["unplaced "]},
        {"include": ["unplaced", "unplaced"]},
        {"include": ["unplaced"], "exclude": ["unplaced"]},
        {"only": ["unplaced"], "include": [str(fixture["ancestor"])]},
    ):
        response = client.post(route, json={"folders": folders})
        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == "validation_failed"

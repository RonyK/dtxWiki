"""Workspace owners switch the Knowledge Map on from Settings (F06-T1)."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.workspace import Workspace
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration


def _client(page_service, *, role: str, email: str) -> tuple[TestClient, str]:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="settings-graph-card-secret",
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
    return client, str(workspace_id)


def _graph_card(client: TestClient, workspace_id: str) -> dict:
    response = client.get(f"/api/v1/workspaces/{workspace_id}/settings/policies")
    assert response.status_code == 200, response.text
    return response.json()["data"]["graph"]


def _save(client: TestClient, workspace_id: str, revision: str, changes: dict):
    return client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={"group": "graph", "expected_revision": revision, "changes": changes},
    )


def test_graph_card_defaults_keep_the_map_off_and_usage_signals_on(page_service):
    client, workspace_id = _client(page_service, role="owner", email="o1@example.com")

    card = _graph_card(client, workspace_id)

    assert card["policy"] == {
        "workspace_map_enabled": False,
        "usage_relation_signals_enabled": True,
    }


def test_owner_enables_the_workspace_map_with_an_audited_card_save(page_service):
    client, workspace_id = _client(page_service, role="owner", email="o2@example.com")
    card = _graph_card(client, workspace_id)

    saved = _save(
        client,
        workspace_id,
        card["revision"],
        {"workspace_map_enabled": True, "usage_relation_signals_enabled": False},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["graph"]["policy"] == {
        "workspace_map_enabled": True,
        "usage_relation_signals_enabled": False,
    }
    with page_service.session_factory() as session:
        workspace = session.get(Workspace, UUID(workspace_id))
        assert workspace.workspace_graph_enabled is True
        assert workspace.usage_relation_signals_enabled is False
        actions = session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.workspace_id == UUID(workspace_id),
                AuditEvent.action == "settings.policy.graph.updated",
            )
        ).all()
    assert len(actions) == 1


def test_graph_card_rejects_stale_revisions_and_non_boolean_values(page_service):
    client, workspace_id = _client(page_service, role="owner", email="o3@example.com")
    card = _graph_card(client, workspace_id)
    assert _save(
        client, workspace_id, card["revision"], {"workspace_map_enabled": True}
    ).status_code == 200

    stale = _save(
        client, workspace_id, card["revision"], {"workspace_map_enabled": False}
    )
    assert stale.status_code == 409, stale.text

    fresh = _graph_card(client, workspace_id)
    for bad in ("true", 1, None):
        rejected = _save(
            client, workspace_id, fresh["revision"], {"workspace_map_enabled": bad}
        )
        assert rejected.status_code == 422, rejected.text
    unknown = _save(client, workspace_id, fresh["revision"], {"map_enabled": True})
    assert unknown.status_code == 422, unknown.text
    assert _graph_card(client, workspace_id)["policy"]["workspace_map_enabled"] is True


def test_non_owner_cannot_change_the_graph_card(page_service):
    client, workspace_id = _client(page_service, role="editor", email="e1@example.com")
    card = _graph_card(client, workspace_id)

    rejected = _save(
        client, workspace_id, card["revision"], {"workspace_map_enabled": True}
    )

    assert rejected.status_code == 403, rejected.text
    with page_service.session_factory() as session:
        workspace = session.get(Workspace, UUID(workspace_id))
        assert workspace.workspace_graph_enabled is False

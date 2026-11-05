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
    assert (
        _save(
            client, workspace_id, card["revision"], {"workspace_map_enabled": True}
        ).status_code
        == 200
    )

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


def test_labs_save_is_persisted_atomic_and_workspace_admin_only(page_service):
    """One lifecycle covers the Labs storage boundary and role revocation."""
    from backend.models.user import User
    from backend.models.workspace import WorkspaceMembership

    client, workspace_id = _client(page_service, role="owner", email="labs@example.com")
    base = f"/api/v1/workspaces/{workspace_id}"
    original = client.get(f"{base}/settings/policies").json()["data"]["labs"]
    assert original["policy"] == {
        "ask_enabled": False,
        "agent_enabled": False,
        "ask_retrieval_mode": "auto",
        "workspace_map_enabled": False,
        "usage_relation_signals_enabled": True,
    }
    assert client.get(f"{base}/ask/sessions").status_code == 404
    assert client.get(f"{base}/agent/pages").status_code == 404
    # Keep the owner here: the core reader already hides sensitive Agent
    # guides from members, so a member-only assertion would miss this bypass.
    disabled_search = client.post(
        f"{base}/search", json={"query": "system", "scope": "all"}
    )
    assert disabled_search.status_code == 200, disabled_search.text
    assert not any(
        item["type"] == "agent_page"
        for item in disabled_search.json()["data"]["ranked_results"]
    )
    assert client.get(f"{base}/tree?section=agent").status_code == 404
    assert (
        client.post(
            f"{base}/search", json={"query": "system", "scope": "agent_page"}
        ).status_code
        == 404
    )
    with page_service.session_factory.begin() as session:
        other = Workspace(slug="other-labs", title="Other", name="Other")
        session.add(other)
        session.flush()
        other_id = other.id

    changes = {
        "ask_enabled": True,
        "agent_enabled": True,
        "ask_retrieval_mode": "keyword",
        "workspace_map_enabled": True,
        "usage_relation_signals_enabled": False,
    }
    saved = client.patch(
        f"{base}/settings/policies/batch",
        json={
            "group": "labs",
            "expected_revision": original["revision"],
            "changes": changes,
        },
    )
    assert saved.status_code == 200, saved.text
    reloaded = client.get(f"{base}/settings/policies").json()["data"]["labs"]
    assert reloaded["policy"] == changes
    assert (
        saved.json()["data"]["workspace"]["policy"]["ask_retrieval_mode"] == "keyword"
    )
    assert client.get(f"{base}/ask/sessions").status_code == 200
    assert client.get(f"{base}/agent/pages").status_code == 200
    tree = client.get(f"{base}/tree?section=agent")
    assert tree.status_code == 200, tree.text
    agent_root_id = next(
        node["id"] for node in tree.json()["data"]["nodes"] if node["parent_id"] is None
    )
    assert client.get(f"{base}/tree/nodes/{agent_root_id}").status_code == 200
    agent_search = client.post(
        f"{base}/search", json={"query": "system", "scope": "all"}
    )
    assert agent_search.status_code == 200, agent_search.text
    assert any(
        item["type"] == "agent_page"
        for item in agent_search.json()["data"]["ranked_results"]
    )
    assert (
        client.get(f"{base}/graph/capability").json()["data"]["workspace_map"][
            "effective_enabled"
        ]
        is True
    )
    assert (
        client.patch(
            f"{base}/settings/policies/batch",
            json={
                "group": "labs",
                "expected_revision": original["revision"],
                "changes": {"ask_enabled": False},
            },
        ).status_code
        == 409
    )
    rejected = client.patch(
        f"{base}/settings/policies/batch",
        json={
            "group": "labs",
            "expected_revision": reloaded["revision"],
            "changes": {"ask_enabled": False, "agent_enabled": "true"},
        },
    )
    assert rejected.status_code == 422
    invalid_mode = client.patch(
        f"{base}/settings/policies/batch",
        json={
            "group": "labs",
            "expected_revision": reloaded["revision"],
            "changes": {"agent_enabled": False, "ask_retrieval_mode": "unsupported"},
        },
    )
    assert invalid_mode.status_code == 422
    assert (
        client.get(f"{base}/settings/policies").json()["data"]["labs"]["policy"]
        == changes
    )
    with page_service.session_factory.begin() as session:
        assert session.get(Workspace, other_id).labs_policy == {}
        assert session.get(Workspace, other_id).workspace_graph_enabled is False
        assert session.get(Workspace, other_id).ask_retrieval_mode == "auto"
        assert (
            session.get(Workspace, UUID(workspace_id)).ask_retrieval_mode == "keyword"
        )
        user_id = session.scalar(
            select(User.id).where(User.email == "labs@example.com")
        )
        membership = session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == UUID(workspace_id),
                WorkspaceMembership.user_id == user_id,
            )
        )
        membership.role = "editor"
    assert client.get(f"{base}/ask/sessions").status_code == 403
    assert client.get(f"{base}/agent/pages").status_code == 403
    assert client.get(f"{base}/tree?section=agent").status_code == 403
    assert client.get(f"{base}/tree/nodes/{agent_root_id}").status_code == 403
    assert (
        client.post(
            f"{base}/tree/nodes",
            json={"section": "agent", "parent_id": agent_root_id, "title": "Denied"},
        ).status_code
        == 403
    )
    visible_tree = client.get(f"{base}/tree")
    assert visible_tree.status_code == 200, visible_tree.text
    assert not any(
        node["section"] in {"agent", "ask"}
        for node in visible_tree.json()["data"]["nodes"]
    )
    assert (
        client.post(
            f"{base}/search", json={"query": "system", "scope": "agent_page"}
        ).status_code
        == 403
    )
    mixed_search = client.post(
        f"{base}/search", json={"query": "system", "scope": "all"}
    )
    assert mixed_search.status_code == 200, mixed_search.text
    assert not any(
        item["type"] == "agent_page"
        for item in mixed_search.json()["data"]["ranked_results"]
    )
    assert (
        client.get(f"{base}/graph/capability").json()["data"]["workspace_map"]["reason"]
        == "permission_denied"
    )
    assert client.post(f"{base}/graph/overview/query", json={}).status_code == 404
    denied = client.patch(
        f"{base}/settings/policies/batch",
        json={
            "group": "labs",
            "expected_revision": reloaded["revision"],
            "changes": {"ask_enabled": False},
        },
    )
    assert denied.status_code == 403

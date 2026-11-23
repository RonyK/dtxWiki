import base64
import json
import logging
from datetime import timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.llm_connection import UserLLMConnection, WorkspaceLLMConnection
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.tests.conftest import authenticate_test_client


def test_browser_connection_crud_preserves_ownership_and_secret_boundary(
    page_service, tmp_path, caplog, monkeypatch
):
    request_logger = logging.getLogger("dtxwiki.request")
    monkeypatch.setattr(request_logger, "propagate", True)
    caplog.set_level(logging.INFO, logger=request_logger.name)
    keyring = tmp_path / "vault-keys.json"
    keyring.write_text(
        json.dumps(
            {
                "active_key_id": "test",
                "keys": {"test": base64.urlsafe_b64encode(bytes(32)).decode()},
            }
        ),
        encoding="utf-8",
    )
    keyring.chmod(0o600)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="llm-connection-api-test",
        llm_credential_keyring_file=str(keyring),
    )
    app = create_app(settings=settings, page_service=page_service)
    origin = f"http://127.0.0.1:{settings.backend_port}"
    owner = TestClient(app, base_url=origin)
    other = TestClient(app, base_url=origin)
    owner_email = "llm-owner@example.com"
    other_email = "llm-editor@example.com"
    authenticate_test_client(owner, owner_email)
    authenticate_test_client(other, other_email)
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    other_workspace_id = other.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    cookie_codec = app.state.session_service.token_codec
    with page_service.session_factory.begin() as session:
        editor = session.scalar(select(User).where(User.email == other_email))
        grant_workspace_membership(
            session,
            user_id=editor.id,
            workspace_id=UUID(workspace_id),
            role="editor",
        )
        owner_user = session.scalar(select(User).where(User.email == owner_email))
        aged_session, _ = cookie_codec.issue(
            owner_user,
            authenticated_at=cookie_codec.now()
            - timedelta(seconds=settings.auth_reauthentication_max_age_seconds + 5),
        )
    owner.cookies.set(
        settings.session_cookie_name, aged_session, domain="127.0.0.1", path="/"
    )

    personal_path = "/api/v1/me/llm-connections"
    workspace_path = f"/api/v1/workspaces/{workspace_id}/llm-connections"
    marker = "credential-value-which-must-stay-private"
    payload = {
        "name": "Example connection",
        "adapter_id": "builtin.openai.responses",
        "model_profiles": [{"id": "default", "model_id": "example-model"}],
        "credential": {"api_key": marker},
    }
    denied = owner.post(
        personal_path, json=payload, headers={"Origin": "https://other.example"}
    )
    assert (
        denied.status_code == 403 and denied.json()["error_code"] == "origin_required"
    )

    # Vite forwards the browser's origin while rewriting the backend Host.
    owner.headers["Origin"] = f"http://127.0.0.1:{settings.frontend_dev_port}"
    denied = owner.post(personal_path, json=payload)
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "recent_reauthentication_required"
    assert (
        owner.post(
            "/api/v1/auth/reauthenticate", json={"current_password": "correct-password"}
        ).status_code
        == 200
    )

    # Pydantic's error location would include this user-supplied map key.
    invalid = owner.post(personal_path, json={**payload, "credential": {marker: []}})
    assert invalid.status_code == 422
    assert marker not in invalid.text

    personal = owner.post(personal_path, json=payload)
    workspace = owner.post(workspace_path, json=payload)
    assert personal.status_code == workspace.status_code == 201, (
        personal.text,
        workspace.text,
    )
    assert (
        personal.headers["cache-control"]
        == workspace.headers["cache-control"]
        == "no-store"
    )
    assert marker not in personal.text + workspace.text
    personal_record = personal.json()["data"]
    workspace_record = workspace.json()["data"]
    assert personal_record["has_credential"] is True
    assert personal_record["status"] == "disabled"
    assert personal_record["verification_status"] == "unverified"
    assert owner.get(personal_path).json()["data"] == [personal_record]

    personal_item = f"{personal_path}/{personal_record['id']}"
    other_workspace_path = f"/api/v1/workspaces/{other_workspace_id}/llm-connections"
    assert other.get(personal_item).status_code == 404
    assert other.get(workspace_path).status_code == 403
    assert (
        other.get(f"{other_workspace_path}/{workspace_record['id']}").status_code == 404
    )

    activity_path = f"/api/v1/workspaces/{workspace_id}/activity?period=all"
    owner_activity = owner.get(activity_path)
    editor_activity = other.get(activity_path)
    assert owner_activity.status_code == editor_activity.status_code == 200
    assert workspace_record["id"] in owner_activity.text
    assert workspace_record["id"] not in editor_activity.text
    assert "llm.connection.created" not in editor_activity.text

    issued = owner.post(
        "/api/v1/auth/tokens",
        json={
            "name": "Read credential",
            "scopes": ["mcp:read"],
            "workspace_grants": [{"workspace_id": workspace_id}],
        },
    )
    assert issued.status_code == 201, issued.text
    bearer = issued.json()["data"]["plain_token"]
    bearer_denied = owner.get(
        workspace_path, headers={"Authorization": f"Bearer {bearer}"}
    )
    assert bearer_denied.status_code == 403
    assert "requires a user session" in bearer_denied.json()["message"]

    revision = personal_record["revision"]
    duplicate_profiles = owner.patch(
        personal_item,
        json={
            "expected_revision": revision,
            "model_profiles": [
                {"id": "repeated", "model_id": "first-model"},
                {"id": "repeated", "model_id": "second-model"},
            ],
        },
    )
    assert duplicate_profiles.status_code == 422
    change = {"expected_revision": revision, "name": "Renamed connection"}
    updated = owner.patch(personal_item, json=change)
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["name"] == change["name"]
    assert owner.patch(personal_item, json=change).status_code == 409

    deleted = owner.delete(
        personal_item, params={"expected_revision": updated.json()["data"]["revision"]}
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["status"] == "deleted"
    assert deleted.json()["data"]["has_credential"] is False
    assert owner.get(personal_item).status_code == 404

    with page_service.session_factory() as session:
        personal_stored = session.get(UserLLMConnection, UUID(personal_record["id"]))
        workspace_stored = session.get(
            WorkspaceLLMConnection, UUID(workspace_record["id"])
        )
        assert (
            personal_stored.active_credential
            is personal_stored.pending_credential
            is None
        )
        assert workspace_stored.active_credential is not None
        assert marker not in repr(workspace_stored.active_credential)
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action.like("llm.connection.%"))
        ).all()
        assert {(event.action, event.workspace_id) for event in audit} == {
            ("llm.connection.created", None),
            ("llm.connection.updated", None),
            ("llm.connection.deleted", None),
            ("llm.connection.created", UUID(workspace_id)),
        }
        assert marker not in repr(
            [(event.payload, event.event_summary) for event in audit]
        )
    assert personal_path in caplog.text
    assert marker not in caplog.text

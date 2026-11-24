import base64
import json
import logging
from contextlib import ExitStack
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select

from backend.api.deps import get_workspace_session_factory
from backend.api.llm_connections import ConnectionOwner, workspace_owner
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.llm_connection import UserLLMConnection, WorkspaceLLMConnection
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.services.llm_connection_service import LLMConnectionService
from backend.tests.conftest import authenticate_test_client, captured_log_records


def test_browser_connection_crud_preserves_ownership_and_secret_boundary(
    page_service, tmp_path, request, monkeypatch
):
    log_capture = ExitStack()
    request.addfinalizer(log_capture.close)
    records = log_capture.enter_context(
        captured_log_records("dtxwiki.request", logging.INFO)
    )
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

    with page_service.session_factory.begin() as session:
        editor = session.scalar(select(User).where(User.email == other_email))
        grant_workspace_membership(
            session,
            user_id=editor.id,
            workspace_id=UUID(workspace_id),
            role="editor",
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
    # Pydantic's error location would include this user-supplied map key.
    invalid = owner.post(personal_path, json={**payload, "credential": {marker: []}})
    assert invalid.status_code == 422
    assert marker not in invalid.text

    endpoint_required = owner.post(
        personal_path,
        json={**payload, "adapter_id": "builtin.openai_compatible.chat_completions"},
    )
    assert endpoint_required.status_code == 422
    assert endpoint_required.json()["error_code"] == "adapter_endpoint_required"

    # The control read must finish before any Workspace session starts writing.
    authorized = []
    original_authorize = LLMConnectionService.authorize_workspace_write

    def record_authorization(self, session, **kwargs):
        result = original_authorize(self, session, **kwargs)
        authorized.append(session)
        return result

    monkeypatch.setattr(
        LLMConnectionService, "authorize_workspace_write", record_authorization
    )

    # Simulate a shard that has no control-plane tables, including on writes.
    class WorkspaceOnlySession(Session):
        def execute(self, statement, *args, **kwargs):
            assert not authorized or not authorized[-1].in_transaction()
            if isinstance(statement, Select):
                names = {table.name for table in statement.get_final_froms()}
                assert names.isdisjoint({"workspace", "user", "workspace_membership"})
            return super().execute(statement, *args, **kwargs)

        def scalar(self, statement, *args, **kwargs):
            if isinstance(statement, Select):
                names = {table.name for table in statement.get_final_froms()}
                assert names.isdisjoint({"workspace", "user", "workspace_membership"})
            return super().scalar(statement, *args, **kwargs)

    shard_factory = sessionmaker(
        bind=page_service.session_factory.kw["bind"], class_=WorkspaceOnlySession
    )
    app.dependency_overrides[get_workspace_session_factory] = lambda: shard_factory
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
    personal_item = f"{personal_path}/{personal_record['id']}"
    workspace_item = f"{workspace_path}/{workspace_record['id']}"
    selected_columns = []

    def capture_connection_results(_connection, cursor, statement, *_args):
        if cursor.description and "llm_connection." in statement:
            selected_columns.append({column[0] for column in cursor.description})

    engine = page_service.session_factory.kw["bind"]
    event.listen(engine, "after_cursor_execute", capture_connection_results)
    try:
        assert owner.get(personal_path).json()["data"] == [personal_record]
        assert owner.get(personal_item).json()["data"] == personal_record
        assert owner.get(workspace_path).json()["data"] == [workspace_record]
        assert owner.get(workspace_item).json()["data"] == workspace_record
    finally:
        event.remove(engine, "after_cursor_execute", capture_connection_results)
    assert len(selected_columns) == 4
    assert all(
        columns.isdisjoint({"active_credential", "pending_credential"})
        for columns in selected_columns
    )

    other_workspace_path = f"/api/v1/workspaces/{other_workspace_id}/llm-connections"
    assert other.get(personal_item).status_code == 404
    assert other.get(workspace_path).status_code == 403
    assert (
        other.get(f"{other_workspace_path}/{workspace_record['id']}").status_code == 404
    )

    app.dependency_overrides.pop(get_workspace_session_factory)
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
    bearer_write = owner.post(
        personal_path, json=payload, headers={"Authorization": f"Bearer {bearer}"}
    )
    assert bearer_write.status_code == 403
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
    with page_service.session_factory.begin() as session:
        stored = session.get(UserLLMConnection, UUID(personal_record["id"]))
        stored.adapter_implementation_version = "0.0.0"
        stored.verification_status = "ready"
        stored.adapter_id = "vendor.uninstalled"
        stored.config_schema_version = 999
    assert (
        owner.get(personal_item).json()["data"]["adapter_implementation_version"]
        == "0.0.0"
    )
    assert (
        owner.get(personal_path).json()["data"][0]["adapter_implementation_version"]
        == "0.0.0"
    )
    change = {"expected_revision": revision, "name": "Renamed connection"}
    updated = owner.patch(personal_item, json=change)
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["name"] == change["name"]
    assert updated.json()["data"]["adapter_implementation_version"] == "0.0.0"
    assert updated.json()["data"]["verification_status"] == "ready"
    assert owner.patch(personal_item, json=change).status_code == 409
    with page_service.session_factory.begin() as session:
        stored = session.get(UserLLMConnection, UUID(personal_record["id"]))
        stored.adapter_id = payload["adapter_id"]
    config_change = {
        "expected_revision": updated.json()["data"]["revision"],
        "provider_options": {},
    }
    assert (
        owner.patch(personal_item, json=config_change).json()["error_code"]
        == "adapter_version_unavailable"
    )
    with page_service.session_factory.begin() as session:
        stored = session.get(UserLLMConnection, UUID(personal_record["id"]))
        stored.config_schema_version = personal_record["config_schema_version"]
        stored.endpoint_profile = {
            "kind": "adapter_default",
            "base_url": "https://old.example",
        }
    upgraded = owner.patch(personal_item, json=config_change)
    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["data"]["adapter_implementation_version"] != "0.0.0"
    assert upgraded.json()["data"]["verification_status"] == "unverified"
    with page_service.session_factory() as session:
        stored = session.get(UserLLMConnection, UUID(personal_record["id"]))
        assert stored.endpoint_profile["base_url"] != "https://old.example"

    with page_service.session_factory.begin() as session:
        stored = session.get(UserLLMConnection, UUID(personal_record["id"]))
        stored.pending_credential = {"ciphertext": "existing encrypted candidate"}
        stored.adapter_implementation_version = "0.0.0"
    deleted = owner.delete(
        personal_item, params={"expected_revision": upgraded.json()["data"]["revision"]}
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
    app.dependency_overrides[get_workspace_session_factory] = lambda: shard_factory
    workspace_updated = owner.patch(
        workspace_item,
        json={
            "expected_revision": workspace_record["revision"],
            "name": "Workspace renamed",
        },
    )
    assert workspace_updated.status_code == 200, workspace_updated.text
    workspace_deleted = owner.delete(
        workspace_item,
        params={"expected_revision": workspace_updated.json()["data"]["revision"]},
    )
    assert workspace_deleted.status_code == 200, workspace_deleted.text
    # Missing control wiring must fail closed for Workspace writes.
    app.dependency_overrides[workspace_owner] = lambda: ConnectionOwner(
        "workspace", UUID(workspace_id), None, shard_factory, None
    )
    with pytest.raises(RuntimeError, match="require a control session"):
        owner.post(workspace_path, json=payload)
    with pytest.raises(RuntimeError, match="require a control session"):
        owner.patch(workspace_item, json={"expected_revision": 1, "name": "x"})
    with pytest.raises(RuntimeError, match="require a control session"):
        owner.delete(workspace_item, params={"expected_revision": 1})
    messages = [record.getMessage() for record in records]
    assert any(personal_path in message for message in messages)
    assert all(marker not in message for message in messages)

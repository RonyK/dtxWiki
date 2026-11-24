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
from backend.llm import transport as llm_transport
from backend.llm.adapter_registry import LLMAdapterRegistry
from backend.llm.credential_vault import CredentialBinding
from backend.models.audit import AuditEvent
from backend.models.llm_connection import UserLLMConnection, WorkspaceLLMConnection
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.services.llm_connection_service import (
    LLMConnectionService,
    build_llm_connection_service,
)
from backend.spi.llm import TransportResponse
from backend.tests.conftest import authenticate_test_client, captured_log_records


def _settings(tmp_path, **values) -> Settings:
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
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="llm-connection-api-test",
        llm_credential_keyring_file=str(keyring),
        **values,
    )


def test_browser_connection_crud_preserves_ownership_and_secret_boundary(
    page_service, tmp_path, request, monkeypatch
):
    log_capture = ExitStack()
    request.addfinalizer(log_capture.close)
    records = log_capture.enter_context(
        captured_log_records("dtxwiki.request", logging.INFO)
    )
    settings = _settings(tmp_path)
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
    original_authorize = LLMConnectionService.authorize_write

    def record_authorization(self, session, **kwargs):
        result = original_authorize(self, session, **kwargs)
        authorized.append(session)
        return result

    monkeypatch.setattr(LLMConnectionService, "authorize_write", record_authorization)

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
    assert all("active_credential" not in columns for columns in selected_columns)

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
    # A settings edit never moves the stored key to another address: the
    # retired default stays until a key replacement, and the view says so.
    upgraded = owner.patch(personal_item, json=config_change)
    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["data"]["adapter_implementation_version"] != "0.0.0"
    assert upgraded.json()["data"]["verification_status"] == "unverified"
    assert upgraded.json()["data"]["unavailable_reason"] == "endpoint_unavailable"
    with page_service.session_factory() as session:
        stored = session.get(UserLLMConnection, UUID(personal_record["id"]))
        assert stored.endpoint_profile["base_url"] == "https://old.example"

    with page_service.session_factory.begin() as session:
        stored = session.get(UserLLMConnection, UUID(personal_record["id"]))
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
        assert personal_stored.active_credential is None
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


def test_verification_applies_only_to_the_setup_it_checked(
    page_service, tmp_path, monkeypatch
):
    adapter = "builtin.openai_compatible.chat_completions"
    settings = _settings(
        tmp_path,
        llm_endpoint_profiles_json=json.dumps(
            [
                {
                    "id": "gateway",
                    "adapter_id": adapter,
                    "base_url": "https://gateway.example/v1/",
                    "label": "Team gateway",
                }
            ]
        ),
    )
    app = create_app(settings=settings, page_service=page_service)
    owner = TestClient(app, base_url=f"http://127.0.0.1:{settings.backend_port}")
    authenticate_test_client(owner, "llm-verifier@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    catalog = owner.get("/api/v1/llm-adapters").json()["data"]
    assert next(item for item in catalog if item["adapter_id"] == adapter)[
        "endpoints"
    ] == [
        {
            "id": "gateway",
            "label": "Team gateway",
            "base_url": "https://gateway.example/v1",
        }
    ]

    calls = []
    during_call = []

    class Provider:
        """Accepts keys starting with "good"; any other key is refused."""

        async def post_json(self, url, *, headers, payload, timeout_seconds):
            calls.append((url, payload["model"]))
            for action in during_call:
                action()
            accepted = headers.get("authorization", "").startswith("Bearer good")
            body = (
                {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
                if accepted
                else {"error": {"message": "invalid key"}}
            )
            return TransportResponse(
                status_code=200 if accepted else 401,
                headers={},
                body=json.dumps(body).encode(),
            )

    monkeypatch.setattr(llm_transport, "HttpxLLMTransport", Provider)
    control = page_service.session_factory
    path = f"/api/v1/workspaces/{workspace_id}/llm-connections"
    created = owner.post(
        path,
        json={
            "name": "Gateway",
            "adapter_id": adapter,
            "endpoint_profile_id": "gateway",
            "model_profiles": [{"id": "small", "model_id": "small-model"}],
            "credential": {"api_key": "good-first"},
        },
    )
    assert created.status_code == 201, created.text
    record = created.json()["data"]
    assert record["endpoint_profile_id"] == "gateway"
    item = f"{path}/{record['id']}"

    def stored_key():
        with control() as session:
            stored = session.get(WorkspaceLLMConnection, UUID(record["id"]))
            return connections.vault.decrypt(
                stored.active_credential,
                binding=CredentialBinding(
                    "workspace", UUID(workspace_id), stored.id, stored.secret_revision
                ),
            )["api_key"]

    connections = build_llm_connection_service(settings, LLMAdapterRegistry())
    enable = {"expected_revision": record["revision"], "status": "enabled"}
    assert owner.patch(item, json=enable).json()["error_code"] == (
        "connection_not_verified"
    )

    verified = owner.post(f"{item}/verify", json={"expected_revision": 1})
    assert verified.status_code == 200, verified.text
    assert verified.json()["data"]["verification"]["status"] == "succeeded"
    assert verified.json()["data"]["connection"]["verification_status"] == "ready"
    assert calls == [("https://gateway.example/v1/chat/completions", "small-model")]
    record = owner.patch(item, json=enable).json()["data"]
    assert (record["status"], record["revision"]) == ("enabled", 2)

    # A refused key is never stored; the working key stays in use.
    replace = {"expected_revision": 2, "credential": {"api_key": "bad"}}
    refused = owner.put(f"{item}/credential", json=replace).json()["data"]
    assert refused["verification"]["status"] == "failed"
    assert refused["verification"]["error_code"] == "authentication_failed"
    assert refused["connection"] == record
    assert stored_key() == "good-first"

    # An edit made while the new key is being checked wins: nothing is saved,
    # and a refused key reports the conflict too, so a client retrying from
    # that answer cannot adopt an edit it has not seen. Budget exception (two
    # rows): an accepted key is stopped by the write's revision check, a
    # refused key by the check on the answer -- separate code, and neither
    # row can fail for the other's defect.
    def rename():
        with control.begin() as session:
            session.get(WorkspaceLLMConnection, UUID(record["id"])).revision += 1

    during_call.append(rename)
    for revision, key in ((2, "good-second"), (3, "bad")):
        replace = {"expected_revision": revision, "credential": {"api_key": key}}
        raced = owner.put(f"{item}/credential", json=replace)
        assert raced.json()["error_code"] == "connection_revision_conflict"
        assert stored_key() == "good-first"
    during_call.clear()

    # A verified key replaces the active one.
    replace = {"expected_revision": 4, "credential": {"api_key": "good-second"}}
    swapped = owner.put(f"{item}/credential", json=replace).json()["data"]
    assert swapped["verification"]["status"] == "succeeded"
    assert swapped["connection"]["revision"] == 5
    assert swapped["connection"]["verification_status"] == "ready"
    assert stored_key() == "good-second"
    with control() as session:
        actions = session.scalars(
            select(AuditEvent.action).where(AuditEvent.target_id == UUID(record["id"]))
        ).all()
    assert {"llm.connection.verified", "llm.connection.credential_rotated"} <= set(
        actions
    )

    # The operator moved the profile's address: the stored setup cannot be
    # switched on again, and a key replacement naming the profile applies it.
    with control.begin() as session:
        stored = session.get(WorkspaceLLMConnection, UUID(record["id"]))
        stored.endpoint_profile = {**stored.endpoint_profile, "base_url": "https://old"}
        stored.status = "disabled"
    moved = owner.get(path).json()["data"][0]
    assert moved["unavailable_reason"] == "endpoint_unavailable"
    enable = {"expected_revision": moved["revision"], "status": "enabled"}
    assert owner.patch(item, json=enable).json()["error_code"] == "endpoint_unavailable"
    replace = {
        "expected_revision": moved["revision"],
        "credential": {"api_key": "good-third"},
        "endpoint_profile_id": "gateway",
    }
    rebound = owner.put(f"{item}/credential", json=replace).json()["data"]
    assert rebound["connection"]["unavailable_reason"] is None

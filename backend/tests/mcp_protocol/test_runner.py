import copy
import json
import logging
from dataclasses import replace
from uuid import UUID, uuid4

import anyio
import pytest
from sqlalchemy import func, select

from backend.mcp.protocol import runner as runner_module
from backend.mcp.protocol.output_validation import (
    MCPOutputContractViolation,
    validate_tool_output,
)
from backend.mcp.protocol.runner import call_tool_async
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.bundle import AgentInputBundle
from backend.models.mcp_chat import McpChatSession
from backend.models.mcp_trace import MCPToolCall
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.mcp_client_service import runtime_binding_key_for_actor
from backend.tests.conftest import captured_log_messages


def _pat_actor(mcp_default_actor, *, actor_id: str, scopes=("*",)) -> ActorContext:
    return mcp_default_actor(actor_id=actor_id, scopes=tuple(scopes))


@pytest.mark.parametrize(
    ("arguments", "expected"),
    (
        ({"conversation_token": "plain-token"}, "plain-token"),
        (
            {"_meta": {"com.dtxwiki/conversationToken": "namespaced-token"}},
            "namespaced-token",
        ),
        ({"_meta": {"conversation_token": "legacy-token"}}, "legacy-token"),
    ),
)
def test_requested_conversation_token_accepts_each_carrier(arguments, expected):
    assert runner_module._requested_conversation_token(arguments) == expected


def test_requested_conversation_token_prefers_plain_then_namespaced_carrier():
    plain_token = _EqualToken("same-token")
    namespaced_token = _EqualToken("same-token")
    legacy_token = _EqualToken("same-token")

    assert (
        runner_module._requested_conversation_token(
            {
                "conversation_token": plain_token,
                "_meta": {
                    "com.dtxwiki/conversationToken": namespaced_token,
                    "conversation_token": legacy_token,
                },
            }
        )
        is plain_token
    )
    assert (
        runner_module._requested_conversation_token(
            {
                "_meta": {
                    "com.dtxwiki/conversationToken": namespaced_token,
                    "conversation_token": legacy_token,
                }
            }
        )
        is namespaced_token
    )


@pytest.mark.parametrize(
    "arguments",
    (
        {
            "conversation_token": "same-token",
            "_meta": {"com.dtxwiki/conversationToken": "same-token"},
        },
        {
            "conversation_token": "same-token",
            "_meta": {"conversation_token": "same-token"},
        },
        {
            "_meta": {
                "com.dtxwiki/conversationToken": "same-token",
                "conversation_token": "same-token",
            },
        },
        {
            "conversation_token": "same-token",
            "_meta": {
                "com.dtxwiki/conversationToken": "same-token",
                "conversation_token": "same-token",
            },
        },
    ),
)
def test_requested_conversation_token_accepts_identical_carriers(arguments):
    assert runner_module._requested_conversation_token(arguments) == "same-token"


@pytest.mark.parametrize(
    ("arguments", "expected_carriers"),
    (
        (
            {
                "conversation_token": "plain-token",
                "_meta": {
                    "com.dtxwiki/conversationToken": "namespaced-token",
                },
            },
            (
                "conversation_token",
                '_meta["com.dtxwiki/conversationToken"]',
            ),
        ),
        (
            {
                "conversation_token": "plain-token",
                "_meta": {"conversation_token": "legacy-token"},
            },
            ("conversation_token", "_meta.conversation_token"),
        ),
        (
            {
                "_meta": {
                    "com.dtxwiki/conversationToken": "namespaced-token",
                    "conversation_token": "legacy-token",
                },
            },
            (
                '_meta["com.dtxwiki/conversationToken"]',
                "_meta.conversation_token",
            ),
        ),
    ),
)
def test_conflicting_conversation_token_carriers_are_validation_failed(
    page_service,
    mcp_default_actor,
    arguments,
    expected_carriers,
):
    async def run():
        return await call_tool_async(
            "list_my_workspaces",
            arguments,
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="carrier-conflict-test",
                scopes=("*",),
            ),
            page_service=page_service,
        )

    result = anyio.run(run)
    payload = json.loads(result.content[0].text)
    envelope = payload["envelope"]

    assert result.isError is True
    assert envelope["error_code"] == "validation_failed"
    assert envelope["error_message"] == (
        "Conversation token carriers have conflicting values: "
        f"{', '.join(expected_carriers)}."
    )
    assert "plain-token" not in result.content[0].text
    assert "namespaced-token" not in result.content[0].text
    assert "legacy-token" not in result.content[0].text


class _EqualToken(str):
    pass


def test_call_tool_async_returns_text_content_with_envelope(
    page_service, mcp_default_actor
):
    async def run():
        return await call_tool_async(
            "does_not_exist",
            {},
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="test-mcp",
            ),
            page_service=page_service,
        )

    result = anyio.run(run)
    payload = json.loads(result.content[0].text)

    assert result.isError is True
    assert payload["envelope"]["result_status"] == "failed"
    assert payload["envelope"]["error_code"] == "unknown_tool"


def test_disabled_and_deleted_runtime_client_is_denied_before_trace_or_chat(
    page_service,
    mcp_default_actor,
):
    actor = _pat_actor(
        mcp_default_actor,
        actor_id="runtime-denied-pat",
        scopes=("*",),
    )
    assert actor.workspace_id is not None
    workspace_id = actor.workspace_id
    with page_service.session_factory.begin() as session:
        client = MCPClient(
            workspace_id=workspace_id,
            name="runtime-denied-client",
            client_type="generic",
            scopes=["*"],
            status="disabled",
            runtime_binding_key=runtime_binding_key_for_actor(actor),
        )
        session.add(client)
        session.flush()
        client_id = client.id
        expected_client_count = session.scalar(
            select(func.count()).select_from(MCPClient)
        )

    async def run():
        return await call_tool_async(
            "guide",
            {"workspace_id": str(workspace_id)},
            actor=actor,
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    disabled_result = anyio.run(run)
    disabled_payload = json.loads(disabled_result.content[0].text)
    assert disabled_result.isError is True
    assert disabled_payload["envelope"]["error_code"] == "mcp_client_disabled"

    # Audit rows are append-only and stamped by the host clock, which is not
    # monotonic, so which row belongs to which call cannot be read off their
    # timestamps. Name the first call's row now, before the second call adds its
    # own, and tell the two apart by identity afterwards.
    with page_service.session_factory.begin() as session:
        disabled_denials = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "mcp.client.runtime.denied")
        ).all()
        assert len(disabled_denials) == 1
        assert disabled_denials[0].payload["reason"] == "mcp_client_disabled"
        disabled_denial_id = disabled_denials[0].id
        persisted = session.get(MCPClient, client_id)
        persisted.deleted_at = utc_now()

    deleted_result = anyio.run(run)
    deleted_payload = json.loads(deleted_result.content[0].text)
    assert deleted_result.isError is True
    assert deleted_payload["envelope"]["error_code"] == "mcp_client_deleted"

    with page_service.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(MCPClient))
            == expected_client_count
        )
        assert session.scalar(select(func.count()).select_from(MCPToolCall)) == 0
        assert session.scalar(select(func.count()).select_from(McpChatSession)) == 0
        assert session.scalar(select(func.count()).select_from(AgentInputBundle)) == 0
        denials = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "mcp.client.runtime.denied")
        ).all()
        # One audit for each denied call: the first call's row, unchanged, and
        # exactly one more, added by the call made after the client was deleted.
        assert len(denials) == 2
        by_id = {row.id: row for row in denials}
        assert by_id[disabled_denial_id].payload["reason"] == "mcp_client_disabled"
        deleted_denials = [row for row in denials if row.id != disabled_denial_id]
        assert len(deleted_denials) == 1
        assert deleted_denials[0].payload["reason"] == "mcp_client_deleted"


@pytest.mark.parametrize("replica_state", ("missing", "revoked"))
def test_credential_replica_gap_is_denied_before_client_trace_or_chat(
    page_service,
    replica_state,
):
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        user = User(
            email=f"runner-replica-{replica_state}@example.com",
            password_hash="unused",
            display_name=f"Runner replica {replica_state}",
        )
        session.add(user)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        token = ApiToken(
            user_id=user.id,
            name=f"runner-replica-{replica_state}",
            token_lookup_hash=f"runner-replica-{replica_state}",
            hashed_token="unused",
            scopes=["mcp:read"],
        )
        session.add(token)
        session.flush()
        session.add(
            ApiTokenWorkspace(
                token_id=token.id,
                workspace_id=workspace.id,
                desired_scopes=["mcp:read"],
                revision=1,
                sync_state="synced",
            )
        )
        if replica_state == "revoked":
            session.add(
                WorkspaceCredentialGrant(
                    workspace_id=workspace.id,
                    credential_kind="pat",
                    credential_id=token.id,
                    user_id=user.id,
                    granted_scopes=["mcp:read"],
                    synced_revision=1,
                    revoked_at=utc_now(),
                )
            )
        actor = ActorContext(
            actor_kind="human",
            actor_id=str(user.id),
            scopes=("mcp:read",),
            workspace_role="owner",
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
            token_kind="api_token",
            token_id=token.id,
            token_workspace_ids=(workspace.id,),
            provenance="personal_access_token",
        )
        workspace_id = workspace.id
        expected_client_count = session.scalar(
            select(func.count()).select_from(MCPClient)
        )

    async def run(tool_name, arguments):
        return await call_tool_async(
            tool_name,
            arguments,
            actor=actor,
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    for tool_name, arguments in (
        ("guide", {"workspace_id": str(workspace_id)}),
        ("list_my_workspaces", {}),
    ):
        result = anyio.run(run, tool_name, arguments)
        payload = json.loads(result.content[0].text)
        assert result.isError is True
        assert payload["envelope"]["error_code"] == "grant_replica_missing"
        assert payload["envelope"]["result_status"] == "failed"
        assert payload["envelope"]["retryable"] is True

    with page_service.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(MCPClient))
            == expected_client_count
        )
        assert session.scalar(select(func.count()).select_from(MCPToolCall)) == 0
        assert session.scalar(select(func.count()).select_from(McpChatSession)) == 0


@pytest.mark.parametrize("tool_name", ("list_my_workspaces", "guide"))
def test_grant_removed_after_authentication_is_not_an_internal_error(
    page_service,
    mcp_default_actor,
    monkeypatch,
    tool_name,
):
    actor = _pat_actor(
        mcp_default_actor,
        actor_id="preflight-grant-removal",
        scopes=("mcp:read",),
    )
    workspace_a = actor.workspace_id
    assert workspace_a is not None
    with page_service.session_factory.begin() as session:
        workspace_b = seed_default_workspace(
            session,
            slug="preflight-grant-removal-b",
            title="Preflight grant removal B",
        ).id
        grant_workspace_membership(
            session,
            user_id=actor.user_id,
            workspace_id=workspace_b,
            role="owner",
        )
        session.add_all(
            [
                ApiTokenWorkspace(
                    token_id=actor.token_id,
                    workspace_id=workspace_b,
                    desired_scopes=["mcp:read"],
                    revision=1,
                    sync_state="synced",
                ),
                WorkspaceCredentialGrant(
                    workspace_id=workspace_b,
                    credential_kind="pat",
                    credential_id=actor.token_id,
                    user_id=actor.user_id,
                    granted_scopes=["mcp:read"],
                    synced_revision=1,
                ),
            ]
        )
    actor = replace(actor, token_workspace_ids=(workspace_a, workspace_b))
    protected_models = (MCPClient, MCPToolCall, McpChatSession)
    with page_service.session_factory() as session:
        before = {
            model: session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.workspace_id == workspace_a)
            )
            for model in protected_models
        }

    original_preflight = runner_module._scope_runtime_actor_to_live_grant
    removed = False

    def _remove_a_before_live_grant_check(**kwargs):
        nonlocal removed
        if kwargs["workspace_id"] == workspace_a and not removed:
            # The actor already passed snapshot/membership checks. Commit the
            # edit separately before the real preflight reads the live grant.
            with page_service.session_factory.begin() as session:
                grant = session.get(ApiTokenWorkspace, (actor.token_id, workspace_a))
                assert grant is not None
                grant.removed_at = utc_now()
            removed = True
        return original_preflight(**kwargs)

    monkeypatch.setattr(
        runner_module,
        "_scope_runtime_actor_to_live_grant",
        _remove_a_before_live_grant_check,
    )

    async def run():
        return await call_tool_async(
            tool_name,
            {"workspace_id": str(workspace_a)} if tool_name == "guide" else {},
            actor=actor,
            page_service=page_service,
        )

    result = anyio.run(run)
    assert removed is True
    payload = result.structuredContent
    assert payload is not None
    if tool_name == "list_my_workspaces":
        assert result.isError is False
        assert payload["envelope"]["result_status"] == "succeeded"
        assert [workspace["id"] for workspace in payload["data"]["workspaces"]] == [
            str(workspace_b)
        ]
    else:
        assert result.isError is True
        assert payload["envelope"]["result_status"] == "rejected_by_policy"
        assert payload["envelope"]["error_code"] == "token_workspace_denied"
        assert payload["envelope"]["retryable"] is False
    with page_service.session_factory() as session:
        for model, expected_count in before.items():
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_a)
                )
                == expected_count
            )


def test_missing_workspace_target_never_reads_page_directory(
    page_service,
    monkeypatch,
    mcp_default_actor,
):
    existing_page = page_service.write_page(
        workspace_slug="default",
        slug="runner-missing-workspace-target",
        title="Runner missing workspace target",
        body_md="# Existing\n",
        actor_kind="system",
        actor_id="runner-missing-workspace-target-test",
        actor_provenance="internal",
    )
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        workspace_id = workspace.id

    directory_lookups = []

    def _spy_page_lookup(_self, _session, page_id):
        directory_lookups.append(("page", page_id))
        return workspace_id if page_id == existing_page.id else None

    def _spy_slug_lookup(_self, _session, workspace_slug):
        directory_lookups.append(("slug", workspace_slug))
        return workspace_id if workspace_slug == "default" else None

    monkeypatch.setattr(
        "backend.app.container.WorkspaceDirectory.workspace_id_by_context_page",
        _spy_page_lookup,
    )
    monkeypatch.setattr(
        "backend.app.container.WorkspaceDirectory.workspace_id_by_slug",
        _spy_slug_lookup,
    )
    actor = _pat_actor(
        mcp_default_actor,
        actor_id="missing-workspace-target-client",
        scopes=("*",),
    )

    async def run(page_id, workspace_slug):
        return await call_tool_async(
            "upload_image",
            {
                "filename": "diagram.png",
                "mime_type": "image/png",
                "content_base64": "AA==",
                "marker_uuid": str(uuid4()),
                "page_id": str(page_id),
                "workspace_slug": workspace_slug,
                "editing_version_id": str(uuid4()),
                "conversation_token": "new",
            },
            actor=actor,
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    results = []
    for page_id, workspace_slug in (
        (existing_page.id, "default"),
        (uuid4(), "does-not-exist"),
    ):
        result = anyio.run(run, page_id, workspace_slug)
        payload = json.loads(result.content[0].text)
        envelope = payload["envelope"]
        results.append(
            (
                result.isError,
                envelope["error_code"],
                envelope["error_message"],
                envelope["retryable"],
            )
        )

    assert results == [
        (True, "validation_failed", "workspace_id: Field required", False),
        (True, "validation_failed", "workspace_id: Field required", False),
    ]
    assert directory_lookups == []


def test_list_my_workspaces_cannot_bypass_disabled_runtime_client(
    page_service, mcp_default_actor
):
    actor = _pat_actor(
        mcp_default_actor,
        actor_id="disabled-directory-pat",
        scopes=("*",),
    )
    assert actor.workspace_id is not None
    workspace_id = actor.workspace_id
    with page_service.session_factory.begin() as session:
        client = MCPClient(
            workspace_id=workspace_id,
            name="disabled-directory-client",
            client_type="generic",
            scopes=["*"],
            status="disabled",
            runtime_binding_key=runtime_binding_key_for_actor(actor),
        )
        session.add(client)
        session.flush()
        client_id = client.id

    async def run():
        return await call_tool_async(
            "list_my_workspaces",
            {},
            actor=actor,
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    result = anyio.run(run)
    payload = json.loads(result.content[0].text)
    assert result.isError is True
    assert payload["envelope"]["error_code"] == "mcp_client_disabled"

    with page_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MCPToolCall)) == 0
        assert session.scalar(select(func.count()).select_from(McpChatSession)) == 0
        assert session.scalar(select(func.count()).select_from(AgentInputBundle)) == 0
        denials = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "mcp.client.runtime.denied",
                AuditEvent.target_id == client_id,
            )
        ).all()
        assert [row.payload["reason"] for row in denials] == ["mcp_client_disabled"]


def test_oauth_runtime_binding_uses_stable_grant_identity():
    grant_id = UUID("00000000-0000-0000-0000-000000000777")
    user_id = UUID("00000000-0000-0000-0000-000000000778")
    first = ActorContext(
        actor_kind="human",
        actor_id="oauth-user",
        user_id=user_id,
        token_kind="oauth_jwt",
        token_id=grant_id,
        client_id="dynamic-client",
    )
    rotated = ActorContext(
        actor_kind="human",
        actor_id="oauth-user",
        user_id=user_id,
        token_kind="oauth_jwt",
        token_id=grant_id,
        client_id="dynamic-client",
    )

    assert runtime_binding_key_for_actor(first) == f"oauth_grant:{grant_id}"
    assert runtime_binding_key_for_actor(rotated) == runtime_binding_key_for_actor(
        first
    )


@pytest.mark.parametrize(
    "actor",
    [
        ActorContext(actor_kind="mcp_client", actor_id="retired-direct-client"),
        ActorContext(
            actor_kind="human",
            actor_id="browser-user",
            user_id=UUID("00000000-0000-0000-0000-000000000779"),
            token_kind="session",
        ),
        ActorContext(
            actor_kind="human",
            actor_id="incomplete-oauth-user",
            user_id=UUID("00000000-0000-0000-0000-000000000780"),
            token_kind="oauth_jwt",
            client_id="oauth-client-without-grant",
        ),
    ],
)
def test_runtime_binding_rejects_non_bearer_and_incomplete_identities(actor):
    with pytest.raises(PermissionError, match="unsupported_mcp_credential"):
        runtime_binding_key_for_actor(actor)


def test_call_tool_async_unexpected_dispatch_error_is_sanitized_and_traced(
    page_service, monkeypatch, mcp_default_actor
):
    """An uncaught dispatch exception must not leak ``str(exc)`` to the
    client (SQLAlchemy errors carry the SQL statement + bound parameters)
    and must still land a failed tool-call trace in the MCP-chats audit
    trail instead of vanishing into the SDK's exception fallback.
    """
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace_id = workspace.id

    def _boom(*_args, **_kwargs):
        raise RuntimeError("INSERT INTO pages (secret) VALUES ('do-not-leak')")

    monkeypatch.setattr(runner_module, "dispatch", _boom)

    async def run():
        return await call_tool_async(
            "search",
            {"workspace_id": str(workspace_id), "query": "boom"},
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="trace-fault-client",
                scopes=("*",),
            ),
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    result = anyio.run(run)
    payload = json.loads(result.content[0].text)

    assert result.isError is True
    assert payload["envelope"]["error_code"] == "internal_error"
    assert payload["envelope"]["result_status"] == "failed"
    assert payload["envelope"]["conversation_token"].startswith("conv_")
    assert "do-not-leak" not in result.content[0].text

    with page_service.session_factory() as session:
        trace = session.scalar(
            select(MCPToolCall).where(MCPToolCall.tool_name == "search")
        )
    assert trace is not None
    assert trace.result_status == "failed"
    assert trace.error_code == "internal_error"
    assert "do-not-leak" not in str(trace.result_summary)


class _StubResponse:
    """Minimal stand-in for a handler's Pydantic response object."""

    def __init__(self, payload):
        self._payload = payload

    def model_dump(self, mode="json"):
        return self._payload


def test_call_tool_async_contract_violating_result_fails_closed(
    page_service, monkeypatch, mcp_default_actor
):
    """A handler payload that violates its own declared output contract must
    become an internal_error result, not a success. ``isError`` is derived
    from the handler's own ``result_status``, and no client-side check exists
    (outputSchema is deliberately off the wire), so serving it would present a
    broken payload as a valid success with nothing anywhere to catch it.
    """
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace_id = workspace.id

    sentinel = "CONTRACT-VIOLATION-MUST-NOT-REACH-CLIENT"
    violating = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "request_id": "44444444-4444-4444-4444-444444444444",
            "result_status": "succeeded",
            "warnings": [],
        },
        # The tool output schemas declare additionalProperties: false.
        "leaked_internal_field": sentinel,
    }
    monkeypatch.setattr(
        runner_module, "dispatch", lambda *_args, **_kwargs: _StubResponse(violating)
    )
    metrics: list[str] = []
    monkeypatch.setattr(
        runner_module,
        "inc_mcp_tool_call",
        lambda *, tool, status: metrics.append(status),
    )

    async def run():
        return await call_tool_async(
            "search",
            {"workspace_id": str(workspace_id), "query": "contract"},
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="contract-violation-client",
                scopes=("*",),
            ),
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    result = anyio.run(run)
    payload = json.loads(result.content[0].text)

    assert result.isError is True
    assert payload["envelope"]["error_code"] == "internal_error"
    assert payload["envelope"]["result_status"] == "failed"
    assert "leaked_internal_field" not in result.content[0].text
    assert sentinel not in result.content[0].text

    with page_service.session_factory() as session:
        trace = session.scalar(
            select(MCPToolCall).where(MCPToolCall.tool_name == "search")
        )
    assert trace is not None
    assert trace.result_status == "failed"
    assert trace.error_code == "internal_error"
    # The success path sets metric_status "ok" immediately before the contract
    # check, so a fail-closed call must reset it or it is counted as a success.
    assert metrics == ["error"]


def test_call_tool_async_fails_closed_when_the_wire_copy_violates_the_contract(
    page_service, monkeypatch, mcp_default_actor
):
    """The payload that is validated must be the payload that is sent. Here the
    handler's own result conforms and only the wire transform is defective, so
    a check that ran solely on the pre-redaction payload would let it through
    in complete silence.
    """
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace_id = workspace.id

    sentinel = "WIRE-CORRUPTION-MUST-NOT-REACH-CLIENT"
    conforming = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "request_id": "55555555-5555-5555-5555-555555555555",
            "result_status": "succeeded",
            "warnings": [],
        }
    }
    monkeypatch.setattr(
        runner_module, "dispatch", lambda *_args, **_kwargs: _StubResponse(conforming)
    )

    original = runner_module.redact_tool_capture

    def _corrupting(name, arguments, payload):
        trace_arguments, redacted = original(name, arguments, payload)
        # Only the success path is corrupted: the fail-closed replacement
        # payload runs through this same function, and corrupting that too
        # would test the sentinel out of the assertion below.
        if redacted.get("envelope", {}).get("result_status") != "succeeded":
            return trace_arguments, redacted
        return trace_arguments, {**redacted, "leaked_by_redaction": sentinel}

    monkeypatch.setattr(runner_module, "redact_tool_capture", _corrupting)

    async def run():
        return await call_tool_async(
            "search",
            {"workspace_id": str(workspace_id), "query": "wire"},
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="wire-contract-client",
                scopes=("*",),
            ),
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    with captured_log_messages("dtxwiki.mcp", logging.ERROR) as messages:
        result = anyio.run(run)
    payload = json.loads(result.content[0].text)

    assert result.isError is True
    assert payload["envelope"]["error_code"] == "internal_error"
    assert "leaked_by_redaction" not in result.content[0].text
    assert sentinel not in result.content[0].text
    assert any("phase=wire" in message for message in messages)


def test_call_tool_async_does_not_validate_its_own_fail_closed_replacement(
    page_service, monkeypatch, mcp_default_actor
):
    """Re-entrancy guard. The ``except`` handlers build the replacement payload
    through the same capture helper, and a raise from inside an except block
    has no barrier above it -- it would escape ``call_tool_async`` entirely and
    hand the SDK the raw-exception fallback this barrier exists to prevent.
    So the fail-closed result must never itself be validated.
    """
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace_id = workspace.id

    conforming = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "request_id": "66666666-6666-6666-6666-666666666666",
            "result_status": "succeeded",
            "warnings": [],
        }
    }
    monkeypatch.setattr(
        runner_module, "dispatch", lambda *_args, **_kwargs: _StubResponse(conforming)
    )

    original = runner_module.redact_tool_capture

    def _always_corrupting(name, arguments, payload):
        # Unconditional, unlike the wire-phase test above: the error path's own
        # payload is corrupted too, which is exactly the condition that would
        # make a validating except handler raise out of the function.
        trace_arguments, redacted = original(name, arguments, payload)
        return trace_arguments, {**redacted, "leaked_everywhere": True}

    monkeypatch.setattr(runner_module, "redact_tool_capture", _always_corrupting)

    async def run():
        return await call_tool_async(
            "search",
            {"workspace_id": str(workspace_id), "query": "reentrancy"},
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="reentrancy-client",
                scopes=("*",),
            ),
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    result = anyio.run(run)

    assert result.isError is True
    assert json.loads(result.content[0].text)["envelope"]["error_code"] == (
        "internal_error"
    )


def test_call_tool_async_preflight_db_error_is_sanitized(
    page_service, monkeypatch, mcp_default_actor
):
    """H-05: the preflight (workspace/authz/shard/conversation) now runs INSIDE
    the sanitize barrier, so a control-/data-plane DB error there must not leak
    ``str(exc)`` (SQL + bound params) and must return a sanitized internal_error
    envelope like a dispatch failure does."""
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace_id = workspace.id

    def _boom(*_args, **_kwargs):
        raise RuntimeError("SELECT secret FROM control WHERE token = 'do-not-leak'")

    monkeypatch.setattr(runner_module, "_trace_workspace_target", _boom)

    async def run():
        return await call_tool_async(
            "search",
            {"workspace_id": str(workspace_id), "query": "boom"},
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="preflight-fault-client",
                scopes=("*",),
            ),
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    result = anyio.run(run)
    payload = json.loads(result.content[0].text)

    assert result.isError is True
    assert payload["envelope"]["error_code"] == "internal_error"
    assert payload["envelope"]["result_status"] == "failed"
    assert "do-not-leak" not in result.content[0].text


def test_slim_envelope_for_wire_keeps_model_facing_fields_and_folds_the_rest():
    payload = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "request_id": "11111111-1111-1111-1111-111111111111",
            "result_status": "succeeded",
            "workspace_id": "22222222-2222-2222-2222-222222222222",
            "actor": {"kind": "human", "id": "u1"},
            "actor_kind": "human",
            "actor_id": "u1",
            "result_summary": "ok",
            "audit_event_id": None,
            "activity_ref": None,
            "provider": "unknown",
            "model_id": "unknown",
            "model_version": None,
            "params_summary": {},
            "client_self_reported": False,
            "error_code": None,
            "error_message": None,
            "error_details": [],
            "retryable": None,
            "review_item_id": None,
            "warnings": [],
            "conversation_token": "conv-1",
        },
        "data": {"stuff": 1},
    }

    wire = runner_module._slim_envelope_for_wire(payload)

    assert set(wire["envelope"]) == runner_module._WIRE_ENVELOPE_FIELDS | {"meta"}
    assert wire["envelope"]["result_status"] == "succeeded"
    assert wire["envelope"]["conversation_token"] == "conv-1"
    assert wire["envelope"]["meta"]["schema_version"] == "llmwiki.mcp.v1"
    assert wire["envelope"]["meta"]["workspace_id"] == (
        "22222222-2222-2222-2222-222222222222"
    )
    assert wire["envelope"]["meta"]["actor_kind"] == "human"
    # Non-envelope keys (the tool's own result data) are untouched.
    assert wire["data"] == {"stuff": 1}


def test_slim_envelope_for_wire_is_a_no_op_when_envelope_is_absent():
    payload = {"pages": []}
    assert runner_module._slim_envelope_for_wire(payload) == payload


def test_wire_slimmed_envelope_validates_against_the_checked_in_schema():
    """Regression for the 79420555..3ea8580d review's High #7: the
    checked-in shared/schemas/mcp/v1/*.json contract must describe what a
    client actually receives on the wire, not the pre-trim shape. Before
    MCPResultEnvelope grew a real `meta` field, this raised
    ``ValidationError: Additional properties are not allowed ('meta' was
    unexpected)`` -- the schema and the wire had silently diverged."""
    import jsonschema

    from backend.scripts.export_mcp_schemas import build_mcp_schemas

    payload = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "request_id": "11111111-1111-1111-1111-111111111111",
            "result_status": "succeeded",
            "workspace_id": "22222222-2222-2222-2222-222222222222",
            "actor": {"kind": "human", "id": "u1"},
            "actor_kind": "human",
            "actor_id": "u1",
            "result_summary": "ok",
            "audit_event_id": None,
            "activity_ref": None,
            "provider": "unknown",
            "model_id": "unknown",
            "model_version": None,
            "params_summary": {},
            "client_self_reported": False,
            "error_code": None,
            "error_message": None,
            "error_details": [],
            "retryable": None,
            "review_item_id": None,
            "warnings": [],
            "conversation_token": "conv-1",
        },
        "data": None,
    }

    wire = runner_module._slim_envelope_for_wire(payload)

    schema = build_mcp_schemas()["guide"]["output"]
    jsonschema.validate(instance=wire, schema=schema)


def test_capture_trace_and_wire_payload_keeps_full_envelope_for_trace():
    payload = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "result_status": "succeeded",
            "workspace_id": "33333333-3333-3333-3333-333333333333",
            "provider": "unknown",
            "warnings": [],
        }
    }

    trace_arguments, trace_payload, wire_payload = (
        runner_module._capture_trace_and_wire_payload("guide", {}, payload)
    )

    # The trace copy retains every envelope field -- mcp_trace_capture.py's
    # field reads (audit_event_id, provider, workspace_id, ...) must keep
    # working unchanged.
    assert trace_payload["envelope"]["workspace_id"] == (
        "33333333-3333-3333-3333-333333333333"
    )
    assert trace_payload["envelope"]["provider"] == "unknown"
    assert trace_payload["envelope"]["schema_version"] == "llmwiki.mcp.v1"
    # The wire copy is slimmed.
    assert "workspace_id" not in wire_payload["envelope"]
    assert wire_payload["envelope"]["meta"]["workspace_id"] == (
        "33333333-3333-3333-3333-333333333333"
    )
    assert trace_arguments == {}


def test_call_tool_async_fails_closed_on_a_violation_the_wire_phase_cannot_see(
    page_service, monkeypatch, mcp_default_actor
):
    """Pins the HANDLER-phase call site specifically.

    The other two fail-closed tests inject a top-level extra key, which
    survives slimming and so is caught a second time by the wire phase --
    deleting the handler-phase call leaves them green. Here the violation is
    in ``params_summary``, one of the 14 envelope fields
    ``_slim_envelope_for_wire`` folds into ``envelope.meta``, typed
    ``dict[str, Any] | None`` where any JSON is legal. The wire phase is
    provably blind to it (asserted below), so the handler phase is the only
    gate that can catch a handler emitting a malformed audit envelope.
    """
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace_id = workspace.id

    violating = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "request_id": "77777777-7777-7777-7777-777777777777",
            "result_status": "succeeded",
            # params_summary is object|null; client_self_reported is boolean.
            "params_summary": "not-a-dict",
            "client_self_reported": "yes",
            "warnings": [],
        }
    }

    # The wire phase genuinely cannot see this: both fields land in
    # envelope.meta, which accepts arbitrary JSON. If this ever starts
    # raising, this test has stopped pinning what it claims to pin.
    validate_tool_output(
        "search",
        runner_module._slim_envelope_for_wire(copy.deepcopy(violating)),
        phase="wire",
    )

    monkeypatch.setattr(
        runner_module, "dispatch", lambda *_args, **_kwargs: _StubResponse(violating)
    )

    async def run():
        return await call_tool_async(
            "search",
            {"workspace_id": str(workspace_id), "query": "folded"},
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="folded-violation-client",
                scopes=("*",),
            ),
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    with captured_log_messages("dtxwiki.mcp", logging.ERROR) as messages:
        result = anyio.run(run)
    payload = json.loads(result.content[0].text)

    assert result.isError is True
    assert payload["envelope"]["error_code"] == "internal_error"
    assert payload["envelope"]["result_status"] == "failed"
    assert payload["envelope"]["meta"].get("params_summary") != "not-a-dict"
    assert any(
        "phase=handler" in message and "$.envelope.params_summary" in message
        for message in messages
    )


def test_runtime_denial_result_is_slimmed_and_wire_contract_clean(
    page_service, mcp_default_actor
):
    """The ``MCPClientRuntimeDenied`` return is a served tool result like any
    other: it must carry the slimmed 9-field envelope, not the full 23-field
    audit envelope. Before this was routed through the capture helper it was
    the one return path validated in neither phase, and the wire-phase schema
    classifies the un-slimmed payload it served as a hard violation.
    """
    actor = _pat_actor(
        mcp_default_actor,
        actor_id="wire-shape-denied-pat",
        scopes=("*",),
    )
    assert actor.workspace_id is not None
    workspace_id = actor.workspace_id
    with page_service.session_factory.begin() as session:
        client = MCPClient(
            workspace_id=workspace_id,
            name="wire-shape-denied-client",
            client_type="generic",
            scopes=["*"],
            status="disabled",
            runtime_binding_key=runtime_binding_key_for_actor(actor),
        )
        session.add(client)
        session.flush()

    async def run():
        return await call_tool_async(
            "guide",
            {"workspace_id": str(workspace_id)},
            actor=actor,
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    result = anyio.run(run)
    payload = json.loads(result.content[0].text)

    assert result.isError is True
    assert payload["envelope"]["error_code"] == "mcp_client_disabled"
    assert set(payload["envelope"]) == runner_module._WIRE_ENVELOPE_FIELDS | {"meta"}
    # The audit fields are folded, not dropped -- and not repeated at the top.
    assert payload["envelope"]["meta"]["schema_version"] == "llmwiki.mcp.v1"
    # The served shape satisfies the contract the wire phase enforces.
    validate_tool_output("guide", payload, phase="wire")
    # structuredContent and the text body must be the same slimmed object.
    assert result.structuredContent == payload
    # Guard against this assertion silently becoming vacuous: the un-slimmed
    # payload this path used to serve really is a wire-phase violation.
    with pytest.raises(MCPOutputContractViolation):
        validate_tool_output(
            "guide",
            {"envelope": {**payload["envelope"], **payload["envelope"]["meta"]}},
            phase="wire",
        )

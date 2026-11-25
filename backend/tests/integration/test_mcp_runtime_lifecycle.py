from urllib.parse import parse_qs, urlparse

import jwt
from sqlalchemy import func, select

from backend.models.audit import AuditEvent
from backend.models.bundle import AgentInputBundle
from backend.models.mcp_chat import McpChatSession
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient
from backend.services.oauth_service import challenge_s256
from backend.tests.integration._cross_transport_helpers import (
    issue_oauth_access_token,
    issue_personal_api_token,
    make_client,
    seed_viewer_target_with_restricted_library_item,
)


def _mcp_post(client, bearer: str, method: str, *, params=None, request_id=1):
    return client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            **({"params": params} if params is not None else {}),
        },
    )


def _assert_protocol_lifecycle_error(response, expected: str) -> None:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["error"]["data"]["error_code"] == expected, payload


def _assert_tool_lifecycle_error(response, expected: str) -> None:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert "structuredContent" in result, response.text
    structured = result["structuredContent"]
    assert structured["envelope"]["error_code"] == expected, structured
    assert structured["envelope"]["result_status"] == "rejected_by_policy"


def test_http_pat_disable_reactivate_and_tombstone_has_no_runtime_side_effects(
    page_service,
):
    client = make_client(page_service)
    pat = issue_personal_api_token(client)

    first = _mcp_post(client, pat, "tools/list")
    assert first.status_code == 200, first.text
    assert "result" in first.json()

    with page_service.session_factory.begin() as session:
        runtime_client = session.scalar(
            select(MCPClient).where(
                MCPClient.runtime_binding_key.like("api_token:%")
            )
        )
        assert runtime_client is not None
        runtime_client.status = "disabled"
        runtime_client_id = runtime_client.id
        client_count = session.scalar(select(func.count()).select_from(MCPClient))

    _assert_protocol_lifecycle_error(
        _mcp_post(client, pat, "tools/list", request_id=2),
        "mcp_client_disabled",
    )
    _assert_tool_lifecycle_error(
        _mcp_post(
            client,
            pat,
            "tools/call",
            params={"name": "list_my_workspaces", "arguments": {}},
            request_id=3,
        ),
        "mcp_client_disabled",
    )

    with page_service.session_factory.begin() as session:
        session.get(MCPClient, runtime_client_id).status = "active"
    reactivated = _mcp_post(client, pat, "tools/list", request_id=4)
    assert "result" in reactivated.json(), reactivated.text

    with page_service.session_factory.begin() as session:
        runtime_client = session.get(MCPClient, runtime_client_id)
        runtime_client.status = "disabled"
        from backend.models.base import utc_now

        runtime_client.deleted_at = utc_now()

    _assert_protocol_lifecycle_error(
        _mcp_post(client, pat, "tools/list", request_id=5),
        "mcp_client_deleted",
    )

    with page_service.session_factory() as session:
        persisted_client_count = session.scalar(
            select(func.count()).select_from(MCPClient)
        )
        assert persisted_client_count == client_count
        assert session.scalar(select(func.count()).select_from(MCPToolCall)) == 0
        assert session.scalar(select(func.count()).select_from(McpChatSession)) == 0
        assert session.scalar(select(func.count()).select_from(AgentInputBundle)) == 0
        reasons = session.scalars(
            select(AuditEvent.payload).where(
                AuditEvent.action == "mcp.client.runtime.denied",
                AuditEvent.target_id == runtime_client_id,
            )
        ).all()
        assert [payload["reason"] for payload in reasons] == [
            "mcp_client_disabled",
            "mcp_client_disabled",
            "mcp_client_deleted",
        ]


def test_http_oauth_refresh_and_all_discovery_surfaces_keep_disabled_binding(
    page_service,
):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Lifecycle refresh client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    verifier = "v" * 64
    state = "lifecycle-refresh-0123456789abcdef"
    authorize = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": state,
        "workspace_id": workspace_id,
    }
    assert client.get("/oauth/authorize", params=authorize).status_code == 200
    # P10.3 T3: POST submits the checklist's workspace_ids (repeatable), not
    # the singular workspace_id GET hint.
    post_body = {k: v for k, v in authorize.items() if k != "workspace_id"}
    post_body["workspace_ids"] = workspace_id
    allowed = client.post(
        "/oauth/authorize",
        data={**post_body, "decision": "allow"},
        follow_redirects=False,
    )
    assert allowed.status_code == 303, allowed.text
    code = parse_qs(urlparse(allowed.headers["location"]).query)["code"][0]
    issued = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert issued.status_code == 200, issued.text
    access_token = issued.json()["access_token"]
    refresh_token = issued.json()["refresh_token"]
    grant_id = jwt.decode(
        access_token,
        options={"verify_signature": False},
    )["grant_id"]

    assert "result" in _mcp_post(client, access_token, "tools/list").json()
    with page_service.session_factory.begin() as session:
        runtime_client = session.scalar(
            select(MCPClient).where(
                MCPClient.runtime_binding_key == f"oauth_grant:{grant_id}"
            )
        )
        assert runtime_client is not None
        runtime_client.status = "disabled"
        runtime_client_id = runtime_client.id
        client_count = session.scalar(select(func.count()).select_from(MCPClient))

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    rotated_access_token = refreshed.json()["access_token"]
    rotated_grant_id = jwt.decode(
        rotated_access_token,
        options={"verify_signature": False},
    )["grant_id"]
    assert rotated_grant_id == grant_id

    for request_id, method in enumerate(
        (
            "tools/list",
            "prompts/list",
            "resources/list",
            "resources/templates/list",
        ),
        start=10,
    ):
        _assert_protocol_lifecycle_error(
            _mcp_post(
                client,
                rotated_access_token,
                method,
                request_id=request_id,
            ),
            "mcp_client_disabled",
        )

    for request_id, (method, params) in enumerate(
        (
            (
                "prompts/get",
                {
                    "name": "dtxwiki.context_workflow",
                    "arguments": {"workspace_id": workspace_id},
                },
            ),
            (
                "resources/read",
                {"uri": f"dtxwiki://workspaces/{workspace_id}/guide"},
            ),
        ),
        start=14,
    ):
        _assert_protocol_lifecycle_error(
            _mcp_post(
                client,
                rotated_access_token,
                method,
                params=params,
                request_id=request_id,
            ),
            "mcp_client_disabled",
        )

    _assert_tool_lifecycle_error(
        _mcp_post(
            client,
            rotated_access_token,
            "tools/call",
            params={"name": "list_my_workspaces", "arguments": {}},
            request_id=20,
        ),
        "mcp_client_disabled",
    )

    with page_service.session_factory() as session:
        persisted_client_count = session.scalar(
            select(func.count()).select_from(MCPClient)
        )
        assert persisted_client_count == client_count
        assert session.get(MCPClient, runtime_client_id).status == "disabled"
        assert session.scalar(select(func.count()).select_from(MCPToolCall)) == 0
        assert session.scalar(select(func.count()).select_from(McpChatSession)) == 0
        assert session.scalar(select(func.count()).select_from(AgentInputBundle)) == 0


def test_http_oauth_nondefault_workspace_lists_are_lifecycle_guarded(page_service):
    """Exercise the repaired default-workspace mismatch over the real HTTP path."""

    client = make_client(page_service)
    target_workspace_id = seed_viewer_target_with_restricted_library_item(page_service)
    access_token = issue_oauth_access_token(client, target_workspace_id)
    grant_id = jwt.decode(
        access_token,
        options={"verify_signature": False},
    )["grant_id"]
    assert "result" in _mcp_post(client, access_token, "prompts/list").json()

    with page_service.session_factory.begin() as session:
        runtime_client = session.scalar(
            select(MCPClient).where(
                MCPClient.runtime_binding_key == f"oauth_grant:{grant_id}"
            )
        )
        assert runtime_client is not None
        runtime_client.status = "disabled"

    for request_id, method in enumerate(
        ("prompts/list", "resources/list", "resources/templates/list"),
        start=30,
    ):
        _assert_protocol_lifecycle_error(
            _mcp_post(client, access_token, method, request_id=request_id),
            "mcp_client_disabled",
        )

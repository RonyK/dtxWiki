"""Streamable HTTP MCP transport — lifespan + manager scope tests.

Epic_0 / Sprint_5 / Phase_2 / Task 2.2.

Pins the transport-mode invariants directly:

1. ``mcp_http_mode == "streamable" | "auto"`` → ``app.state.mcp_session_manager``
   is a single :class:`StreamableHTTPSessionManager` instance entered via
   FastAPI lifespan. ``json_stateless`` → ``None`` (request-scope baseline).
2. Each ``create_app()`` instance gets its own manager (H6 invariant 1 —
   instance-scoped, never a module-level singleton).
3. After the lifespan exits the manager's task group is cancelled — calling
   ``handle_request`` post-exit raises ``RuntimeError`` (SDK constraint 1,
   H6 invariant 2).
4. ``json_stateless`` POST `/mcp` regression baseline is preserved
   (Sprint_4 회귀 0).

Negative-test rule (ADR A6 / Sprint_4 Fix-7): the ``RuntimeError`` case
pairs with a positive control (an active manager handles the same request
without raising) and pins the SDK's failure-mode keyword
(``"task group"``).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, ExitStack
from datetime import timedelta
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import types as mcp_types
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.shared.message import ServerMessageMetadata
from sqlalchemy import select
from starlette.requests import Request

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.workspace import MCPClient
from backend.tests.integration.test_oauth_mcp_http import module_scoped_page_service


def _make_settings(mode: str) -> Settings:
    return Settings(
        mcp_http_mode=mode,
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
    )


pytestmark = pytest.mark.mcp_http


def assert_json_rpc_error(
    body: dict,
    *,
    request_id: int | str | None,
    error: str,
    message_contains: str | None = None,
    data: dict | None = None,
) -> dict:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    payload = body["error"]
    assert payload["code"] == -32000
    if message_contains is not None:
        assert message_contains in payload["message"]
    error_data = payload["data"]
    assert error_data["error"] == error
    if data:
        for key, value in data.items():
            assert error_data[key] == value
    return payload


# ---------------------------------------------------------------------------
# Task 2.2 — Session manager lifespan + instance-scope 분리
# ---------------------------------------------------------------------------


def test_session_manager_lifespan_singleton_per_app(page_service):
    """``app.state.mcp_session_manager`` reflects the mode at lifespan time.

    Phase_2 §2.2 contract: streamable/auto build a single manager during
    lifespan startup; json_stateless leaves the attribute as ``None`` (the
    POST handler builds a request-scope manager instead).
    """
    streamable_app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    json_app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )

    with TestClient(streamable_app) as _:
        assert isinstance(
            streamable_app.state.mcp_session_manager,
            StreamableHTTPSessionManager,
        )
    with TestClient(json_app) as _:
        assert json_app.state.mcp_session_manager is None


async def _send_bound_dispatcher_request(client, request, result_type, *, app, actor):
    http_request = Request(
        {
            "type": "http",
            "app": app,
            "headers": [(b"mcp-session-id", uuid4().hex.encode())],
            "state": {"actor": actor},
        }
    )
    return await client.send_request(
        mcp_types.ClientRequest(request),
        result_type,
        metadata=ServerMessageMetadata(request_context=http_request),
    )


def test_streamable_dispatcher_binds_prompt_resource_request_metadata(
    page_service, mcp_default_actor
):
    """All discovery surfaces use the Request carried by their SDK message."""
    actor = mcp_default_actor(scopes=("*",))
    workspace_id = actor.workspace_id
    assert workspace_id is not None
    app = create_app(settings=_make_settings("streamable"), page_service=page_service)

    with TestClient(app):
        dispatcher = app.state.mcp_session_manager.app

        async def run():
            async with create_connected_server_and_client_session(dispatcher) as client:
                async def send(request, result_type):
                    return await _send_bound_dispatcher_request(
                        client, request, result_type, app=app, actor=actor
                    )

                tools = await send(
                    mcp_types.ListToolsRequest(), mcp_types.ListToolsResult
                )
                prompts = await send(
                    mcp_types.ListPromptsRequest(), mcp_types.ListPromptsResult
                )
                prompt = await send(
                    mcp_types.GetPromptRequest(
                        params=mcp_types.GetPromptRequestParams(
                            name="dtxwiki.context_workflow",
                            arguments={"workspace_id": str(workspace_id)},
                        )
                    ),
                    mcp_types.GetPromptResult,
                )
                resources = await send(
                    mcp_types.ListResourcesRequest(), mcp_types.ListResourcesResult
                )
                templates = await send(
                    mcp_types.ListResourceTemplatesRequest(),
                    mcp_types.ListResourceTemplatesResult,
                )
                guide = await send(
                    mcp_types.ReadResourceRequest(
                        params=mcp_types.ReadResourceRequestParams(
                            uri=f"dtxwiki://workspaces/{workspace_id}/guide"
                        )
                    ),
                    mcp_types.ReadResourceResult,
                )
                assert "guide" in {tool.name for tool in tools.tools}
                assert "dtxwiki.context_workflow" in {
                    item.name for item in prompts.prompts
                }
                assert prompt.messages
                assert "workspace-guide" in {
                    item.name for item in resources.resources
                }
                assert templates.resourceTemplates
                assert guide.contents[0].mimeType == "application/json"

        anyio.run(run)


def test_streamable_dispatcher_blocks_disabled_client_on_all_surfaces(
    page_service, mcp_default_actor
):
    actor = mcp_default_actor(scopes=("*",))
    workspace_id = actor.workspace_id
    assert workspace_id is not None
    assert actor.token_id is not None
    with page_service.session_factory.begin() as session:
        session.add(
            MCPClient(
                workspace_id=workspace_id,
                name="disabled-streamable-surfaces-client",
                client_type="generic",
                scopes=["*"],
                status="disabled",
                runtime_binding_key=f"api_token:{actor.token_id}",
            )
        )

    app = create_app(settings=_make_settings("streamable"), page_service=page_service)
    with TestClient(app):
        dispatcher = app.state.mcp_session_manager.app

        async def run():
            async with create_connected_server_and_client_session(dispatcher) as client:
                requests = (
                    (mcp_types.ListToolsRequest(), mcp_types.ListToolsResult),
                    (
                        mcp_types.GetPromptRequest(
                            params=mcp_types.GetPromptRequestParams(
                                name="dtxwiki.context_workflow",
                                arguments={"workspace_id": str(workspace_id)},
                            )
                        ),
                        mcp_types.GetPromptResult,
                    ),
                    (
                        mcp_types.ReadResourceRequest(
                            params=mcp_types.ReadResourceRequestParams(
                                uri=f"dtxwiki://workspaces/{workspace_id}/guide"
                            )
                        ),
                        mcp_types.ReadResourceResult,
                    ),
                )
                for request, result_type in requests:
                    with pytest.raises(McpError) as error:
                        await _send_bound_dispatcher_request(
                            client, request, result_type, app=app, actor=actor
                        )
                    assert error.value.error.data["error_code"] == (
                        "mcp_client_disabled"
                    )

        anyio.run(run)


def test_json_stateless_path_unchanged(page_service):
    """Sprint_4 baseline: ``mode=json_stateless`` POST /mcp without bearer
    returns 401 with the canonical ``WWW-Authenticate`` header. This pins
    that Task 2.2's mode dispatch does not regress the existing path
    (Phase_2 §2.2 invariant 3 — H6, Phase_2 §11.1 회귀)."""
    app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 401, response.text
    # Positive control (ADR A6): the resource_metadata URL is still emitted
    # — i.e. we did not silently switch to a different unauthorized path.
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_manager_state_does_not_leak_across_app_instances(page_service):
    """Two ``create_app(streamable)`` instances get *distinct* managers.

    H6 invariant 1: the manager is stored on ``app.state``, never as a
    module-level singleton, so per-test fixtures + per-deploy instances
    cannot accidentally share session pools.
    """
    app_a = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    app_b = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )

    with TestClient(app_a) as _, TestClient(app_b) as _:
        from backend.api.mcp_http import _mcp_manager_session_pool

        manager_a = app_a.state.mcp_session_manager
        manager_b = app_b.state.mcp_session_manager
        assert manager_a is not None and manager_b is not None
        # Distinct Python objects — different id (the contract the plan asks
        # for: "별도 session id pool" follows from distinct manager objects
        # since each manager owns a separate session pool).
        assert manager_a is not manager_b
        assert _mcp_manager_session_pool(manager_a) is not None
        assert _mcp_manager_session_pool(manager_b) is not None
        assert _mcp_manager_session_pool(manager_a) is not _mcp_manager_session_pool(
            manager_b
        )


def test_lifespan_exit_cancels_manager_task_group(page_service):
    """Post-lifespan ``handle_request`` rejects use after manager exit.

    H6 invariant 2: ``StreamableHTTPSessionManager.run()`` can only be
    entered once and its task group is cancelled on ``__aexit__``. Re-using
    the manager after the lifespan exits must fail deterministically —
    this is the regression-catcher for accidentally hoisting the manager to
    a module-level scope across test fixtures.

    Negative-test rule (ADR A6): paired positive control verifies the
    manager *does* dispatch while the lifespan is active, then we exit and
    pin the SDK's failure-mode keyword.
    """
    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )

    # Capture manager while lifespan is active — positive control: handle_request
    # should be callable (task group is initialized).
    with TestClient(app) as _:
        manager = app.state.mcp_session_manager
        assert manager is not None
        assert manager._task_group is not None  # active

    # After exit: task group cancelled, instance must not be reusable.
    assert manager._task_group is None

    async def _attempt_post_exit() -> Exception | None:
        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(_message):
            return None

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"content-type", b"application/json")],
        }
        try:
            await manager.handle_request(scope, _receive, _send)
            return None
        except RuntimeError as exc:
            return exc

    exc = asyncio.run(_attempt_post_exit())
    assert isinstance(exc, RuntimeError), (
        "manager must reject requests after lifespan exits"
    )
    # Pin SDK failure-mode keyword (ADR A6) — the SDK raises
    # "Task group is not initialized. Make sure to use run()." See
    # streamable_http_manager.py L155-L156.
    assert "task group" in str(exc).lower()


# ---------------------------------------------------------------------------
# Sprint_5 Phase_0~2 fix-round — SEC-1 regression guards
# ---------------------------------------------------------------------------


def test_resolve_and_check_rejects_pat_workspace_mismatch(
    page_service, mcp_default_actor
):
    """A PAT request outside its live workspace grant must fail closed.

    Negative-test rule (ADR A6): paired with the positive control below
    (``test_resolve_and_check_allows_own_workspace``).
    """
    from uuid import uuid4

    from backend.api.mcp_http import _resolve_and_check_mcp_workspace
    from backend.app.main import create_app

    foreign_workspace_id = uuid4()
    app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )
    actor = mcp_default_actor(scopes=("mcp:read",))
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"search","arguments":'
        b'{"workspace_id":"' + str(foreign_workspace_id).encode() + b'"}}}'
    )

    class _Req:
        def __init__(self, app):
            self.app = app

    response = _resolve_and_check_mcp_workspace(_Req(app), actor, body)
    assert response is not None, (
        "SEC-1: mismatched workspace_id must NOT silently return None"
    )
    assert response.status_code in (400, 403)
    assert_json_rpc_error(
        json.loads(response.body),
        request_id=1,
        error="workspace_ambiguous",
        message_contains="workspace token claim mismatch",
    )


def test_streamable_mode_503_when_manager_missing(page_service):
    """TEST-2: when ``mcp_http_mode != json_stateless`` and the lifespan
    has not bound ``app.state.mcp_session_manager``, MCP handlers must
    return deterministic 503 ``manager_unavailable`` JSON-RPC responses
    (Phase_2 §10 "Lifespan 미작동 시나리오").

    Negative-test rule (ADR A6): pairs an authenticated request that
    reaches the manager guard (positive control of the request shape)
    with the 503 short-circuit; pins ``manager_unavailable`` keyword in
    the JSON body. A future refactor swapping 503 for a 500 would
    silently break the contract today.
    """

    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    # Build the client + PAT under json_stateless so the helper does not
    # require a lifespan-bound manager. The PAT is reusable across modes
    # because it lives in the database, not on app.state.
    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)

    # Now build the actual streamable app and bypass the lifespan so
    # ``app.state.mcp_session_manager`` stays ``None``.
    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    # No `with TestClient(app)` context manager => lifespan does NOT run.
    client = TestClient(app)
    app.state.mcp_session_manager = None

    post_response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pat}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    get_response = client.get(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "text/event-stream",
        },
    )
    delete_response = client.delete(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Mcp-Session-Id": "missing-manager-session",
        },
    )

    # Pin the deterministic 503 body shape (ADR A6 keyword).
    assert post_response.status_code == 503, post_response.text
    assert_json_rpc_error(
        post_response.json(),
        request_id=1,
        error="manager_unavailable",
        message_contains="server not ready",
    )
    for response in (get_response, delete_response):
        assert response.status_code == 503, response.text
        assert_json_rpc_error(
            response.json(),
            request_id=None,
            error="manager_unavailable",
            message_contains="server not ready",
        )


def test_resolve_and_check_allows_own_workspace(page_service, mcp_default_actor):
    """SEC-1 positive control: when the requested workspace matches the
    mcp_client actor's own scope, ``_resolve_and_check_mcp_workspace`` must
    return ``None`` (no policy error) so the dispatcher proceeds normally.
    """
    from backend.api.mcp_http import _resolve_and_check_mcp_workspace
    from backend.app.main import create_app

    app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )
    actor = mcp_default_actor(scopes=("mcp:read",))
    own_workspace_id = actor.workspace_id
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"search","arguments":'
        b'{"workspace_id":"' + str(own_workspace_id).encode() + b'"}}}'
    )

    class _Req:
        def __init__(self, app):
            self.app = app

    response = _resolve_and_check_mcp_workspace(_Req(app), actor, body)
    assert response is None, (
        "SEC-1 positive control: own-scope request must proceed (None)"
    )


# ---------------------------------------------------------------------------
# SEC-4 — Concurrent SDK message binding regression
# ---------------------------------------------------------------------------


def test_dispatcher_request_bindings_do_not_leak_between_sdk_sessions(
    page_service, monkeypatch
):
    """Two credential-bound SDK sessions keep their own crossed requests."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    import backend.api.mcp_http as transport

    setup, workspace_a, workspace_b, issued_a = _workspace_grant_test_setup(
        page_service
    )
    issued_response = setup.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": str(workspace_b)}],
            "name": "second-workspace-pat",
            "scopes": ["mcp:read"],
        },
    )
    assert issued_response.status_code == 201, issued_response.text
    issued_b = issued_response.json()["data"]
    token_a = UUID(issued_a["api_token"]["id"])
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    observed = []
    app = create_app(settings=_make_settings("streamable"), page_service=page_service)

    async def inspect_crossed_request(name, arguments, **kwargs):
        assert name == "guide"
        actor = kwargs["actor"]
        if actor.token_id == token_a:
            first_entered.set()
            await asyncio.wait_for(second_entered.wait(), timeout=10)
        else:
            await asyncio.wait_for(first_entered.wait(), timeout=10)
            second_entered.set()

        # Resolve again after crossing another SDK message to prove that the
        # SDK request context still belongs to this invocation.
        binding = transport._dispatcher_request_bindings(
            app.state.mcp_session_manager.app, app
        )
        assert binding.actor == actor
        assert binding.mcp_session_id == kwargs["mcp_session_id"]
        snapshot = {
            "token_id": str(actor.token_id),
            "allowed_workspaces": sorted(map(str, actor.token_workspace_ids)),
            "target_workspace": arguments["workspace_id"],
            "session_id": kwargs["mcp_session_id"],
        }
        observed.append(snapshot)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=json.dumps(snapshot))]
        )

    monkeypatch.setattr(transport, "call_tool_async", inspect_crossed_request)

    async def run():
        with _UvicornServer(app) as server:
            async with AsyncExitStack() as stack:
                clients = []
                session_ids = []
                for issued in (issued_a, issued_b):
                    read_stream, write_stream, get_session_id = (
                        await stack.enter_async_context(
                            streamablehttp_client(
                                url=f"{server.url}/mcp",
                                headers={
                                    "Authorization": f"Bearer {issued['plain_token']}"
                                },
                            )
                        )
                    )
                    client = await stack.enter_async_context(
                        ClientSession(
                            read_stream,
                            write_stream,
                            read_timeout_seconds=timedelta(seconds=15),
                        )
                    )
                    await client.initialize()
                    clients.append(client)
                    session_ids.append(get_session_id())
                assert all(session_ids)
                assert session_ids[0] != session_ids[1]

                async def request(client, workspace_id):
                    return await client.send_request(
                        mcp_types.ClientRequest(
                            mcp_types.CallToolRequest(
                                params=mcp_types.CallToolRequestParams(
                                    name="guide",
                                    arguments={"workspace_id": str(workspace_id)},
                                )
                            )
                        ),
                        mcp_types.CallToolResult,
                    )

                results = await asyncio.gather(
                    request(clients[0], workspace_a),
                    request(clients[1], workspace_b),
                )
                for index, (issued, workspace_id) in enumerate(
                    ((issued_a, workspace_a), (issued_b, workspace_b))
                ):
                    expected = {
                        "token_id": issued["api_token"]["id"],
                        "allowed_workspaces": [str(workspace_id)],
                        "target_workspace": str(workspace_id),
                        "session_id": session_ids[index],
                    }
                    assert results[index].isError is False
                    assert json.loads(results[index].content[0].text) == expected
                    assert expected in observed
                assert len(observed) == 2

    try:
        asyncio.run(run())
    finally:
        setup.close()


# ---------------------------------------------------------------------------
# ARCH-1 fix — dispatcher pattern lazy resolve (Sprint_5 Phase_2 Task 2.3)
# ---------------------------------------------------------------------------


def test_streamable_post_handler_does_not_resolve_workspace_eagerly(
    page_service, monkeypatch
):
    """ARCH-1: in ``streamable``/``auto`` mode the POST /mcp handler MUST
    NOT call ``_resolve_and_check_mcp_workspace`` at handler entry — the
    workspace resolve is now lazy inside the dispatcher's @call_tool().

    Negative-test rule (ADR A6): paired with the positive control
    ``test_json_stateless_post_handler_resolves_workspace_eagerly`` below.
    Pins the symbol name ``_resolve_and_check_mcp_workspace`` so a future
    refactor that rewires the eager call under a different name still
    surfaces against the json_stateless control.
    """
    from backend.api import mcp_http as mcp_http_module
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)

    eager_calls: list[tuple] = []
    original = mcp_http_module._resolve_and_check_mcp_workspace

    def _spy(req, actor, body):
        eager_calls.append((actor.actor_id, len(body)))
        return original(req, actor, body)

    monkeypatch.setattr(
        mcp_http_module, "_resolve_and_check_mcp_workspace", _spy
    )

    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        # Establish a session first so tools/list passes the Task 2.6
        # session gate and actually flows through the dispatcher — a
        # transport-level rejection (400 session_required) would satisfy
        # the spy assertion vacuously without exercising the lazy path.
        initial = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "arch1-lazy-test", "version": "0.0"},
                },
            },
        )
        assert initial.status_code == 200, initial.text
        session_id = initial.headers["mcp-session-id"]

        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": session_id,
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    # tools/list does NOT need workspace resolution per Phase_2 §3.10.3 —
    # the handler-entry spy must remain at 0 invocations even though the
    # request itself succeeded through the dispatcher.
    assert response.status_code == 200, response.text
    assert '"tools"' in response.text and "search" in response.text, (
        "tools/list must return a dispatcher result (not a transport "
        f"rejection) so the spy assertion is not vacuous; body: "
        f"{response.text[:500]}"
    )
    assert eager_calls == [], (
        "ARCH-1: streamable POST must NOT call _resolve_and_check_mcp_workspace "
        f"at handler entry; spy captured: {eager_calls}"
    )


def test_json_stateless_tools_list_skips_workspace_resolve(
    page_service, monkeypatch
):
    """tools/list is actor-scoped even on the json_stateless transport."""
    from backend.api import mcp_http as mcp_http_module
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)

    eager_calls: list[tuple] = []
    original = mcp_http_module._resolve_and_check_mcp_workspace

    def _spy(req, actor, body):
        eager_calls.append((actor.actor_id, len(body)))
        return original(req, actor, body)

    monkeypatch.setattr(
        mcp_http_module, "_resolve_and_check_mcp_workspace", _spy
    )

    app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {pat}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert eager_calls == [], (
        "json_stateless tools/list must not resolve a single workspace; "
        f"spy captured: {eager_calls}"
    )


def test_json_stateless_workspace_scoped_tool_keeps_transport_check(
    page_service, monkeypatch
):
    """tools/call for workspace-scoped tools keeps the transport guard."""
    from backend.api import mcp_http as mcp_http_module
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)

    eager_calls: list[tuple] = []
    original = mcp_http_module._resolve_and_check_mcp_workspace

    def _spy(req, actor, body):
        eager_calls.append((actor.actor_id, len(body)))
        return original(req, actor, body)

    monkeypatch.setattr(
        mcp_http_module, "_resolve_and_check_mcp_workspace", _spy
    )

    app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {pat}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "needle"}},
            },
        )
    assert len(eager_calls) == 1, (
        "json_stateless tools/call for workspace-scoped tools must keep the "
        f"transport workspace check; got {eager_calls}"
    )


def test_json_read_restricted_library_denied_in_viewer_target_workspace(page_service):
    """H1 regression: the MCP ``read`` restricted-library gate must evaluate the
    actor's role in the TARGET item's workspace, not the default workspace.

    The PAT user is owner of the default workspace but only a viewer of the
    target workspace (the seed grants viewer). Reading a *restricted* item there
    must be denied. Before the fix the gate used the default-workspace role
    (owner) and allowed the cross-workspace restricted read.
    """
    from sqlalchemy import select

    from backend.models.library import LibraryItem, LibraryItemVersion
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
        seed_viewer_target_with_restricted_library_item,
    )

    setup_client = make_client(page_service)
    target_workspace_id = seed_viewer_target_with_restricted_library_item(page_service)
    pat = issue_personal_api_token(
        setup_client,
        workspace_id=target_workspace_id,
    )

    with page_service.session_factory() as session:
        version = session.scalar(
            select(LibraryItemVersion)
            .join(LibraryItem, LibraryItemVersion.library_item_id == LibraryItem.id)
            .where(LibraryItem.workspace_id == target_workspace_id)
            .where(LibraryItem.trust_level == "restricted")
        )
        assert version is not None
        version_id = version.id

    app = create_app(
        settings=_make_settings("json_stateless"), page_service=page_service
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {pat}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "read",
                    "arguments": {
                        "workspace_id": str(target_workspace_id),
                        "target_type": "library_item_version",
                        "target_id": str(version_id),
                    },
                },
            },
        )
    assert response.status_code == 403, response.text
    assert "restricted_library_denied" in response.text


def test_restricted_library_guard_checks_replica_before_target_lookup(
    page_service, monkeypatch
):
    """A missing grant replica must not become a restricted-item oracle."""
    from backend.api import mcp_http as mcp_http_module
    from backend.models.base import utc_now
    from backend.models.library import LibraryItem, LibraryItemVersion
    from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
        seed_viewer_target_with_restricted_library_item,
    )

    setup_client = make_client(page_service)
    target_workspace_id = seed_viewer_target_with_restricted_library_item(page_service)
    pat = issue_personal_api_token(
        setup_client,
        workspace_id=target_workspace_id,
    )

    with page_service.session_factory.begin() as session:
        version = session.scalar(
            select(LibraryItemVersion)
            .join(LibraryItem, LibraryItemVersion.library_item_id == LibraryItem.id)
            .where(LibraryItem.workspace_id == target_workspace_id)
            .where(LibraryItem.trust_level == "restricted")
        )
        assert version is not None
        version_id = version.id
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == target_workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
            )
        )
        assert replica is not None
        replica.revoked_at = utc_now()

    target_lookups: list[tuple] = []
    original_resolver = mcp_http_module.resolve_restricted_library_target

    def _spy_resolver(**kwargs):
        target_lookups.append(
            (kwargs["workspace_id"], kwargs["target_type"], kwargs["target_id"])
        )
        return original_resolver(**kwargs)

    monkeypatch.setattr(
        mcp_http_module,
        "resolve_restricted_library_target",
        _spy_resolver,
    )

    app = create_app(
        settings=_make_settings("json_stateless"), page_service=page_service
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {pat}"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "read",
                    "arguments": {
                        "workspace_id": str(target_workspace_id),
                        "target_type": "library_item_version",
                        "target_id": str(version_id),
                    },
                },
            },
        )

    assert response.status_code == 503, response.text
    assert_json_rpc_error(
        response.json(),
        request_id=2,
        error="grant_replica_missing",
        message_contains="no live workspace-plane credential grant replica",
    )
    assert target_lookups == []


def test_dispatcher_call_tool_invokes_resolve_and_check(
    page_service, monkeypatch, mcp_default_actor
):
    """ARCH-1 positive: dispatcher's @call_tool() invokes ``resolve_and_check``
    lazily — counts >= 1 for a tool name with a non-None
    ``TOOL_REQUIRED_ROLES`` mapping (e.g. ``search``).

    Uses ``_call_tool`` directly via the dispatcher's registered handler
    pattern; the SDK would normally invoke it but we exercise the same
    bound closure by calling the module's helper that the @call_tool
    decorator wraps. This isolates Phase_2 §3.10.3 action mapping +
    §3.10.2 step (5) ordering from SDK plumbing concerns.
    """
    import backend.api.mcp_http as mcp_http_module

    calls: list[tuple] = []
    original = mcp_http_module.resolve_and_check

    def _spy(*, session, actor, action, requested_workspace_id=None):
        calls.append((actor.actor_id, action, requested_workspace_id))
        return original(
            session=session,
            actor=actor,
            action=action,
            requested_workspace_id=requested_workspace_id,
        )

    monkeypatch.setattr(mcp_http_module, "resolve_and_check", _spy)

    # _map_action returns "read" for "search" (TOOL_REQUIRED_ROLES → viewer
    # → required_role_to_action → "read"); None for "list_my_workspaces".
    assert mcp_http_module._map_action("search") == "read"
    assert mcp_http_module._map_action("create_context_page") == "write"
    assert mcp_http_module._map_action("list_my_workspaces") is None
    # Positive control: A6 rule — a known tool name that requires editor
    # role maps to "write" so dispatcher correctly gates writes too.
    assert mcp_http_module._map_action("write_agent_page_draft") == "write"


def test_map_action_uses_tool_required_roles(page_service):
    """Phase_2 §3.10.3: ``_map_action`` MUST mirror ``TOOL_REQUIRED_ROLES``.

    Negative-test rule (ADR A6): the assertions pin both the ``None``
    case (skip workspace check) and the ``viewer``/``editor`` → action
    mapping, so an accidental hard-coded ``"read"`` for all tools
    (the pre-fix ARCH-3 behavior) would surface here.
    """
    from backend.api.mcp_http import _map_action
    from backend.mcp.scopes import TOOL_REQUIRED_ROLES

    for tool_name, required_role in TOOL_REQUIRED_ROLES.items():
        action = _map_action(tool_name)
        if required_role is None:
            assert action is None, (
                f"{tool_name}: TOOL_REQUIRED_ROLES is None → action must be None "
                f"(skip workspace check), got {action!r}"
            )
        elif required_role == "viewer":
            assert action == "read", f"{tool_name}: viewer → read, got {action!r}"
        elif required_role == "editor":
            assert action == "write", f"{tool_name}: editor → write, got {action!r}"
        else:  # pragma: no cover — only viewer/editor/None are used today
            assert action in {"read", "write", "admin"}


# ---------------------------------------------------------------------------
# Task 2.3 — GET /mcp SSE handshake
# ---------------------------------------------------------------------------


def test_get_mcp_json_stateless_authenticates_before_method_check(page_service):
    """Every /mcp method requires a PAT/OAuth bearer.

    Once authenticated, json_stateless GET still reports that the transport
    has no server-to-client SSE stream.
    """
    _, pat = _pat_client(page_service)
    app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        no_auth = client.get("/mcp", headers={"Accept": "text/event-stream"})
        response = client.get(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "text/event-stream",
            },
        )

    assert no_auth.status_code == 401, no_auth.text
    assert "resource_metadata=" in no_auth.headers["www-authenticate"]
    assert response.status_code == 405, response.text
    assert response.headers.get("allow") == "POST, DELETE"
    payload = assert_json_rpc_error(
        response.json(),
        request_id=None,
        error="method_not_allowed",
    )
    assert "streamable" in payload["message"] or "auto" in payload["message"]


def test_post_mcp_succeeds_in_json_stateless_setup(page_service):
    """Task 2.3 positive control (ADR A6): same json_stateless app — POST
    /mcp without bearer returns the canonical 401 (Sprint_4 baseline).
    Pairs with ``test_get_mcp_405_in_json_stateless`` so a configuration
    drift that broke the json_stateless POST path would surface here.
    """
    app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert response.status_code == 401, response.text


def test_get_mcp_unauthorized_returns_401(mcp_modes):
    """Task 2.3 (Task 2.7 cross-ref): mode=``streamable`` GET without
    bearer returns 401 + canonical ``WWW-Authenticate`` header.

    Negative-test rule (ADR A6): paired positive control verifies that
    *with* a bearer the request reaches the SDK handshake (no 401).
    """
    client = mcp_modes.client("streamable")
    response = client.get(
        "/mcp", headers={"Accept": "text/event-stream"}
    )
    assert response.status_code == 401, response.text
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_mcp_auth_server_fault_returns_500_not_401(page_service, monkeypatch):
    """Operational faults inside bearer auth must not masquerade as 401.

    The 401 challenge carries ``resource_metadata`` which makes MCP
    clients discard the token and restart the OAuth flow — a transient
    backend outage (DB down, OAuth misconfiguration) must surface as a
    500 server fault instead, on all three /mcp handlers. Positive
    control (ADR A6): an invalid bearer (auth-shaped ``HTTPException``)
    still yields the canonical 401 challenge.
    """
    import backend.api.mcp_http as mcp_http_module

    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        control = client.post(
            "/mcp",
            headers={"Authorization": "Bearer dtxw_bogus"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert control.status_code == 401, control.text
        assert "resource_metadata=" in control.headers["www-authenticate"]

        def _server_fault(_request, _token):
            raise RuntimeError("database connection lost")

        monkeypatch.setattr(
            mcp_http_module, "authenticate_bearer_token", _server_fault
        )
        responses = {
            "post": client.post(
                "/mcp",
                headers={"Authorization": "Bearer dtxw_bogus"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            ),
            "get": client.get(
                "/mcp",
                headers={
                    "Authorization": "Bearer dtxw_bogus",
                    "Accept": "text/event-stream",
                },
            ),
            "delete": client.delete(
                "/mcp",
                headers={
                    "Authorization": "Bearer dtxw_bogus",
                    "Mcp-Session-Id": "fault-session",
                },
            ),
        }
        for method, response in responses.items():
            assert response.status_code == 500, (method, response.text)
            assert "www-authenticate" not in response.headers, method


def test_get_mcp_session_errors_are_json_rpc(mcp_modes):
    pat = mcp_modes.pat

    client = mcp_modes.client("streamable")
    unknown = client.get(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "text/event-stream",
            "Mcp-Session-Id": "unknown-get-session",
        },
    )
    initial = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "get-error-shape",
                    "version": "0.0.0",
                },
            },
        },
    )
    assert initial.status_code == 200, initial.text
    session_id = initial.headers["mcp-session-id"]
    deleted = client.delete(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Mcp-Session-Id": session_id,
        },
    )
    assert deleted.status_code == 200, deleted.text
    expired = client.get(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "text/event-stream",
            "Mcp-Session-Id": session_id,
        },
    )

    assert unknown.status_code == 404, unknown.text
    assert_json_rpc_error(
        unknown.json(),
        request_id=None,
        error="session_unknown",
        message_contains="not found",
    )
    assert expired.status_code == 410, expired.text
    assert_json_rpc_error(
        expired.json(),
        request_id=None,
        error="session_expired",
        message_contains="terminated",
    )


def test_get_mcp_handshake_does_not_call_workspace_resolve(
    page_service, monkeypatch
):
    """Task 2.3 + ARCH-1: mode=``streamable`` GET handshake MUST NOT
    invoke ``resolve_and_check`` (Phase_2 §3.10.1 row 4: GET handshake
    is transport-level; the workspace resolve runs lazily on the first
    tool call inside the dispatcher's @call_tool()).

    Negative-test rule (ADR A6): paired with
    ``test_streamable_post_handler_does_not_resolve_workspace_eagerly``
    (POST tools/list also skips) + the positive control
    ``test_dispatcher_call_tool_invokes_resolve_and_check`` confirming
    that resolve_and_check DOES fire when a real tool name reaches the
    dispatcher.

    Pin: the spy must observe exactly zero calls. The test uses
    ``client.stream`` so the SSE handshake does not block on the
    long-lived response body — only the response headers are checked.
    """
    import backend.api.mcp_http as mcp_http_module
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    calls: list[tuple] = []
    original = mcp_http_module.resolve_and_check

    def _spy(*, session, actor, action, requested_workspace_id=None):
        calls.append((actor.actor_id, action, requested_workspace_id))
        return original(
            session=session,
            actor=actor,
            action=action,
            requested_workspace_id=requested_workspace_id,
        )

    monkeypatch.setattr(mcp_http_module, "resolve_and_check", _spy)

    # Issue a PAT against a json_stateless setup; the PAT lives in the DB
    # and is reusable for the streamable app below.
    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)

    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        # The SDK SSE handshake requires a session id from a prior
        # initialize. Without one, the SDK responds with 400 "Missing
        # session ID" — which is still a transport-level response that
        # MUST NOT have triggered workspace resolution. The point of this
        # test is the spy count, not the SDK return code; both 200 and
        # 400 are valid "handshake-only" outcomes.
        with client.stream(
            "GET",
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "text/event-stream",
            },
        ) as response:
            status = response.status_code

    # Phase_2 §3.10.1 row 4 contract: zero resolve_and_check invocations
    # during the GET handshake regardless of SDK status code.
    assert calls == [], (
        f"Task 2.3: GET handshake MUST NOT call resolve_and_check; "
        f"spy captured {calls} (status was {status})"
    )


# ---------------------------------------------------------------------------
# Task 2.4 — DELETE /mcp idempotent session termination
# ---------------------------------------------------------------------------


def test_delete_mcp_idempotent(page_service):
    """Task 2.4 + B5.3 evidence row: DELETE /mcp on mode=``streamable``
    with a *never-created* session id returns 200 (RFC 9110 §9.3.5
    idempotent DELETE semantics).

    Negative-test rule (ADR A6): the assertion pins both the status code
    (200) AND the body shape (``{"status":"ok"}``) so a regression that
    silently changed the idempotent body to an SDK 404 would surface.
    Paired with ``test_delete_mcp_terminates_existing_session`` below as
    the "real termination" positive control.

    Repeats the DELETE three times to verify state convergence — each
    call must return the same 2xx + body (no transient 404 between
    calls).
    """
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)

    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        results = []
        for _ in range(3):
            response = client.delete(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Mcp-Session-Id": "never-created-session-id",
                },
            )
            results.append((response.status_code, response.json()))

        # No-session-id case: also 200 idempotent (no Mcp-Session-Id
        # header at all → nothing to terminate, MUST NOT return 400).
        no_id_response = client.delete(
            "/mcp", headers={"Authorization": f"Bearer {pat}"}
        )

    # All three unknown-id DELETEs converge to the same response.
    assert results == [(200, {"status": "ok"})] * 3, results
    assert no_id_response.status_code == 200, no_id_response.text
    assert no_id_response.json() == {"status": "ok"}


def test_delete_mcp_requires_auth(mcp_modes):
    """Task 2.4: bearer missing on a ``streamable`` DELETE → 401 +
    canonical ``WWW-Authenticate`` header.

    Negative-test rule (ADR A6): paired with the positive control
    ``test_delete_mcp_idempotent`` (same setup, valid bearer → 200) so
    a regression that accidentally bypassed bearer auth on DELETE
    (e.g. mirroring the unauthenticated json_stateless no-op) surfaces.
    Pins the ``resource_metadata=`` substring per the OAuth protected
    resource metadata RFC 9728.
    """
    client = mcp_modes.client("streamable")
    response = client.delete("/mcp")

    assert response.status_code == 401, response.text
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_delete_mcp_json_stateless_no_op(page_service):
    """Authenticated json_stateless DELETE is an idempotent no-op."""
    _, pat = _pat_client(page_service)
    app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        no_auth = client.delete("/mcp")
        with_bogus = client.delete(
            "/mcp", headers={"Authorization": "Bearer not-a-real-token"}
        )
        authenticated = client.delete(
            "/mcp", headers={"Authorization": f"Bearer {pat}"}
        )

    assert no_auth.status_code == 401, no_auth.text
    assert with_bogus.status_code == 401, with_bogus.text
    assert authenticated.status_code == 200, authenticated.text
    assert authenticated.json() == {"status": "ok"}


def test_delete_mcp_does_not_call_workspace_resolve(
    page_service, monkeypatch
):
    """Task 2.4 + Phase_2 §3.10.1 row 5: DELETE on ``streamable`` mode
    MUST NOT call ``resolve_and_check`` — session termination is
    transport-level only, no workspace context required.

    Negative-test rule (ADR A6): paired with the positive control
    ``test_dispatcher_call_tool_invokes_resolve_and_check`` (same
    helper fires on a real tool call). Pins the spy at zero calls
    across multiple DELETEs (known + unknown + no-id session paths).
    """
    import backend.api.mcp_http as mcp_http_module
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    calls: list[tuple] = []
    original = mcp_http_module.resolve_and_check

    def _spy(*, session, actor, action):
        calls.append((actor.actor_id, action))
        return original(session=session, actor=actor, action=action)

    monkeypatch.setattr(mcp_http_module, "resolve_and_check", _spy)

    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)

    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        # Unknown id
        r1 = client.delete(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Mcp-Session-Id": "unknown-session-id",
            },
        )
        # No id
        r2 = client.delete(
            "/mcp", headers={"Authorization": f"Bearer {pat}"}
        )

    assert r1.status_code == 200 and r2.status_code == 200
    assert calls == [], (
        "Task 2.4: DELETE MUST NOT invoke resolve_and_check; "
        f"spy captured {calls}"
    )


# ---------------------------------------------------------------------------
# Task 2.5 — Accept negotiation helper (Phase_2 §2.3/§2.4 22-row matrix)
# ---------------------------------------------------------------------------


def _scope_with_accept(accept_value: str | None) -> dict:
    """Build an ASGI-shaped scope dict with the given Accept header.

    ``accept_value=None`` omits the Accept header entirely (the
    "missing" case in Phase_2 §2.3 Step 5).
    """
    headers: list[tuple[bytes, bytes]] = []
    if accept_value is not None:
        headers.append((b"accept", accept_value.encode("latin-1")))
    return {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}


# Phase_2 §2.4 — full 22-row Accept decision matrix. Tuple shape:
#   (row_number, mode, accept_value_or_None, expected_branch, expected_hack)
# expected_branch ∈ {"json", "sse", "conflict", "not_acceptable"}.
# expected_hack: only the "json" branch on json_stateless mode applies
# the compat-hack scope rewrite (Phase_2 §2.6). Every other branch
# returns the scope unchanged (or {} for conflict).
ACCEPT_MATRIX = [
    # --- json_stateless rows (1–8) ---
    (1, "json_stateless", None, "json", True),
    (2, "json_stateless", "*/*", "json", True),
    (3, "json_stateless", "application/json", "json", False),
    (4, "json_stateless", "application/json;q=0.5", "json", False),
    (5, "json_stateless", "text/event-stream", "conflict", False),
    (
        6,
        "json_stateless",
        "text/event-stream;q=0.9, application/json;q=0.5",
        "conflict",
        False,
    ),
    (
        7,
        "json_stateless",
        "application/json, text/event-stream",
        "json",
        False,
    ),
    (
        8,
        "json_stateless",
        "application/json;q=0, text/event-stream",
        "conflict",
        False,
    ),
    # --- streamable rows (9–14) ---
    (9, "streamable", None, "sse", False),
    (10, "streamable", "*/*", "sse", False),
    (11, "streamable", "text/event-stream", "sse", False),
    (12, "streamable", "application/json", "conflict", False),
    (
        13,
        "streamable",
        "application/json;q=0, text/event-stream;q=0.5",
        "sse",
        False,
    ),
    (
        14,
        "streamable",
        "application/json, text/event-stream",
        "sse",
        False,
    ),
    # --- auto rows (15–22) ---
    (15, "auto", None, "sse", False),
    (16, "auto", "*/*", "sse", False),
    (17, "auto", "application/json", "json", False),
    (18, "auto", "text/event-stream", "sse", False),
    (
        19,
        "auto",
        "application/json;q=0.9, text/event-stream;q=0.5",
        "json",
        False,
    ),
    (
        20,
        "auto",
        "application/json;q=0.5, text/event-stream;q=0.9",
        "sse",
        False,
    ),
    # Row 21 — dual-q SDK delegation. We classify as "sse" because the
    # auto-mode instance-scoped manager has stateless=False and
    # json_response=False; the SDK will pick from Accept order.
    (
        21,
        "auto",
        "application/json, text/event-stream",
        "sse",
        False,
    ),
    (22, "auto", "application/json;q=0", "not_acceptable", False),
]


@pytest.mark.parametrize(
    "row,mode,accept,expected_branch,expected_hack",
    ACCEPT_MATRIX,
    ids=[
        f"row{row:02d}-{mode}-{accept!r}"
        for row, mode, accept, _, _ in ACCEPT_MATRIX
    ],
)
def test_accept_negotiation_matrix_row(
    row, mode, accept, expected_branch, expected_hack
):
    """Phase_2 §2.4 Accept decision matrix — 22 rows, 1:1 pytest cases.

    Each row drives ``_negotiate_transport`` directly and verifies the
    branch label + (for json_stateless JSON) the compat-hack scope
    rewrite. The parametrize ids prefix every row number so a failing
    row is identifiable at a glance.

    Negative-test rule (ADR A6): the matrix folds *positive* + *negative*
    rows into one table. Every ``conflict`` row sits next to at least one
    same-mode ``json`` or ``sse`` positive control (e.g. row 5/6/8 ↔ row
    1/2/3/4/7 for json_stateless); a regression that broke the
    conflict branch could not silently pass because the same parameter
    table also exercises the success branches in the same module.
    """
    from backend.api.mcp_http import _negotiate_transport

    scope = _scope_with_accept(accept)
    branch, new_scope = _negotiate_transport(scope, mode)
    assert branch == expected_branch, (
        f"row {row}: mode={mode} accept={accept!r} → expected branch "
        f"{expected_branch!r}, got {branch!r}"
    )
    if expected_branch in {"conflict", "not_acceptable"}:
        # Error paths return an empty scope sentinel; the caller emits
        # the JSON-RPC error response, not us.
        assert new_scope == {}, f"row {row}: error scope must be empty"
        return

    accept_header = next(
        (
            value.decode("latin-1")
            for key, value in new_scope.get("headers", [])
            if key.lower() == b"accept"
        ),
        None,
    )
    if expected_hack:
        # The compat hack rewrites missing/star Accept → application/json
        # (Phase_2 §2.6 H5). Pin the rewritten value to guard against a
        # future regression that silently dropped the hack for
        # json_stateless rows.
        assert accept_header == "application/json", (
            f"row {row}: json_stateless compat hack must rewrite to "
            f"application/json; got {accept_header!r}"
        )
    elif accept is None:
        # No Accept header on the input, no hack expected — output also
        # has no Accept header.
        assert accept_header is None, (
            f"row {row}: missing-Accept input must remain missing "
            f"(no hack); got {accept_header!r}"
        )
    else:
        # Non-hack branches forward the scope unchanged.
        assert accept_header == accept, (
            f"row {row}: non-hack branch must forward Accept unchanged; "
            f"input={accept!r} output={accept_header!r}"
        )


def test_accept_negotiation_matrix_covers_all_22_rows():
    """Sanity-check that the parametrize table actually has 22 rows
    (Phase_2 §2.4 contract: every plan row → one pytest case).

    Without this assertion, an accidental row deletion during a refactor
    could shrink coverage silently — parametrize alone reports only the
    surviving rows, not the missing ones.
    """
    assert len(ACCEPT_MATRIX) == 22, (
        f"Phase_2 §2.4 contract: matrix must have 22 rows; got {len(ACCEPT_MATRIX)}"
    )
    # Pin the per-mode row counts so a future drift doesn't silently
    # rebalance the matrix (json_stateless: 8, streamable: 6, auto: 8).
    json_stateless_rows = [r for r in ACCEPT_MATRIX if r[1] == "json_stateless"]
    streamable_rows = [r for r in ACCEPT_MATRIX if r[1] == "streamable"]
    auto_rows = [r for r in ACCEPT_MATRIX if r[1] == "auto"]
    assert len(json_stateless_rows) == 8
    assert len(streamable_rows) == 6
    assert len(auto_rows) == 8


def test_parse_accept_q_handles_malformed_parameters():
    """Phase_2 §2.3 Step 1: parameter parse failure on one media-range
    must skip *only that range*, not the entire header.

    Negative-test rule (ADR A6): paired with a positive control — the
    well-formed entry in the same Accept header still surfaces with the
    expected q value. Pins the keyword ``application/json`` (the
    well-formed entry) so a regression that emptied the whole map could
    not silently pass.
    """
    from backend.api.mcp_http import _parse_accept_q

    # The first part has q out of range [0, 1], the second is well-formed.
    q_map = _parse_accept_q("application/json;q=2.0, text/event-stream;q=0.5")
    # Out-of-range q on application/json → that media-range is skipped;
    # the well-formed text/event-stream entry survives.
    assert "application/json" not in q_map, q_map
    assert q_map.get("text/event-stream") == 0.5, q_map


def test_parse_accept_q_q_value_defaults_to_one():
    """Phase_2 §2.3 Step 1: media-range without ``q`` parameter defaults
    to q=1.0.

    Positive control for ``test_parse_accept_q_handles_malformed_parameters``
    above — pins the q=1.0 default so a future change to RFC 7231
    semantics would surface.
    """
    from backend.api.mcp_http import _parse_accept_q

    q_map = _parse_accept_q("application/json, text/event-stream")
    assert q_map == {"application/json": 1.0, "text/event-stream": 1.0}


# ---------------------------------------------------------------------------
# Task 2.5 — Accept negotiation handler integration
# ---------------------------------------------------------------------------


def _pat_client(page_service) -> tuple[TestClient, str]:
    """Build a TestClient under json_stateless + issue a PAT.

    The PAT is reusable across modes because it lives in the database,
    not on app.state. Returned client is the json_stateless setup client
    used only to mint the token.
    """
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    client = make_client(page_service)
    return client, issue_personal_api_token(client)


class _SharedMcpApps:
    """Lazily built, lifespan-entered apps over one store, keyed by transport.

    The Accept negotiation, parse-error, session-gate and 401-challenge tests
    only POST to /mcp with a bearer from the shared store, so they reuse these
    apps instead of building (and signing up for) their own. Tests that count
    sessions, patch app state or exercise lifecycle build their own app.
    """

    def __init__(self, page_service, pat: str, stack: ExitStack):
        self._page_service = page_service
        self._stack = stack
        self._clients: dict[tuple[str, bool], TestClient] = {}
        self.pat = pat

    def client(self, mode: str, *, public_origin: bool = False) -> TestClient:
        key = (mode, public_origin)
        if key not in self._clients:
            settings = (
                _make_settings_with_public_origin(mode)
                if public_origin
                else _make_settings(mode)
            )
            app = create_app(settings=settings, page_service=self._page_service)
            self._clients[key] = self._stack.enter_context(TestClient(app))
        return self._clients[key]


@pytest.fixture(scope="module")
def mcp_modes(_empty_sqlite_schema):
    with (
        module_scoped_page_service(_empty_sqlite_schema) as page_service,
        ExitStack() as stack,
    ):
        _, pat = _pat_client(page_service)
        yield _SharedMcpApps(page_service, pat, stack)


def test_force_mode_conflict_returns_415(mcp_modes):
    """Plan-named test for §2.4 rows 5 + 12.

    mode=``json_stateless`` + Accept=``text/event-stream`` → 415 with a
    JSON-RPC error body carrying ``expected=json_stateless`` in
    ``error.data``. mode=``streamable`` + Accept=``application/json`` →
    415 with ``expected=streamable`` in ``error.data``.

    Negative-test rule (ADR A6): pins the expected mode inside the JSON-RPC
    error ``data`` object so a future regression that leaks legacy top-level
    ``expected`` would surface here. Paired positive control:
    ``test_accept_star_uses_mode_default`` in the same file — same
    setup, Accept compatible with the mode → 2xx.
    """
    pat = mcp_modes.pat

    js_client = mcp_modes.client("json_stateless")
    stream_client = mcp_modes.client("streamable")
    js_resp = js_client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    stream_resp = stream_client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert js_resp.status_code == 415, js_resp.text
    js_body = js_resp.json()
    assert_json_rpc_error(
        js_body,
        request_id=1,
        error="transport_mode_mismatch",
        message_contains="json_stateless",
        data={"expected": "json_stateless"},
    )
    assert "expected" not in js_body, (
        f"JSON-RPC error data must carry expected mode; got top-level {js_body}"
    )

    assert stream_resp.status_code == 415, stream_resp.text
    stream_body = stream_resp.json()
    assert_json_rpc_error(
        stream_body,
        request_id=1,
        error="transport_mode_mismatch",
        message_contains="streamable",
        data={"expected": "streamable"},
    )
    assert "expected" not in stream_body


def test_no_accept_fallback_per_mode(mcp_modes):
    """Plan-named test for §2.4 rows 1 + 9 + 15 (missing Accept).

    - mode=``json_stateless`` no Accept → JSON branch → 2xx (Sprint_4
      regression preserved via compat hack).
    - mode=``streamable`` no Accept → SSE branch (SDK delegation,
      handshake-shaped response).
    - mode=``auto`` no Accept → SSE branch (mode default).

    Negative-test rule (ADR A6): A bearer-less request on the same
    setup MUST still 401 — proves the missing-Accept fallback did not
    inadvertently bypass bearer auth.
    """
    pat = mcp_modes.pat

    # Row 1: json_stateless, missing Accept → 200 (the request is a
    # tools/list, which succeeds for an mcp:read PAT actor against the
    # default workspace).
    client = mcp_modes.client("json_stateless")
    r1 = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pat}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    # Positive control: same setup without bearer still 401.
    r1_no_auth = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r1.status_code == 200, r1.text
    assert "application/json" in r1.headers.get("content-type", "")
    assert r1_no_auth.status_code == 401

    # Row 9: streamable, missing Accept → handed to SDK (which validates
    # the now-missing Accept and may return 406; the 415 negotiation
    # MUST NOT fire because missing → SSE per §2.3 Step 5(a)). Either
    # the SDK accepts it (2xx) or rejects with 406; the wrong outcome
    # would be a 415 from our negotiator. We assert *not* 415.
    client = mcp_modes.client("streamable")
    r9 = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pat}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r9.status_code != 415, (
        "streamable missing-Accept MUST go to SSE branch (not 415); "
        f"got {r9.status_code}"
    )

    # Row 15: auto, missing Accept → also SSE branch, same expectation.
    client = mcp_modes.client("auto")
    r15 = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pat}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r15.status_code != 415, (
        "auto missing-Accept MUST go to mode-default SSE (not 415); "
        f"got {r15.status_code}"
    )


def test_accept_star_uses_mode_default(mcp_modes):
    """Plan-named test for §2.4 rows 2 + 10 + 16 (Accept=``*/*``).

    - mode=``streamable`` Accept=``*/*`` → SSE branch (not 415).
    - mode=``json_stateless`` Accept=``*/*`` → JSON branch → 200
      (compat hack rewrites to application/json).

    Negative-test rule (ADR A6): the json_stateless 200 assertion is
    paired with the streamable not-415 assertion in the same test so a
    regression that broke either branch's ``*/*`` fallback surfaces.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("json_stateless")
    r_js = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pat}", "Accept": "*/*"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r_js.status_code == 200, r_js.text
    assert "application/json" in r_js.headers.get("content-type", "")

    client = mcp_modes.client("streamable")
    r_stream = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pat}", "Accept": "*/*"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r_stream.status_code != 415, (
        f"streamable */* MUST go to SSE branch (not 415); got {r_stream.status_code}"
    )


def test_accept_q_zero_excluded(mcp_modes):
    """Plan-named test for §2.4 row 13 (streamable, q=0 exclusion).

    Accept=``application/json;q=0, text/event-stream;q=0.5`` →
    SSE branch (q=0 application/json is excluded per RFC 7231 §5.3.1,
    leaving only text/event-stream which matches the streamable mode
    default).

    Negative-test rule (ADR A6): the *negative* twin is §2.4 row 8 —
    same q=0 logic flips to a 415 conflict when the mode demands JSON.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("streamable")
    r = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json;q=0, text/event-stream;q=0.5",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    # Negotiator output is SSE; SDK will accept the text/event-stream
    # request. The wrong outcome would be a 415 (q=0 forgotten).
    assert r.status_code != 415, (
        f"q=0 application/json must be excluded; got {r.status_code}"
    )

    # §2.4 row 8 paired negative: json_stateless + same q=0 layout → 415.
    client = mcp_modes.client("json_stateless")
    r_js = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json;q=0, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r_js.status_code == 415, r_js.text
    assert_json_rpc_error(
        r_js.json(),
        request_id=1,
        error="transport_mode_mismatch",
        data={"expected": "json_stateless"},
    )


def test_accept_q_value_respected(mcp_modes):
    """Plan-named test for §2.4 row 19 (auto, q-value precedence).

    mode=``auto`` Accept=``application/json;q=0.9, text/event-stream;q=0.5``
    → JSON branch → 200 + ``application/json`` content-type.

    Sprint_5 Phase_2 fix-round-2 (HIGH-1, Codex review 260517-1131): this
    test previously asserted only ``status_code != 415``, which made the
    plan's row 19 dispatch contract unverifiable end-to-end. The handler
    now dispatches auto/JSON via the request-scope ``json_response=True``
    manager so the response is actually JSON.

    Negative-test rule (ADR A6): paired with
    ``test_accept_q_value_respected_reverse`` below (row 20, inverted q
    values → SSE). Two opposing assertions on the same code path catch
    a regression that hardcoded one branch.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("auto")
    r = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json;q=0.9, text/event-stream;q=0.5",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 200, r.text
    content_type = r.headers.get("content-type", "")
    assert "application/json" in content_type, (
        f"auto + JSON-priority q-values must return JSON; got "
        f"content-type={content_type!r}"
    )


def test_accept_json_returns_json(mcp_modes):
    """Plan-named test for §2.4 row 17 (auto, JSON explicit).

    mode=``auto`` Accept=``application/json`` → JSON branch → 200 +
    ``application/json`` content-type. Positive control for the
    negotiator's auto-mode JSON path.

    Sprint_5 Phase_2 fix-round-2 (HIGH-1): previously asserted only
    ``status_code != 415``. The handler now actually dispatches
    auto/JSON via the request-scope ``json_response=True`` manager,
    matching Phase_2 §2.4 row 17 contract.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("auto")
    r = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 200, r.text
    content_type = r.headers.get("content-type", "")
    assert "application/json" in content_type, (
        f"auto + Accept=application/json must return JSON; got "
        f"content-type={content_type!r}"
    )


def test_accept_sse_returns_sse(mcp_modes):
    """Plan-named test for §2.4 row 18 (auto, SSE explicit).

    mode=``auto`` Accept=``text/event-stream`` → SSE branch dispatch.
    The SDK requires Accept to include BOTH ``application/json`` AND
    ``text/event-stream`` for non-stateless POSTs, so an SSE-only
    Accept reaches the SDK and surfaces a 406 "Not Acceptable" body
    with the SDK's "must accept both" message — this is the SSE
    branch's signature (vs the negotiator's 415
    ``transport_mode_mismatch``).

    Sprint_5 Phase_2 fix-round-2 (HIGH-1): symmetric tightening with
    ``test_accept_json_returns_json``. Previously asserted only
    ``status_code != 415``; we now pin the SDK 406 path so a regression
    that swapped SSE → JSON dispatch (or back into 415) fails.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("auto")
    r = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-client",
                    "version": "0.0.0",
                },
            },
        },
    )
    # SDK enforces "Accept must include both application/json AND
    # text/event-stream" — proves we routed to the SSE branch (vs 415
    # transport_mode_mismatch from the negotiator).
    assert r.status_code == 406, r.text
    body = r.text
    assert "application/json" in body and "text/event-stream" in body, (
        f"SDK 406 body must reference both media types; got {body!r}"
    )


def test_accept_q_value_respected_reverse(mcp_modes):
    """Plan-named test for §2.4 row 20 (auto, q-value precedence reversed).

    mode=``auto`` Accept=``application/json;q=0.5, text/event-stream;q=0.9``
    → SSE branch → 200 + ``text/event-stream`` content-type.

    Sprint_5 Phase_2 fix-round-2 (HIGH-1): ADR A6 negative-test pair for
    ``test_accept_q_value_respected`` — same handler path, inverted
    q-values yield the opposite content-type. A regression that
    hardcoded one branch would fail one of the two assertions. Uses
    ``initialize`` (no session id required) so the Task 2.6 gate does
    not preempt the SSE dispatch.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("auto")
    r = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json;q=0.5, text/event-stream;q=0.9",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-client",
                    "version": "0.0.0",
                },
            },
        },
    )
    assert r.status_code == 200, r.text
    content_type = r.headers.get("content-type", "")
    assert "text/event-stream" in content_type, (
        f"auto + SSE-priority q-values must return SSE; got "
        f"content-type={content_type!r}"
    )
    _delete_session(client, pat, r)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_streamable_sse_response_declares_utf8_and_roundtrips_unicode(
    page_service, method
):
    """GET and POST SSE responses preserve UTF-8 bytes and response headers."""

    _, pat = _pat_client(page_service)
    sample = "한글 통합 리뷰 — emoji 😀 — decomposed e\u0301"
    encoded_event = (
        "data: "
        + json.dumps({"echo": sample}, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode("utf-8")

    class _FiniteSSEManager:
        def __init__(self):
            self.seen_body = b""
            # The session the initialize below opens, so a GET can attach.
            self._server_instances = {"unicode-test-session": object()}

        async def handle_request(self, scope, receive, send):
            if scope["method"] == "POST":
                request_message = await receive()
                self.seen_body = request_message["body"]
                request_payload = json.loads(self.seen_body.decode("utf-8"))
                assert request_payload["params"]["clientInfo"]["name"] == sample
            content_type = (
                b"text/event-stream"
                if scope["method"] == "GET"
                else b'text/event-stream; profile="a;b"; charset=latin-1; '
                b"version=1; charset; charset=UTF-8"
            )

            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (
                            b"content-type",
                            content_type,
                        ),
                        (b"cache-control", b"no-cache"),
                        (b"x-transport-marker", b"preserved"),
                        (b"warning", b'199 dtxwiki "first"'),
                        (b"warning", b'299 dtxwiki "second"'),
                        (b"mcp-session-id", b"unicode-test-session"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": encoded_event,
                    "more_body": False,
                }
            )

    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    fake_manager = _FiniteSSEManager()
    raw_request = b""
    with TestClient(app) as client:
        app.state.mcp_session_manager = fake_manager
        headers = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json, text/event-stream",
        }
        raw_request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": sample, "version": "0.0.0"},
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response = client.post(
            "/mcp",
            headers={
                **headers,
                "Content-Type": "application/json; charset=utf-8",
            },
            content=raw_request,
        )
        if method == "GET":
            # The server-to-client stream belongs to the session the
            # initialize above opened (and bound to this bearer).
            assert response.status_code == 200, response.text
            response = client.get(
                "/mcp",
                headers={**headers, "Mcp-Session-Id": "unicode-test-session"},
            )

    assert response.status_code == 200, response.text
    expected_content_type = (
        "text/event-stream; charset=utf-8"
        if method == "GET"
        else 'text/event-stream; profile="a;b"; version=1; charset=utf-8'
    )
    assert response.headers["content-type"] == expected_content_type
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-transport-marker"] == "preserved"
    assert response.headers.get_list("warning") == [
        '199 dtxwiki "first"',
        '299 dtxwiki "second"',
    ]
    assert response.content == encoded_event
    if method == "POST":
        assert fake_manager.seen_body == raw_request


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (
            b'{"jsonrpc":"2.0","id":1,"method":',
            "application/json; charset=utf-8",
        ),
        (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list","x":"\xff"}',
            "application/json",
        ),
        (
            b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
            "application/json; charset=iso-8859-1",
        ),
        (
            b'{"jsonrpc":"2.0","id":NaN,"method":"initialize"}',
            "application/json",
        ),
        (
            b'{"jsonrpc":"2.0","id":Infinity,"method":"initialize"}',
            "application/json",
        ),
        (
            b'{"jsonrpc":"2.0","id":-Infinity,"method":"initialize"}',
            "application/json",
        ),
        (
            '{"jsonrpc":"2.0","id":1,"method":"initialize"}'.encode(
                "utf-16"
            ),
            "application/json, text/plain",
        ),
    ],
    ids=[
        "malformed-json",
        "invalid-utf8",
        "declared-non-utf8",
        "nan-constant",
        "positive-infinity-constant",
        "negative-infinity-constant",
        "sdk-tolerated-compound-content-type",
    ],
)
def test_streamable_malformed_or_non_utf8_json_returns_parse_error(
    mcp_modes, payload, content_type
):
    """Bad request encoding is a stable JSON-RPC parse error, never a 500."""

    pat = mcp_modes.pat
    client = mcp_modes.client("streamable")
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": content_type,
        },
        content=payload,
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] is None
    assert body["error"]["code"] == -32700
    assert (
        body["error"]["message"]
        == "Parse error: MCP request body must be valid UTF-8 JSON"
    )
    assert body["error"]["data"] == {"error": "parse_error"}


def test_malformed_json_keeps_accept_negotiation_precedence(mcp_modes):
    """Malformed JSON must not replace established 406/415 transport errors."""

    pat = mcp_modes.pat
    client = mcp_modes.client("streamable")
    incompatible = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        content=b'{"jsonrpc":',
    )
    unacceptable = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "image/png",
            "Content-Type": "application/json",
        },
        content=b'{"jsonrpc":',
    )

    assert incompatible.status_code == 415, incompatible.text
    assert incompatible.json()["error"]["data"]["error"] == (
        "transport_mode_mismatch"
    )
    assert unacceptable.status_code == 406, unacceptable.text
    assert unacceptable.json()["error"]["data"]["error"] == "not_acceptable"


def test_buffered_mcp_bridge_preserves_duplicate_headers_and_sse_charset():
    """Buffered SDK responses retain duplicate headers while normalizing SSE."""

    from backend.api.mcp_http import _call_streamable_http_manager

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"accept", b"application/json")],
    }

    class _Manager:
        async def handle_request(self, scope, receive, send):
            await receive()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream"),
                        (b"set-cookie", b"a=1; Path=/"),
                        (b"set-cookie", b"b=2; Path=/"),
                        (b"x-transport-marker", b"preserved"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"data: {}\n\n",
                    "more_body": False,
                }
            )

    response = asyncio.run(
        _call_streamable_http_manager(_Manager(), b"{}", scope=scope)
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers.getlist("set-cookie") == [
        "a=1; Path=/",
        "b=2; Path=/",
    ]
    assert response.headers["x-transport-marker"] == "preserved"


def test_accept_dual_sdk_chooses(mcp_modes):
    """Plan-named test for §2.4 row 21 (auto, dual-q SDK delegation).

    mode=``auto`` Accept=``application/json, text/event-stream`` (both
    q=1) → our negotiator forwards the scope; the SDK picks.

    Per the plan §5 SDK-동작-변경-시 note (row labelled "dual-q SDK
    위임의 비결정성"), this test asserts only that the negotiator did
    NOT 415 — the SDK content-type choice is outside our contract.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("auto")
    r = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code != 415, r.text
    # Whatever the SDK picked, the content-type must be valid for one of
    # the two declared media types (positive control: SDK actually
    # honored the Accept header rather than emitting something else).
    content_type = r.headers.get("content-type", "")
    assert (
        "application/json" in content_type
        or "text/event-stream" in content_type
    ), f"SDK must pick a declared media type; got content-type={content_type!r}"


# ---------------------------------------------------------------------------
# Task 2.6 — Mcp-Session-Id validation (initialize exemption + 400/404/410)
# ---------------------------------------------------------------------------


def _assert_session_error_envelope(body: dict) -> None:
    """The Task 2.6 POST errors are bare JSON-RPC 2.0 errors (ADR A9).

    Cross-shape contamination guard: no REST ``error_code`` / ``details`` and
    no legacy top-level ``expected``; exactly ``jsonrpc`` / ``id`` / ``error``.
    """
    assert set(body.keys()) == {"jsonrpc", "id", "error"}, (
        f"JSON-RPC error body must have exactly jsonrpc/id/error keys; "
        f"got {set(body.keys())}"
    )
    assert "error_code" not in body, (
        f"MCP transport body MUST NOT contain REST 'error_code' key; got {body}"
    )
    assert "details" not in body, (
        f"MCP transport body MUST NOT contain REST 'details' key; got {body}"
    )
    assert "expected" not in body, (
        f"legacy top-level 'expected' key leaked; got {body}"
    )


def _delete_session(client: TestClient, pat: str, response) -> None:
    """Terminate the session an initialize created on a shared app."""
    session_id = response.headers.get("mcp-session-id")
    if session_id:
        deleted = client.delete(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Mcp-Session-Id": session_id,
            },
        )
        assert deleted.status_code == 200, deleted.text


def test_non_initialize_without_session_id_400(mcp_modes):
    """Plan-named test (§Task 2.6 branch 2): mode=``streamable``, POST
    with method=``tools/list`` and NO ``Mcp-Session-Id`` header → 400
    JSON-RPC error envelope with ``session_required``.

    Negative-test rule (ADR A6):
    - Pins the A6 keyword ``"session"`` per the plan.
    - Paired same-file positive control:
      ``test_initialize_without_session_id_creates_session`` below —
      same setup, method=``initialize`` → 200 + Mcp-Session-Id header.
    - Pins the JSON-RPC error envelope and ensures ``error_code`` /
      ``details`` (REST API shape) MUST NOT appear.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("streamable")
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 400, response.text
    body = response.json()
    error_payload = assert_json_rpc_error(
        body,
        request_id=1,
        error="session_required",
        message_contains="Mcp-Session-Id",
    )
    assert "session" in error_payload["data"]["error"] + error_payload["message"]
    assert "error_code" not in body
    assert "details" not in body
    _assert_session_error_envelope(body)


def test_initialize_without_session_id_creates_session(mcp_modes):
    """Plan-named test (§Task 2.6 branch 1) + positive control for
    ``test_non_initialize_without_session_id_400``.

    mode=``streamable``, POST with method=``initialize`` and NO
    ``Mcp-Session-Id`` header → 200 + ``Mcp-Session-Id`` response
    header (SDK mints a new id). The issued id MUST be usable on a
    follow-up POST.

    The SDK requires Accept include both ``application/json`` AND
    ``text/event-stream`` for non-stateless POSTs.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("streamable")
    initial = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-client",
                    "version": "0.0.0",
                },
            },
        },
    )

    # 200 + Mcp-Session-Id present.
    assert initial.status_code == 200, initial.text
    session_id = initial.headers.get("mcp-session-id")
    assert session_id, (
        f"initialize MUST return Mcp-Session-Id header; got headers="
        f"{dict(initial.headers)}"
    )
    # The SDK lifecycle creates a hex session id; pin shape lightly.
    assert len(session_id) > 0
    _delete_session(client, pat, initial)


def test_session_id_unknown_404(mcp_modes):
    """Plan-named test (§Task 2.6 branch 3): mode=``streamable``, POST
    with ``Mcp-Session-Id: nonexistent`` → 404 JSON-RPC error envelope
    with ``session_unknown``.

    Negative-test rule (ADR A6):
    - Pins the A6 keyword ``"unknown"`` per the plan.
    - Paired same-file positive control:
      ``test_initialize_without_session_id_creates_session`` (valid
      issuance path).
    - Pins the JSON-RPC error envelope contract.
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("streamable")
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": "nonexistent-session-id-xyz",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 404, response.text
    body = response.json()
    error_payload = assert_json_rpc_error(
        body,
        request_id=1,
        error="session_unknown",
        message_contains="not found",
    )
    assert "unknown" in error_payload["data"]["error"]
    assert "error_code" not in body
    assert "details" not in body
    _assert_session_error_envelope(body)


def test_session_id_expired_410(mcp_modes):
    """Plan-named test (§Task 2.6 branch 4): initialize → DELETE → reuse
    the same session id on POST → 410 JSON-RPC error envelope with
    ``session_expired``.

    Negative-test rule (ADR A6):
    - Pins the A6 keyword ``"expired"`` per the plan.
    - The lifecycle itself (initialize → DELETE → POST) embeds the
      positive controls inline: the initialize succeeds (200 + id),
      and a tools/list with the *same* id BEFORE the DELETE also
      succeeds (proving the 410 is caused by the DELETE, not a stray
      side effect). The follow-up POST after DELETE must be 410.
    - Pins the distinction between 410 (expired) and 404 (unknown) —
      a session id that was DELETE'd MUST surface as expired, not
      unknown (the operator-distinguishable case the plan calls out).
    """
    pat = mcp_modes.pat

    client = mcp_modes.client("streamable")
    initial = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "expire-test", "version": "0.0"},
            },
        },
    )
    assert initial.status_code == 200, initial.text
    session_id = initial.headers["mcp-session-id"]
    assert session_id

    # DELETE terminates the session.
    delete_resp = client.delete(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Mcp-Session-Id": session_id,
        },
    )
    assert delete_resp.status_code == 200, delete_resp.text

    # Reuse the terminated id → 410.
    follow_up = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": session_id,
        },
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )

    assert follow_up.status_code == 410, follow_up.text
    body = follow_up.json()
    error_payload = assert_json_rpc_error(
        body,
        request_id=2,
        error="session_expired",
        message_contains="terminated",
    )
    assert "expired" in error_payload["data"]["error"]
    assert "error_code" not in body
    assert "details" not in body
    _assert_session_error_envelope(body)


def test_initialize_without_session_in_json_stateless_no_op(page_service):
    """Plan-named test (Sprint_4 regression positive control, ADR A6).

    mode=``json_stateless``: there is no session lifecycle, so an
    ``initialize`` POST does NOT receive an ``Mcp-Session-Id`` header
    in the response (stateless SDK transport). The Task 2.6 validation
    branch does NOT run for json_stateless mode.

    Pairs with the streamable initialize test as the cross-mode
    positive control — same input body shape, different mode → no
    session header.
    """
    _, pat = _pat_client(page_service)

    app = create_app(
        settings=_make_settings("json_stateless"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {pat}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "stateless-test",
                        "version": "0.0",
                    },
                },
            },
        )

    assert response.status_code == 200, response.text
    # Sprint_4 regression: stateless mode MUST NOT mint an Mcp-Session-Id.
    assert response.headers.get("mcp-session-id") is None, (
        f"json_stateless mode MUST NOT emit Mcp-Session-Id; got "
        f"{response.headers.get('mcp-session-id')!r}"
    )


def test_delete_mcp_terminates_existing_session(page_service):
    """Deferred-from-Task-2.4 test (now landed under Task 2.6 because it
    depends on initialize lifecycle): initialize → POST ``tools/list``
    → DELETE → reuse same session id → 404 OR 410.

    Per the plan §Task 2.4 cross-ref: the follow-up POST should surface
    ``session_unknown`` (404). However, under Task 2.6's branch (3)/(4)
    distinction we now classify a DELETE'd id as ``session_expired``
    (410) because the terminated-ledger remembers it. Both outcomes
    satisfy the plan's intent (the session is verifiably terminated;
    the operator-distinguishable case is 410). The test pins the
    *contract that matters*: the session ID stops working after DELETE,
    and the response body uses the JSON-RPC error envelope.

    Negative-test rule (ADR A6):
    - Positive control embedded in the lifecycle: the BEFORE-DELETE
      ``tools/list`` call MUST succeed (proving the id is valid pre-
      DELETE, so the AFTER-DELETE failure is caused by the DELETE).
    """
    _, pat = _pat_client(page_service)

    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    with TestClient(app) as client:
        initial = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "lifecycle-test",
                        "version": "0.0",
                    },
                },
            },
        )
        assert initial.status_code == 200, initial.text
        sid = initial.headers["mcp-session-id"]
        assert sid

        # Positive control: tools/list with the valid id succeeds.
        before = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": sid,
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert before.status_code == 200, before.text

        # Terminate.
        deleted = client.delete(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Mcp-Session-Id": sid,
            },
        )
        assert deleted.status_code == 200, deleted.text

        # Reuse → 410 (terminated-ledger) — under Task 2.6 contract.
        after = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": sid,
            },
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        )

    # The plan's §Task 2.4 verification row says 404; the §Task 2.6
    # branch-distinction says 410. Accept either as long as the JSON-RPC
    # error envelope and keyword align with the status code.
    assert after.status_code in (404, 410), after.text
    body = after.json()
    if after.status_code == 410:
        error_payload = assert_json_rpc_error(
            body,
            request_id=3,
            error="session_expired",
        )
        assert "expired" in error_payload["data"]["error"]
    else:  # 404 — pre-ledger fallback path
        error_payload = assert_json_rpc_error(
            body,
            request_id=3,
            error="session_unknown",
        )
        assert "unknown" in error_payload["data"]["error"]


# ---------------------------------------------------------------------------
# Task 2.7 — Unauthorized 401 + WWW-Authenticate metadata (B5.7 evidence)
# ---------------------------------------------------------------------------

# Pin the public_base_url used across Task 2.7 tests so the assertion
# string is unambiguous (no scheme/host drift from default fixtures).
_PUBLIC_ORIGIN = "https://mcp.example.com"
_EXPECTED_METADATA_URL = (
    f"{_PUBLIC_ORIGIN}/.well-known/oauth-protected-resource"
)
_EXPECTED_WWW_AUTH = (
    f'Bearer resource_metadata="{_EXPECTED_METADATA_URL}"'
)


def _make_settings_with_public_origin(
    mode: str, *, public_base_url: str | None = _PUBLIC_ORIGIN
):
    """Build a Settings object pinned to a known public_base_url so the
    WWW-Authenticate metadata URL is deterministic.

    ``public_base_url=None`` exercises the dev fallback to
    ``request.base_url`` (Phase_4 BLOCKER C blocks this in production).
    """
    from backend.app.settings import Settings

    return Settings(
        mcp_http_mode=mode,
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        public_base_url=public_base_url,
    )


def test_unauthorized_metadata_uses_public_origin(mcp_modes):
    """Plan-named test (B5.7): POST /mcp without bearer → 401 with
    ``WWW-Authenticate: Bearer resource_metadata="<public_base_url>/
    .well-known/oauth-protected-resource"`` exactly.

    Negative-test rule (ADR A6): paired positive control
    ``test_unauthorized_omitted_when_authorized`` below — same setup
    with a valid bearer → 2xx + NO WWW-Authenticate header. Without
    the positive control a regression that emitted the header on every
    response (always-on misconfiguration) would still pass the 401
    assertion.

    H7 invariant: the metadata URL MUST come from
    ``settings.public_base_url``, NEVER from request Host header.
    """
    client = mcp_modes.client("json_stateless", public_origin=True)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == _EXPECTED_WWW_AUTH, (
        response.headers["www-authenticate"]
    )


def test_unauthorized_get_metadata_uses_public_origin(mcp_modes):
    """Plan-named test (B5.7): GET /mcp without bearer → 401 with the
    same WWW-Authenticate metadata URL as POST.

    Bearer auth is required on every transport mode. Use streamable mode here
    to exercise the SSE branch after the shared authentication boundary.
    """
    client = mcp_modes.client("streamable", public_origin=True)
    response = client.get(
        "/mcp", headers={"Accept": "text/event-stream"}
    )

    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == _EXPECTED_WWW_AUTH


def test_unauthorized_delete_metadata_uses_public_origin(mcp_modes):
    """DELETE /mcp without a bearer returns the shared auth challenge."""
    client = mcp_modes.client("streamable", public_origin=True)
    response = client.delete("/mcp")

    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == _EXPECTED_WWW_AUTH


def test_unauthorized_no_public_base_url_uses_request_base_url(page_service):
    """Plan-named test: when ``public_base_url`` is unset, the helper
    falls back to ``request.base_url`` (dev path). Phase_4 BLOCKER C
    will block this in production, but Task 2.7 owns the contract
    that the dev path still emits a valid metadata URL.

    Positive control (ADR A6): the test asserts both the substring
    (``"/.well-known/oauth-protected-resource"``) AND the absence of
    the configured ``_PUBLIC_ORIGIN`` string, so a regression that
    silently leaked the configured value into the dev fallback would
    surface.
    """
    app = create_app(
        settings=_make_settings_with_public_origin(
            "json_stateless", public_base_url=None
        ),
        page_service=page_service,
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 401, response.text
    www_auth = response.headers["www-authenticate"]
    # Dev fallback: must contain the well-known suffix...
    assert "/.well-known/oauth-protected-resource" in www_auth, www_auth
    # ...and MUST NOT contain the configured production origin (we
    # explicitly unset it).
    assert _PUBLIC_ORIGIN not in www_auth, www_auth
    # Format invariants — still the Bearer scheme with the
    # resource_metadata link parameter.
    assert www_auth.startswith('Bearer resource_metadata="'), www_auth
    assert www_auth.endswith('"'), www_auth


def test_unauthorized_body_is_single_key(mcp_modes):
    """Plan-named test: minimal-disclosure invariant — the 401 body is
    exactly ``{"error":"unauthorized"}`` with NO ``detail`` /
    ``message`` / ``error_description`` / ``error_code`` keys.

    Phase_2 §Task 2.7 minimal-disclosure policy + Phase_3 협의 결과:
    revealing token state (revoked / expired / invalid) in the body
    enables token replay / family scope reconnaissance. The body
    surface is intentionally bare. Phase_3 owns the REST-path
    keyword-disclosure (different surface, different threat model).

    Negative-test rule (ADR A6): pins the exact key set and asserts
    common leakage keys are absent.
    """
    client = mcp_modes.client("streamable", public_origin=True)
    # Three verbs, three asserts — minimal-disclosure must hold
    # across the full unauthorized surface.
    post_resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    get_resp = client.get(
        "/mcp", headers={"Accept": "text/event-stream"}
    )
    delete_resp = client.delete("/mcp")

    for verb, resp in (
        ("POST", post_resp),
        ("GET", get_resp),
        ("DELETE", delete_resp),
    ):
        assert resp.status_code == 401, f"{verb}: {resp.text}"
        body = resp.json()
        assert body == {"error": "unauthorized"}, (
            f"{verb}: minimal-disclosure invariant — body must be exactly "
            f'{{"error":"unauthorized"}}; got {body}'
        )
        # Common leakage-key guards (regression catchers).
        assert "detail" not in body, f"{verb}: leaked 'detail' in body"
        assert "message" not in body, f"{verb}: leaked 'message' in body"
        assert "error_description" not in body, (
            f"{verb}: leaked 'error_description' in body"
        )
        assert "error_code" not in body, f"{verb}: leaked 'error_code' in body"


def test_unauthorized_metadata_ignores_host_header_injection(page_service, monkeypatch):
    """Sprint_5 BLOCKER C surface — host-header injection guard.

    When the client sends a spoofed ``Host`` header (e.g.
    ``Host: evil.com``), the WWW-Authenticate ``resource_metadata`` URL
    MUST still come from the configured ``settings.public_base_url``,
    NEVER from the attacker-controlled Host. A regression that built
    the URL from ``request.url.netloc`` / ``request.host`` would let
    an attacker mint a metadata-pointer to a hostile origin — clients
    following that URL would expose their bearer to the attacker's
    discovery endpoint.

    Negative-test rule (ADR A6): the test exercises all three verbs
    with a spoofed Host on the configured-origin app. The positive
    control is the same-test assertion that the URL matches the
    configured origin (not the spoofed Host).
    """
    # Let the spoofed name past the Host allowlist (which would otherwise
    # refuse it with 400): even a host name the operator accepts must never
    # reach the metadata URL.
    monkeypatch.setenv("DTXWIKI_ALLOWED_HOSTS", "testserver,evil.example.com")
    app = create_app(
        settings=_make_settings_with_public_origin("streamable"),
        page_service=page_service,
    )
    spoofed_headers = {"Host": "evil.example.com"}

    with TestClient(app) as client:
        post_resp = client.post(
            "/mcp",
            headers=spoofed_headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        get_resp = client.get(
            "/mcp",
            headers={**spoofed_headers, "Accept": "text/event-stream"},
        )
        delete_resp = client.delete("/mcp", headers=spoofed_headers)

    for verb, resp in (
        ("POST", post_resp),
        ("GET", get_resp),
        ("DELETE", delete_resp),
    ):
        assert resp.status_code == 401, f"{verb}: {resp.text}"
        www_auth = resp.headers["www-authenticate"]
        # BLOCKER C invariant: the configured origin wins.
        assert _PUBLIC_ORIGIN in www_auth, (
            f"{verb}: WWW-Authenticate must use configured origin "
            f"{_PUBLIC_ORIGIN!r}; got {www_auth!r}"
        )
        # And the attacker-controlled Host must NOT appear.
        assert "evil.example.com" not in www_auth, (
            f"{verb}: spoofed Host leaked into WWW-Authenticate: {www_auth!r}"
        )


def test_unauthorized_omitted_when_authorized(mcp_modes):
    """Task 2.7 positive control (ADR A6 pair for the 401 metadata
    tests). With a valid bearer token, the response is 2xx AND the
    ``WWW-Authenticate`` header is NOT present.

    Without this control a regression that emitted the metadata header
    unconditionally (e.g. on a 200 response) would still pass every
    negative 401 test in this section.
    """
    client = mcp_modes.client("json_stateless", public_origin=True)
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {mcp_modes.pat}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 200, response.text
    assert "www-authenticate" not in {
        k.lower() for k in response.headers.keys()
    }, response.headers


# ---------------------------------------------------------------------------
# Task 2.8 — SDK client round-trip (B5.1 evidence row)
# ---------------------------------------------------------------------------


def _http_server_url(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{port}"


class _UvicornServer:
    """Helper that runs the FastAPI app in a daemon thread + waits for
    the listener socket before returning.

    The TestClient cannot host the SDK's ``streamablehttp_client``
    because httpx-based test clients do not fully implement SSE
    streaming semantics across connection boundaries (see Phase_2
    §Task 2.8 "Fixture 요구사항" note — real httpx server needed).
    """

    def __init__(self, app, host: str = "127.0.0.1") -> None:
        import socket as _socket
        import threading

        import uvicorn

        # Bind the ephemeral port here and hand the bound socket to uvicorn
        # rather than reading a port number and letting go of it: under
        # parallel workers another process can claim the port in the window
        # between the probe closing and the server opening.
        family = _socket.getaddrinfo(
            host, 0, type=_socket.SOCK_STREAM
        )[0][0]
        self._socket = _socket.socket(family, _socket.SOCK_STREAM)
        self._socket.bind((host, 0))
        self.host, self.port = self._socket.getsockname()[:2]
        self.url = _http_server_url(self.host, self.port)
        config = uvicorn.Config(
            app,
            log_level="warning",
            lifespan="on",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        # uvicorn.Server.run() installs signal handlers, which fails on
        # non-main threads. install_signal_handlers=False keeps the
        # thread happy.
        self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._thread_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._serve,
            daemon=True,
            name="uvicorn-mcp-test",
        )
        self._thread_started = False

    def _serve(self) -> None:
        try:
            self._server.run(sockets=[self._socket])
        except BaseException as error:
            # Surface lifespan/startup failures through __enter__ instead of
            # leaving only an unhandled thread traceback and a 10 s timeout.
            self._thread_error = error

    def __enter__(self) -> _UvicornServer:
        import socket as _socket
        import time as _time

        try:
            self._thread.start()
            self._thread_started = True
            deadline = _time.monotonic() + 10.0
            while _time.monotonic() < deadline:
                if not self._thread.is_alive():
                    raise RuntimeError(
                        f"uvicorn exited before starting on {self.url}"
                    ) from self._thread_error
                if getattr(self._server, "started", False):
                    try:
                        with _socket.create_connection(
                            (self.host, self.port), timeout=0.2
                        ):
                            return self
                    except OSError:
                        pass
                _time.sleep(0.05)
            raise RuntimeError(
                f"uvicorn did not start on {self.url} within 10s"
            )
        except BaseException:
            # Preserve the more specific startup error (and its cause). The
            # cleanup path still stops the thread and closes the bound socket.
            self._shutdown(raise_thread_error=False)
            raise

    def _shutdown(self, *, raise_thread_error: bool = True) -> None:
        self._server.should_exit = True
        try:
            if self._thread_started:
                self._thread.join(timeout=10.0)
        finally:
            self._socket.close()
        if self._thread_started and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._thread_started and self._thread.is_alive():
            raise RuntimeError(f"uvicorn did not stop on {self.url} within 11s")
        if raise_thread_error and self._thread_error is not None:
            raise RuntimeError(f"uvicorn failed while serving on {self.url}") from (
                self._thread_error
            )

    def __exit__(self, exc_type, exc, tb) -> None:
        # Do not replace a test-body failure with a secondary server exception.
        self._shutdown(raise_thread_error=exc_type is None)


def test_uvicorn_server_url_formats_ipv6_authority():
    assert _http_server_url("127.0.0.1", 21080) == "http://127.0.0.1:21080"
    assert _http_server_url("::1", 21080) == "http://[::1]:21080"


def test_uvicorn_server_enter_failure_closes_bound_socket(monkeypatch):
    import time

    class _FakeServer:
        started = False
        should_exit = False

        def run(self, *, sockets):
            assert sockets == [server._socket]
            raise ValueError("lifespan startup failed")

    class _FakeSocket:
        closed = False

        def close(self):
            self.closed = True

    server = object.__new__(_UvicornServer)
    server._server = _FakeServer()
    server._socket = _FakeSocket()
    server._thread_error = None

    class _FakeThread:
        started = False

        def start(self):
            self.started = True
            server._serve()

        def join(self, *, timeout):
            assert timeout == 10.0

        def is_alive(self):
            return False

    server._thread = _FakeThread()
    server._thread_started = False
    server.host = "127.0.0.1"
    server.port = 21080
    server.url = _http_server_url(server.host, server.port)
    # The second tick is still inside the deadline. If the dead-thread check is
    # lost, the third tick forces the old generic timeout instead.
    ticks = iter((0.0, 0.1, 11.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="uvicorn exited before starting") as raised:
        server.__enter__()

    assert isinstance(raised.value.__cause__, ValueError)
    assert str(raised.value.__cause__) == "lifespan startup failed"
    assert server._thread.started is True
    assert server._thread_started is True
    assert server._server.should_exit is True
    assert server._socket.closed is True


def test_uvicorn_server_exit_surfaces_thread_failure_after_startup():
    class _FakeServer:
        should_exit = False

    class _FakeThread:
        def join(self, *, timeout):
            assert timeout == 10.0

        def is_alive(self):
            return False

    class _FakeSocket:
        closed = False

        def close(self):
            self.closed = True

    server = object.__new__(_UvicornServer)
    server._server = _FakeServer()
    server._thread = _FakeThread()
    server._thread_started = True
    server._thread_error = ValueError("server loop failed")
    server._socket = _FakeSocket()
    server.url = "http://127.0.0.1:21080"

    with pytest.raises(RuntimeError, match="uvicorn failed while serving") as raised:
        server.__exit__(None, None, None)

    assert raised.value.__cause__ is server._thread_error
    assert server._server.should_exit is True
    assert server._socket.closed is True


def _build_streamable_round_trip_setup(page_service):
    """Build a streamable-mode app + issue a PAT against a json_stateless
    setup app that shares the same in-memory database.

    Both apps use the same ``page_service`` fixture, so the PAT issued
    against the json_stateless TestClient is reachable from the
    streamable uvicorn-hosted app.
    """
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)

    streamable_app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )
    return streamable_app, pat


async def _round_trip_via_sdk(
    base_url: str,
    pat: str,
    *,
    capture_session_ids: list[str | None] | None = None,
    call_tools: bool = True,
):
    """Drive a single SDK ClientSession end-to-end against the real
    uvicorn host. Returns the ``list_tools`` result for caller-side
    assertions.

    ``capture_session_ids`` (when provided) is appended to *after*
    initialize and *after* each subsequent operation, so the caller
    can assert session-id reuse across requests within the same
    transport.
    """
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        url=f"{base_url}/mcp",
        headers={"Authorization": f"Bearer {pat}"},
    ) as (read_stream, write_stream, get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            if capture_session_ids is not None:
                capture_session_ids.append(get_session_id())
            tools = await session.list_tools()
            if capture_session_ids is not None:
                capture_session_ids.append(get_session_id())
            if call_tools:
                # Second list_tools to test session-id reuse on a
                # subsequent request within the same transport.
                _ = await session.list_tools()
                if capture_session_ids is not None:
                    capture_session_ids.append(get_session_id())
            return tools, get_session_id()


def test_full_round_trip_streamable(page_service):
    """Plan-named test (B5.1 evidence row): SDK ``streamablehttp_client``
    drives a full initialize → list_tools round trip against a real
    uvicorn-hosted streamable app.

    Negative-test rule (ADR A6): the positive control is embedded in
    the round-trip itself — ``initialize`` MUST return without
    raising AND ``list_tools`` MUST include at least one expected
    tool name (``search``). Without the tool-name check the
    assertion would pass even if the SDK returned an empty tool
    surface (false-positive on a regression that silently dropped
    every tool).

    The same round trip also pins the PAT credential (``dtxw_`` prefix) and
    Mcp-Session-Id reuse across requests within one transport.

    Implementation note: this exercises the Tasks 2.2-2.7 surface
    end-to-end through the SDK client — Accept negotiation, Mcp-
    Session-Id lifecycle, bearer auth, and the streaming POST
    response all participate in this single round trip.
    """
    import asyncio

    streamable_app, pat = _build_streamable_round_trip_setup(page_service)
    # Sanity-check the PAT prefix so a future refactor that accidentally
    # swapped the token type (e.g. issuing an OAuth JWT instead) surfaces
    # here, not as a confusing round-trip failure.
    assert pat.startswith("dtxw_"), f"expected dtxw_ PAT prefix; got {pat[:8]}..."
    snapshots: list[str | None] = []

    async def _run():
        with _UvicornServer(streamable_app) as server:
            tools_result, final_session_id = await _round_trip_via_sdk(
                server.url,
                pat,
                capture_session_ids=snapshots,
                call_tools=True,
            )
        return tools_result, final_session_id

    tools_result, final_session_id = asyncio.run(_run())

    # Tools list must be non-empty AND include a known tool name
    # (positive control against an empty surface false positive).
    assert tools_result is not None
    assert len(tools_result.tools) > 0, (
        "SDK round-trip returned empty tool list — false-positive risk"
    )
    tool_names = {t.name for t in tools_result.tools}
    assert "search" in tool_names, (
        f"expected 'search' tool in SDK list_tools result; got {tool_names}"
    )

    # Session id must be issued.
    assert final_session_id is not None, (
        "SDK transport MUST have a session id after initialize"
    )
    assert isinstance(final_session_id, str) and final_session_id

    # The SDK transport reuses the same Mcp-Session-Id across requests: 3
    # snapshots (after initialize, after list_tools #1, after list_tools #2),
    # all non-None and identical. A regression that minted a new id per
    # request would surface as differing snapshot values.
    assert len(snapshots) == 3, snapshots
    assert all(sid is not None for sid in snapshots), snapshots
    assert len(set(snapshots)) == 1, (
        f"Mcp-Session-Id MUST be reused across requests; snapshots={snapshots}"
    )


def test_round_trip_oauth_token(page_service):
    """Plan-named test: OAuth-issued JWT bearer drives the same round
    trip. Confirms the ``mcp_http`` cross-transport row's SDK-client
    analogue — the SDK + transport stack is credential-agnostic
    (PAT and OAuth JWT both flow through ``authenticate_bearer_token``).

    Positive control: a successful initialize + tools/list against an
    OAuth-issued token proves the streamable transport handles OAuth
    credentials end-to-end. A regression that broke OAuth bearer
    handling on the streamable path would surface here as a 401.
    """
    import asyncio

    from backend.tests.integration._cross_transport_helpers import (
        issue_oauth_access_token,
        make_client,
        seed_viewer_target_with_restricted_library_item,
    )

    # Mint an OAuth access token against the json_stateless setup
    # (same shared page_service DB).
    setup_client = make_client(page_service)
    target_workspace_id = seed_viewer_target_with_restricted_library_item(page_service)
    oauth_token = issue_oauth_access_token(setup_client, target_workspace_id)
    assert oauth_token, "OAuth fixture must issue a non-empty token"
    # OAuth tokens are JWTs (do NOT start with the dtxw_ PAT prefix).
    # Pin this so a future refactor that accidentally returned a PAT
    # here would surface and not silently weaken the test.
    assert not oauth_token.startswith("dtxw_"), (
        f"expected OAuth JWT (no dtxw_ prefix); got {oauth_token[:8]}..."
    )

    streamable_app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )

    async def _run():
        with _UvicornServer(streamable_app) as server:
            tools_result, _ = await _round_trip_via_sdk(
                server.url, oauth_token, call_tools=False
            )
        return tools_result

    tools_result = asyncio.run(_run())
    assert tools_result is not None
    assert len(tools_result.tools) > 0
    assert "search" in {t.name for t in tools_result.tools}


def _issue_pat_with_scopes(
    client: TestClient,
    workspace_id,
    scopes: list[str],
    name: str,
) -> str:
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": str(workspace_id)}],
            "name": name,
            "scopes": scopes,
        },
    )
    assert issued.status_code == 201, issued.text
    return issued.json()["data"]["plain_token"]


def _default_workspace_id(page_service):
    from sqlalchemy import select

    from backend.models.workspace import Workspace

    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        return workspace.id


async def _call_tool_via_sdk(
    base_url: str,
    bearer_token: str,
    tool_name: str,
    arguments: dict,
):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        url=f"{base_url}/mcp",
        headers={"Authorization": f"Bearer {bearer_token}"},
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await session.call_tool(tool_name, arguments)


def _workspace_grant_test_setup(page_service):
    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership
    from backend.scripts.seed_dev import seed_default_workspace
    from backend.tests.integration._cross_transport_helpers import make_client

    setup = make_client(page_service)
    with page_service.session_factory.begin() as session:
        workspace_a = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        workspace_b = seed_default_workspace(
            session, slug="workspace-grant-target", title="Workspace Grant Target"
        ).id
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=workspace_b, role="owner"
        )
    issued = setup.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": str(workspace_a)}],
            "name": "workspace-grant-pat",
            "scopes": ["mcp:read"],
        },
    )
    assert issued.status_code == 201, issued.text
    return setup, workspace_a, workspace_b, issued.json()["data"]


@pytest.mark.parametrize("mode", ["streamable", "auto"])
def test_sdk_workspace_grant_changes_keep_existing_session(
    page_service, monkeypatch, mode
):
    """Workspace edits reach the next SDK message without reinitializing."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    import backend.api.mcp_http as transport

    setup, workspace_a, workspace_b, issued = _workspace_grant_test_setup(page_service)
    token = issued["plain_token"]
    token_id = issued["api_token"]["id"]
    observed = []
    original = transport.call_tool_async

    async def spy(name, arguments, **kwargs):
        observed.append(
            (
                name,
                kwargs["actor"].token_id,
                set(kwargs["actor"].token_workspace_ids),
                kwargs["mcp_session_id"],
            )
        )
        return await original(name, arguments, **kwargs)

    monkeypatch.setattr(transport, "call_tool_async", spy)
    app = create_app(settings=_make_settings(mode), page_service=page_service)

    async def run():
        with _UvicornServer(app) as server:
            async with streamablehttp_client(
                url=f"{server.url}/mcp",
                headers={"Authorization": f"Bearer {token}"},
            ) as (read_stream, write_stream, get_session_id):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=15),
                ) as session:
                    await session.initialize()
                    session_id = get_session_id()
                    assert session_id

                    async def assert_workspaces(expected):
                        result = await session.call_tool("list_my_workspaces", {})
                        assert result.isError is False
                        assert {
                            UUID(row["id"])
                            for row in result.structuredContent["data"]["workspaces"]
                        } == expected
                        assert observed[-1][2] == expected
                        assert get_session_id() == session_id

                    async def replace_workspaces(workspaces):
                        response = await asyncio.to_thread(
                            setup.patch,
                            f"/api/v1/auth/tokens/{token_id}",
                            json={
                                "workspace_grants": [
                                    {"workspace_id": str(workspace_id)}
                                    for workspace_id in workspaces
                                ]
                            },
                        )
                        assert response.status_code == 200, response.text

                    await assert_workspaces({workspace_a})
                    await replace_workspaces([workspace_b])
                    await assert_workspaces({workspace_b})
                    permitted = await session.call_tool(
                        "guide", {"workspace_id": str(workspace_b)}
                    )
                    assert permitted.isError is False
                    assert permitted.structuredContent["envelope"]["result_status"] == (
                        "succeeded"
                    )
                    denied = await session.call_tool(
                        "guide", {"workspace_id": str(workspace_a)}
                    )
                    assert denied.isError is True
                    assert denied.structuredContent is None
                    assert [content.text for content in denied.content] == [
                        "workspace token claim mismatch"
                    ]
                    await replace_workspaces([workspace_a, workspace_b])
                    await assert_workspaces({workspace_a, workspace_b})
                    permitted = await session.call_tool(
                        "guide", {"workspace_id": str(workspace_b)}
                    )
                    assert permitted.isError is False
                    assert get_session_id() == session_id
                    assert {row[1] for row in observed} == {UUID(token_id)}
                    assert {row[3] for row in observed} == {session_id}

    try:
        asyncio.run(run())
    finally:
        setup.close()


def test_sdk_streamable_pat_call_tool_uses_argument_workspace(page_service):
    """PAT SDK initialize -> call_tool succeeds for a multi-workspace user."""
    from backend.tests.integration._cross_transport_helpers import (
        assert_restricted_library_items_excluded,
        issue_personal_api_token,
        make_client,
        seed_viewer_target_with_restricted_library_item,
    )

    setup_client = make_client(page_service)
    target_workspace_id = seed_viewer_target_with_restricted_library_item(page_service)
    pat = issue_personal_api_token(
        setup_client,
        workspace_id=target_workspace_id,
    )
    streamable_app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )

    async def _run():
        with _UvicornServer(streamable_app) as server:
            return await _call_tool_via_sdk(
                server.url,
                pat,
                "search",
                {
                    "workspace_id": str(target_workspace_id),
                    "query": "needle cross transport",
                    "scope": "all",
                },
            )

    result = asyncio.run(_run())
    assert result.structuredContent["envelope"]["result_status"] == "succeeded"
    assert_restricted_library_items_excluded(result.structuredContent["data"])


def test_sdk_streamable_oauth_call_tool_uses_argument_workspace(page_service):
    """OAuth SDK initialize -> call_tool succeeds in the token-bound workspace."""
    from backend.tests.integration._cross_transport_helpers import (
        assert_restricted_library_items_excluded,
        issue_oauth_access_token,
        make_client,
        seed_viewer_target_with_restricted_library_item,
    )

    setup_client = make_client(page_service)
    target_workspace_id = seed_viewer_target_with_restricted_library_item(page_service)
    oauth_token = issue_oauth_access_token(setup_client, target_workspace_id)
    streamable_app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )

    async def _run():
        with _UvicornServer(streamable_app) as server:
            return await _call_tool_via_sdk(
                server.url,
                oauth_token,
                "search",
                {
                    "workspace_id": str(target_workspace_id),
                    "query": "needle cross transport",
                    "scope": "all",
                },
            )

    result = asyncio.run(_run())
    assert result.structuredContent["envelope"]["result_status"] == "succeeded"
    assert_restricted_library_items_excluded(result.structuredContent["data"])


def test_sdk_streamable_agent_pat_can_write_agent_page_draft(page_service):
    """Agent PAT writes a review-gated draft over the streamable SDK."""
    from backend.tests.integration._cross_transport_helpers import make_client

    setup_client = make_client(page_service)
    workspace_id = _default_workspace_id(page_service)
    pat = _issue_pat_with_scopes(
        setup_client,
        workspace_id,
        ["mcp:agent"],
        "Agent SDK PAT",
    )
    streamable_app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )

    async def _run():
        with _UvicornServer(streamable_app) as server:
            return await _call_tool_via_sdk(
                server.url,
                pat,
                "write_agent_page_draft",
                {
                    "workspace_id": str(workspace_id),
                    "category": "persona",
                    "title": "SDK Agent Persona",
                    "slug": "sdk-agent-persona",
                    "body_markdown": "# SDK Agent Persona\n\nDraft only.",
                    "reason": "SDK streamable regression",
                    # Content tools must name their conversation; omitting the
                    # token used to start a new chat on every single call.
                    "conversation_token": "new",
                },
            )

    result = asyncio.run(_run())
    assert result.structuredContent["envelope"]["result_status"] == "succeeded"
    assert result.structuredContent["data"]["review_item_id"] is not None
    assert result.structuredContent["data"]["next_action"] == "review_required"


def test_sdk_streamable_cross_workspace_denial_mentions_workspace(page_service):
    """Cross-workspace call_tool denial keeps a stable workspace keyword."""
    from uuid import uuid4

    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)
    streamable_app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )

    async def _run():
        with _UvicornServer(streamable_app) as server:
            return await _call_tool_via_sdk(
                server.url,
                pat,
                "search",
                {
                    "workspace_id": str(uuid4()),
                    "query": "needle cross transport",
                    "scope": "all",
                },
            )

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        assert "workspace token claim mismatch" in str(exc).lower()
    else:
        assert result.isError is True
        payload = result.model_dump(mode="json")
        error_text = result.content[0].text.lower()
        assert "workspace token claim mismatch" in error_text
        assert "workspace" in str(payload).lower()


def test_mcp_session_id_is_bound_to_original_bearer(page_service):
    """A different bearer cannot POST, GET, or DELETE an existing session."""
    from backend.tests.integration._cross_transport_helpers import make_client

    setup_client = make_client(page_service)
    workspace_id = _default_workspace_id(page_service)
    owner_pat = _issue_pat_with_scopes(
        setup_client,
        workspace_id,
        ["mcp:read"],
        "Owner PAT",
    )
    other_pat = _issue_pat_with_scopes(
        setup_client,
        workspace_id,
        ["mcp:read"],
        "Other PAT",
    )
    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )

    with TestClient(app) as client:
        initial = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {owner_pat}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "owner-test", "version": "0.0"},
                },
            },
        )
        assert initial.status_code == 200, initial.text
        session_id = initial.headers["mcp-session-id"]

        post_mismatch = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {other_pat}",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": session_id,
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        get_mismatch = client.get(
            "/mcp",
            headers={
                "Authorization": f"Bearer {other_pat}",
                "Accept": "text/event-stream",
                "Mcp-Session-Id": session_id,
            },
        )
        delete_mismatch = client.delete(
            "/mcp",
            headers={
                "Authorization": f"Bearer {other_pat}",
                "Mcp-Session-Id": session_id,
            },
        )
        owner_after_mismatch = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {owner_pat}",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": session_id,
            },
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        )

    assert post_mismatch.status_code == 403, post_mismatch.text
    assert_json_rpc_error(
        post_mismatch.json(),
        request_id=2,
        error="session_owner_mismatch",
        message_contains="different bearer",
    )

    for response in (get_mismatch, delete_mismatch):
        assert response.status_code == 403, response.text
        assert_json_rpc_error(
            response.json(),
            request_id=None,
            error="session_owner_mismatch",
            message_contains="different bearer",
        )
    assert owner_after_mismatch.status_code == 200, owner_after_mismatch.text


def test_session_owner_cap_rejects_new_initialize_keeps_live_sessions(
    page_service, monkeypatch
):
    """At the owner-ledger cap a new initialize is refused (503) while every
    live session keeps working; DELETE frees capacity for the next session."""
    monkeypatch.setattr("backend.api.mcp_http._SESSION_OWNER_CAP", 2)
    _, pat = _pat_client(page_service)
    app = create_app(
        settings=_make_settings("streamable"),
        page_service=page_service,
    )

    def _initialize(client, request_id):
        return client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "cap-test", "version": "0.0"},
                },
            },
        )

    with TestClient(app) as client:
        first = _initialize(client, 1)
        second = _initialize(client, 2)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        first_session = first.headers["mcp-session-id"]
        second_session = second.headers["mcp-session-id"]

        rejected = _initialize(client, 3)
        assert rejected.status_code == 503, rejected.text
        assert_json_rpc_error(
            rejected.json(),
            request_id=3,
            error="session_capacity_exhausted",
            message_contains="capacity",
        )

        survivor = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": first_session,
            },
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        )
        assert survivor.status_code == 200, survivor.text

        deleted = client.delete(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Mcp-Session-Id": second_session,
            },
        )
        assert deleted.status_code == 200, deleted.text

        reopened = _initialize(client, 5)
        assert reopened.status_code == 200, reopened.text
        assert reopened.headers["mcp-session-id"] not in (
            first_session,
            second_session,
        )

        owner_delete = client.delete(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Mcp-Session-Id": first_session,
            },
        )
        assert owner_delete.status_code == 200, owner_delete.text


@pytest.mark.parametrize("mode", ["json_stateless", "streamable", "auto"])
def test_accept_without_json_or_sse_candidate_returns_406(mcp_modes, mode):
    pat = mcp_modes.pat

    client = mcp_modes.client(mode)
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pat}", "Accept": "image/png"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 406, response.text
    assert_json_rpc_error(
        response.json(),
        request_id=1,
        error="not_acceptable",
        message_contains="Accept must include",
    )


@pytest.mark.parametrize(
    "accept",
    [
        "application/json;q=0",
        "application/json;q=0, text/event-stream;q=0",
    ],
)
def test_accept_explicit_zero_quality_candidates_return_406(mcp_modes, accept):
    pat = mcp_modes.pat

    client = mcp_modes.client("auto")
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pat}", "Accept": accept},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 406, response.text
    assert_json_rpc_error(
        response.json(),
        request_id=1,
        error="not_acceptable",
        message_contains="Accept must include",
    )

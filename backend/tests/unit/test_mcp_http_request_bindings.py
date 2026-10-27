from contextvars import Context
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from mcp.server import Server as MCPServer
from mcp.server.lowlevel.server import request_ctx

from backend.api.mcp_http import _dispatcher_request_bindings
from backend.policy.auth import ActorContext


@pytest.mark.parametrize(
    "invalid_binding",
    [
        "missing_context",
        "missing_request",
        "wrong_request_type",
        "missing_actor",
        "wrong_actor_type",
        "wrong_app",
        "cookie_session_actor",
        "missing_session_id",
        "empty_session_id",
    ],
)
def test_dispatcher_request_binding_rejects_incomplete_metadata(invalid_binding):
    """A prior actor cannot authorize an absent or malformed HTTP binding."""

    app = FastAPI()
    dispatcher = MCPServer("request-binding-test")
    actor = ActorContext(
        actor_kind="human",
        actor_id="request-binding-actor",
        user_id=uuid4(),
        token_kind="api_token",
        token_id=uuid4(),
    )
    session_id = uuid4().hex
    session_factory = object()
    page_service = SimpleNamespace(session_factory=session_factory)
    app.state.session_factory = session_factory
    app.state.page_service = page_service
    scope = {
        "type": "http",
        "app": app,
        "headers": [(b"mcp-session-id", session_id.encode())],
        "state": {"actor": actor},
    }
    original_request = Request(scope)
    metadata_request = Request(dict(scope))
    assert metadata_request is not original_request
    assert metadata_request.state.actor is actor

    # The SDK constructs a fresh Request from the authenticated scope. It
    # must resolve the current actor and services without object identity.
    token = request_ctx.set(SimpleNamespace(request=metadata_request))
    try:
        binding = _dispatcher_request_bindings(dispatcher, app)
        assert binding.actor is actor
        assert binding.page_service is page_service
        assert binding.session_factory is session_factory
        assert binding.mcp_session_id == session_id
    finally:
        request_ctx.reset(token)

    if invalid_binding == "missing_context":
        with pytest.raises(RuntimeError, match="request binding"):
            Context().run(_dispatcher_request_bindings, dispatcher, app)
        return

    request = metadata_request
    if invalid_binding == "missing_request":
        request = None
    elif invalid_binding == "wrong_request_type":
        request = SimpleNamespace(app=app, state=metadata_request.state)
    elif invalid_binding == "missing_actor":
        scope["state"].pop("actor")
    elif invalid_binding == "wrong_actor_type":
        scope["state"]["actor"] = SimpleNamespace(token_kind="api_token")
    elif invalid_binding == "wrong_app":
        request = Request({**scope, "app": FastAPI()})
    elif invalid_binding == "cookie_session_actor":
        scope["state"]["actor"] = replace(actor, token_kind="session")
    elif invalid_binding == "missing_session_id":
        request = Request({**scope, "headers": []})
    elif invalid_binding == "empty_session_id":
        request = Request({**scope, "headers": [(b"mcp-session-id", b"")]})

    token = request_ctx.set(SimpleNamespace(request=request))
    try:
        with pytest.raises(RuntimeError, match="request binding"):
            _dispatcher_request_bindings(dispatcher, app)
    finally:
        request_ctx.reset(token)

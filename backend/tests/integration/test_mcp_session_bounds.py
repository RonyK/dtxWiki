"""Streamable MCP sessions stay bounded (0.1.1 contract C8, finding M-5).

Every request the SDK sees without an Mcp-Session-Id opens a session in its
pool. A bearer could grow that pool without limit with GETs or with opening
requests that never succeed, because only a successful initialize ever gets
an owner entry (and with it the owner-ledger cap).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.mcp_http import MCP_SESSION_IDLE_TIMEOUT_SECONDS
from backend.app.main import create_app
from backend.app.settings import Settings

pytestmark = pytest.mark.mcp_http

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "session-bounds", "version": "0.0.0"},
    },
}


def _streamable_app(page_service):
    settings = Settings(
        mcp_http_mode="streamable",
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="mcp-session-bounds-test-secret",
        public_base_url="http://testserver",
    )
    return create_app(settings=settings, page_service=page_service)


def _pat(page_service) -> str:
    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    return issue_personal_api_token(make_client(page_service))


def _pool(app) -> dict:
    return app.state.mcp_session_manager._server_instances


def test_sdk_body_cap_follows_the_mcp_reader(monkeypatch):
    """SDKs that add their own request-body cap (4 MiB from mcp 1.30) get
    /mcp's bound instead, or base64 image uploads would start failing."""

    import backend.api.mcp_http as mcp_http

    built: list[dict] = []
    monkeypatch.setattr(
        mcp_http, "StreamableHTTPSessionManager", lambda **kwargs: built.append(kwargs)
    )

    monkeypatch.setattr(
        mcp_http, "_SESSION_MANAGER_PARAMETERS", frozenset({"app", "stateless"})
    )
    mcp_http._new_session_manager(app=None, stateless=True)
    monkeypatch.setattr(
        mcp_http,
        "_SESSION_MANAGER_PARAMETERS",
        frozenset({"app", "stateless", "max_request_body_size"}),
    )
    mcp_http._new_session_manager(app=None, stateless=True)

    assert "max_request_body_size" not in built[0]
    assert built[1]["max_request_body_size"] == mcp_http.MCP_MAX_REQUEST_BODY_BYTES


def test_sessions_expire_when_idle(page_service):
    app = _streamable_app(page_service)

    with TestClient(app):
        manager = app.state.mcp_session_manager

    assert manager.session_idle_timeout == MCP_SESSION_IDLE_TIMEOUT_SECONDS == 1800


def test_get_without_session_id_does_not_accumulate_sessions(page_service):
    pat = _pat(page_service)
    app = _streamable_app(page_service)

    with TestClient(app) as client:
        for _ in range(25):
            response = client.get(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "text/event-stream",
                },
            )
            assert response.status_code == 400, response.text
            assert response.json()["error"]["data"]["error"] == "session_required"
        assert len(_pool(app)) == 0


def test_refused_initialize_leaves_no_session_behind(page_service):
    pat = _pat(page_service)
    app = _streamable_app(page_service)

    with TestClient(app) as client:
        for _ in range(10):
            # A browser page on another origin: the transport's DNS-rebinding
            # check refuses it after the SDK has already opened a session.
            refused = client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/json, text/event-stream",
                    "Origin": "http://rebind.attacker.example",
                },
                json=INITIALIZE,
            )
            assert refused.status_code == 403, refused.text
        assert len(_pool(app)) == 0
        assert app.state.mcp_session_owners == {}

        # Positive control: a successful initialize keeps its session, owned.
        accepted = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json, text/event-stream",
            },
            json=INITIALIZE,
        )
        assert accepted.status_code == 200, accepted.text
        session_id = accepted.headers["mcp-session-id"]
        assert set(_pool(app)) == {session_id}
        assert set(app.state.mcp_session_owners) == {session_id}


def test_deleted_sessions_leave_the_pool(page_service):
    """Initialize/DELETE cycles stay within the owner ledger's bound."""

    pat = _pat(page_service)
    app = _streamable_app(page_service)
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/json, text/event-stream",
    }

    with TestClient(app) as client:
        for _ in range(10):
            opened = client.post("/mcp", headers=headers, json=INITIALIZE)
            assert opened.status_code == 200, opened.text
            session_id = opened.headers["mcp-session-id"]
            deleted = client.delete(
                "/mcp",
                headers={**headers, "Mcp-Session-Id": session_id},
            )
            assert deleted.status_code == 200, deleted.text
        assert len(_pool(app)) == 0

        # A deleted session still reads as terminated, not unknown.
        expired = client.get(
            "/mcp",
            headers={**headers, "Mcp-Session-Id": session_id},
        )
        assert expired.status_code == 410, expired.text

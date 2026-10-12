"""Host header allowlist (0.1.1 contract C3, readiness finding B-4).

A DNS-rebinding page reaches the server with its own host name in ``Host``;
only loopback names, the DTXWIKI_PUBLIC_BASE_URL host and DTXWIKI_ALLOWED_HOSTS
entries are answered, on the REST API, the SPA and /mcp alike.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.services.page_service import PageService
from backend.tests.conftest import captured_log_messages

REBINDING_HOST = "rebind.attacker.example"


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": "host-allowlist-test-secret",
        # An explicit value: the suite-wide "testserver" allowance must not
        # leak into these tests.
        "allowed_hosts": "",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(scope="module")
def shared_page_service(_empty_sqlite_schema):
    """One schema-only service for the whole module.

    The allowlist middleware answers before authentication and the database,
    so apps that differ only in the Host header can share their backing store.
    """

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    _empty_sqlite_schema.backup(connection)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        creator=lambda: connection,
        poolclass=StaticPool,
    )
    try:
        yield PageService(sessionmaker(bind=engine, expire_on_commit=False))
    finally:
        engine.dispose()
        connection.close()


@pytest.fixture(scope="module")
def spa_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("static")
    (root / "index.html").write_text("<!doctype html><title>dtxWiki</title>")
    return root


@pytest.fixture(scope="module")
def app_for(shared_page_service, spa_root):
    """Apps cached by settings and static root; the Host header varies per client."""

    apps: dict[tuple, object] = {}

    def _app_for(*, static_root=None, **overrides):
        key = (static_root, tuple(sorted(overrides.items())))
        if key not in apps:
            apps[key] = create_app(
                settings=_settings(**overrides),
                page_service=shared_page_service,
                static_root=static_root,
            )
        return apps[key]

    return _app_for


def _client(app_for, host: str, *, static_root=None, **overrides) -> TestClient:
    return TestClient(
        app_for(static_root=static_root, **overrides), base_url=f"http://{host}"
    )


def _assert_refused(response) -> None:
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error_code"] == "invalid_request"
    assert "DTXWIKI_ALLOWED_HOSTS" in body["message"]


def test_foreign_host_is_refused_on_rest_spa_and_mcp(app_for, spa_root):
    client = _client(app_for, f"{REBINDING_HOST}:21080", static_root=spa_root)

    _assert_refused(client.get("/api/v1/auth/capabilities"))
    _assert_refused(client.get("/"))
    _assert_refused(client.get("/login"))
    # Refused before authentication: not the 401 bearer challenge.
    _assert_refused(
        client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    )


def test_rebinding_page_cannot_sign_up(app_for):
    """The first step of the B-4 chain: account creation on the victim's server."""

    client = _client(app_for, REBINDING_HOST)

    response = client.post(
        "/api/v1/auth/signup",
        json={"email": "attacker@example.com", "password": "correct-password"},
    )

    _assert_refused(response)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:21080", "localhost:21173", "LOCALHOST", "[::1]:21080", "127.0.0.1."],
)
def test_loopback_hosts_are_allowed(app_for, spa_root, host):
    # The Host header is set explicitly: TestClient cannot derive its ASGI
    # server address from a bracketed IPv6 base URL.
    client = _client(app_for, "127.0.0.1", static_root=spa_root)
    headers = {"host": host}

    assert client.get("/api/v1/auth/capabilities", headers=headers).status_code == 200
    assert client.get("/", headers=headers).status_code == 200


@pytest.mark.parametrize("host", ["wiki.example.com", "wiki.example.com:443"])
def test_public_base_url_host_is_allowed(app_for, host):
    client = _client(
        app_for,
        host,
        public_base_url="https://wiki.example.com",
        session_cookie_secure=False,
    )

    assert client.get("/api/v1/auth/capabilities").status_code == 200
    _assert_refused(
        TestClient(client.app, base_url="http://other.example.com").get(
            "/api/v1/auth/capabilities"
        )
    )


def test_configured_hosts_are_allowed(app_for):
    allowed = "lan-box.local, 10.0.0.5:21080"

    for host in ("lan-box.local:21080", "10.0.0.5"):
        client = _client(app_for, host, allowed_hosts=allowed)
        assert client.get("/api/v1/auth/capabilities").status_code == 200
    _assert_refused(
        _client(app_for, "10.0.0.6", allowed_hosts=allowed).get(
            "/api/v1/auth/capabilities"
        )
    )


def test_health_probes_answer_any_host(app_for):
    client = _client(app_for, "172.28.0.5:21080")

    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code in {200, 503}
    # Only the two probes: other /health routes still check the host.
    _assert_refused(client.get("/health/storage-topology"))


def test_wildcard_disables_the_check_and_warns(page_service):
    with captured_log_messages("dtxwiki.startup", logging.WARNING) as messages:
        app = create_app(
            settings=_settings(allowed_hosts="*"), page_service=page_service
        )
    client = TestClient(app, base_url=f"http://{REBINDING_HOST}")

    assert client.get("/api/v1/auth/capabilities").status_code == 200
    assert any("DTXWIKI_ALLOWED_HOSTS=*" in message for message in messages)


@pytest.mark.parametrize(
    "headers",
    [[], [(b"host", b"127.0.0.1"), (b"host", b"127.0.0.1")]],
    ids=["missing", "duplicated"],
)
def test_missing_or_duplicated_host_header_is_refused(app_for, headers):
    app = app_for()
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/auth/capabilities",
        "raw_path": b"/api/v1/auth/capabilities",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 40000),
        "server": ("127.0.0.1", 21080),
    }
    asyncio.run(app(scope, receive, send))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 400


def test_mcp_transport_gets_dns_rebinding_protection(page_service):
    settings = _settings(
        mcp_http_mode="streamable",
        public_base_url="https://wiki.example.com",
        allowed_hosts="lan-box.local",
    )
    app = create_app(settings=settings, page_service=page_service)

    with TestClient(app, base_url="http://127.0.0.1:21080"):
        security = app.state.mcp_session_manager.security_settings

    assert security.enable_dns_rebinding_protection is True
    for expected in ("127.0.0.1:*", "localhost", "[::1]:*", "wiki.example.com:*"):
        assert expected in security.allowed_hosts
    assert "lan-box.local:*" in security.allowed_hosts
    assert REBINDING_HOST not in " ".join(security.allowed_hosts)
    assert "http://localhost:*" in security.allowed_origins
    assert "https://wiki.example.com" in security.allowed_origins


def test_mcp_transport_protection_follows_the_wildcard(page_service):
    settings = _settings(mcp_http_mode="streamable", allowed_hosts="*")
    app = create_app(settings=settings, page_service=page_service)

    with TestClient(app, base_url="http://127.0.0.1:21080"):
        security = app.state.mcp_session_manager.security_settings

    assert security.enable_dns_rebinding_protection is False


def test_invalid_allowed_hosts_entry_fails_startup(page_service):
    with pytest.raises(RuntimeError, match="DTXWIKI_ALLOWED_HOSTS"):
        create_app(
            settings=_settings(allowed_hosts="wiki.example.com/path"),
            page_service=page_service,
        )

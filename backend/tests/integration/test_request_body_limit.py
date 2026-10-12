"""Request bodies are bounded before routing and authentication (contract C5).

FastAPI reads a route's body before it runs the authentication dependency,
so without a bound an anonymous client could make the server buffer or parse
an arbitrarily large body (readiness finding M-1). These tests use small
configured bounds so the bodies stay small.
"""

from __future__ import annotations

import asyncio
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.middleware.request_body_limit import AUTH_REQUEST_BODY_LIMIT_BYTES
from backend.api.read import LIBRARY_UPLOAD_MULTIPART_OVERHEAD_BYTES
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.context_media.domain.policy import CONTEXT_MEDIA_UPLOAD_POLICY
from backend.services.page_service import PageService

SMALL_LIMIT = 4096


@pytest.fixture(scope="module")
def shared_page_service(_empty_sqlite_schema):
    """One schema-only service for the module.

    The bounds are decided before routing and authentication, so apps that
    differ only in their body-limit settings can share a store.
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
def apps(shared_page_service):
    """Apps cached by their (immutable) settings overrides."""

    cache: dict[tuple, object] = {}

    def _build(**overrides):
        key = tuple(sorted(overrides.items()))
        if key not in cache:
            values = {
                "database_url": "sqlite+pysqlite:///:memory:",
                "session_secret": "request-body-limit-test-secret",
                "max_request_body_bytes": SMALL_LIMIT,
            }
            values.update(overrides)
            cache[key] = create_app(
                settings=Settings(**values), page_service=shared_page_service
            )
        return cache[key]

    return _build


def _client(apps, **overrides) -> TestClient:
    return TestClient(apps(**overrides))


def _assert_too_large(response, error_code: str = "request_too_large") -> dict:
    assert response.status_code == 413, response.text
    body = response.json()
    assert body["error_code"] == error_code
    return body


def test_oversized_body_is_refused_before_authentication(apps):
    client = _client(apps)
    oversized = {"name": "x" * (SMALL_LIMIT * 2)}

    response = client.post("/api/v1/workspaces", json=oversized)

    # Not the 401 an anonymous request would otherwise get after FastAPI had
    # read and parsed the whole body.
    _assert_too_large(response)
    # Positive control: under the bound the same anonymous request reaches
    # authentication.
    assert client.post("/api/v1/workspaces", json={"name": "x"}).status_code == 401


def _asgi_post(app, path: str, headers: list[tuple[bytes, bytes]], receive):
    """Drive the ASGI app directly: TestClient buffers the whole request body
    before the app sees it, which would hide whether streaming stopped."""

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:21080"), *headers],
        "client": ("127.0.0.1", 40000),
        "server": ("127.0.0.1", 21080),
    }
    asyncio.run(app(scope, receive, send))
    return sent


def test_chunked_body_without_content_length_is_cut_off(apps):
    app = apps()
    chunk = b"x" * 1024
    total_chunks = 1000
    pulled = 0

    async def long_body():
        nonlocal pulled
        pulled += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": pulled < total_chunks,
        }

    sent = _asgi_post(
        app,
        "/api/v1/workspaces",
        [(b"content-type", b"application/json")],
        long_body,
    )

    assert sent[0]["status"] == 413
    # Reading stopped at the first chunk past the bound, not at the end of
    # the 1000-chunk stream.
    assert pulled <= SMALL_LIMIT // len(chunk) + 1


def test_declared_oversize_is_answered_without_reading_the_body(apps):
    app = apps()
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"{}", "more_body": False}

    sent = _asgi_post(
        app,
        "/api/v1/auth/login",
        [
            (b"content-type", b"application/json"),
            (b"content-length", str(10 * 1024 * 1024 * 1024).encode()),
        ],
        receive,
    )

    assert sent[0]["status"] == 413
    assert receive_calls == 0


@pytest.mark.parametrize(
    ("path", "request_kwargs"),
    [
        ("/api/v1/auth/login", {"json": {"email": "a@example.com", "password": "p"}}),
        ("/oauth/register", {"json": {"redirect_uris": ["http://127.0.0.1/cb"]}}),
        (
            "/oauth/token",
            {"data": {"grant_type": "refresh_token", "refresh_token": "t"}},
        ),
    ],
    ids=["auth-login-json", "oauth-register-json", "oauth-token-form"],
)
def test_auth_and_oauth_bodies_get_a_small_bound(apps, path, request_kwargs):
    # The general bound is far larger; the auth/OAuth endpoints parse their
    # bodies synchronously and get a tight one of their own.
    client = _client(apps, max_request_body_bytes=16 * 1024 * 1024)
    padding = "p" * (AUTH_REQUEST_BODY_LIMIT_BYTES + 1)
    if "json" in request_kwargs:
        body = {**request_kwargs["json"], "padding": padding}
        response = client.post(path, json=body)
    else:
        body = {**request_kwargs["data"], "padding": padding}
        response = client.post(path, data=body)

    _assert_too_large(response)
    # Positive control: the unpadded request is processed (whatever its
    # outcome, it is not refused for size).
    assert client.post(path, **request_kwargs).status_code != 413


def _multipart(size: int) -> dict:
    return {"file": ("notes.txt", b"n" * size, "text/plain")}


def test_library_upload_route_gets_its_own_larger_bound(apps):
    client = _client(apps, max_request_body_bytes=SMALL_LIMIT)
    workspace_id = uuid4()

    # 16x the general bound, far below the Library upload cap: reaches
    # authentication instead of being refused for size.
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files=_multipart(SMALL_LIMIT * 16),
    )

    assert response.status_code == 401, response.text


def test_library_upload_beyond_its_cap_is_refused_before_authentication(
    apps,
):
    cap = 1024
    client = _client(apps, library_upload_hard_cap_bytes=cap)
    workspace_id = uuid4()
    oversized = cap + LIBRARY_UPLOAD_MULTIPART_OVERHEAD_BYTES + 1

    for path in (
        f"/api/v1/workspaces/{workspace_id}/library/items",
        f"/api/v1/workspaces/{workspace_id}/library/items/{uuid4()}/versions",
    ):
        body = _assert_too_large(
            client.post(path, files=_multipart(oversized)),
            "library_upload_too_large",
        )
        assert body["limit_bytes"] == cap
        assert body["settings_link"] == "/settings?section=library"


def test_context_media_upload_keeps_its_policy_bound(apps):
    client = _client(apps, max_request_body_bytes=SMALL_LIMIT)
    path = f"/api/v1/workspaces/{uuid4()}/context/pages/{uuid4()}/media"

    # Above the general bound, within the Context media policy.
    within = client.post(path, files=_multipart(SMALL_LIMIT * 16))
    assert within.status_code == 401, within.text

    beyond = client.post(
        path,
        files=_multipart(CONTEXT_MEDIA_UPLOAD_POLICY.max_request_body_bytes + 1),
    )
    _assert_too_large(beyond, "media_too_large")


def test_mcp_keeps_its_own_reader(apps):
    # /mcp authenticates from headers before reading anything and bounds the
    # body itself, answering with JSON-RPC errors; the REST bound stays out.
    client = _client(apps, max_request_body_bytes=SMALL_LIMIT)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "x" * (SMALL_LIMIT * 2)},
    )

    assert response.status_code == 401, response.text

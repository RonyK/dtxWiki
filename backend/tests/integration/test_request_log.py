import json
import logging

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings


@pytest.fixture
def request_log_lines():
    lines: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("dtxwiki.request")
    previous_disabled = logger.disabled
    handler = _ListHandler()
    logger.disabled = False
    logger.addHandler(handler)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.disabled = previous_disabled


def test_log_line_contains_10_fields(request_log_lines):
    client = TestClient(create_app(settings=Settings()))

    response = client.get("/health/live", headers={"X-Request-ID": "req-1"})
    record, _raw = _last_log_record(request_log_lines)

    assert response.status_code == 200
    assert set(record) == {
        "request_id",
        "actor_kind",
        "actor_id",
        "client_id",
        "workspace_id",
        "method",
        "route",
        "status",
        "latency_ms",
        "remote_ip",
    }
    assert record["request_id"] == "req-1"
    assert record["route"] == "/health/live"
    assert record["status"] == 200


def test_log_does_not_include_authorization_header(request_log_lines):
    client = TestClient(create_app(settings=Settings()))

    client.get(
        "/health/live",
        headers={"Authorization": "Bearer dtx_pat_secret_xxx"},
    )
    record, raw = _last_log_record(request_log_lines)

    assert record["method"] == "GET"
    assert record["status"] == 200
    assert "authorization" not in record
    assert "Authorization" not in record
    assert "dtx_pat_secret" not in raw
    assert "request_id" in raw


def test_log_uses_proxy_header_remote_ip(request_log_lines):
    # Chain: client 203.0.113.42 -> proxy 10.0.0.1 -> our peer (testclient). Both
    # intermediaries are trusted, so the originating client is resolved (the
    # rightmost-untrusted hop). A client-injected leftmost value would NOT be
    # trusted unless every hop to its right is allow-listed.
    client = TestClient(
        create_app(
            settings=Settings(
                behind_proxy=True,
                forwarded_allow_ips="testclient,10.0.0.1",
            )
        )
    )

    client.get(
        "/health/live",
        headers={"X-Forwarded-For": "203.0.113.42, 10.0.0.1"},
    )
    record, _raw = _last_log_record(request_log_lines)

    assert record["remote_ip"] == "203.0.113.42"


def test_log_ignores_proxy_header_from_untrusted_peer(request_log_lines):
    client = TestClient(create_app(settings=Settings(behind_proxy=True)))

    client.get(
        "/health/live",
        headers={"X-Forwarded-For": "203.0.113.42, 10.0.0.1"},
    )
    record, _raw = _last_log_record(request_log_lines)

    assert record["remote_ip"] == "testclient"


def test_log_keeps_oauth_code_out(request_log_lines):
    client = TestClient(create_app(settings=Settings()))

    response = client.post(
        "/oauth/token",
        json={
            "grant_type": "unsupported",
            "client_id": "oauth-client",
            "code": "secret_code_xxx",
        },
    )
    record, raw = _last_log_record(request_log_lines)

    assert response.status_code == 400
    assert record["route"] == "/oauth/token"
    assert record["status"] == 400
    assert "secret_code" not in raw
    assert "oauth/token" in raw


def _last_log_record(lines: list[str]):
    line = lines[-1]
    return json.loads(line), line

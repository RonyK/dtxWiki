"""`dtxwiki serve`: network-bind warning (C4) and query-free logging (C6)."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.cli import main as cli_main
from backend.tests.conftest import captured_log_messages
from backend.tests.utils.environment import clear_ambient_dtxwiki_env


@pytest.fixture
def serve(monkeypatch, tmp_path):
    """Invoke `dtxwiki serve` with the server, bootstrap and browser stubbed."""

    clear_ambient_dtxwiki_env(monkeypatch)
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "data"))
    calls: dict = {}
    monkeypatch.setattr(cli_main, "create_app", lambda **_kwargs: object())
    monkeypatch.setattr(cli_main, "bootstrap_app", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main.uvicorn, "run", lambda _app, **kwargs: calls.update(uvicorn=kwargs)
    )
    monkeypatch.setattr(
        cli_main.webbrowser, "open", lambda url: calls.update(browser=url)
    )

    def invoke(*arguments: str):
        result = CliRunner().invoke(
            cli_main.app, ["serve", "--port", "21999", *arguments]
        )
        assert result.exit_code == 0, result.output
        return result, calls

    return invoke


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "wiki-box"])
def test_solo_local_on_a_network_address_warns(serve, host):
    result, calls = serve("--host", host, "--no-open")

    assert "WARNING" in result.stderr
    assert "solo_local" in result.stderr
    assert "without TLS" in result.stderr
    assert "can create an account" in result.stderr
    assert "DTXWIKI_ALLOWED_HOSTS" in result.stderr
    # A warning, not a refusal: the container image binds 0.0.0.0.
    assert calls["uvicorn"]["host"] == host


def test_warning_omits_open_signup_when_signup_is_disabled(serve, monkeypatch):
    monkeypatch.setenv("DTXWIKI_SIGNUP_ENABLED", "false")

    result, _ = serve("--host", "0.0.0.0", "--no-open")

    assert "solo_local" in result.stderr
    assert "can create an account" not in result.stderr


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_bind_does_not_warn(serve, host):
    result, _ = serve("--host", host, "--no-open")

    assert "WARNING" not in result.stderr


def test_remote_modes_do_not_get_the_solo_local_warning(serve, monkeypatch):
    monkeypatch.setenv("DTXWIKI_APP_MODE", "team")
    monkeypatch.setenv("DTXWIKI_SESSION_SECRET", "team-session-secret-" + "t" * 40)

    result, _ = serve("--host", "0.0.0.0", "--no-open")

    assert "WARNING" not in result.stderr


def test_uvicorn_access_log_is_disabled(serve):
    _, calls = serve("--host", "127.0.0.1", "--no-open")

    # uvicorn's access log records the request target, query string and all.
    assert calls["uvicorn"]["access_log"] is False


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "http://127.0.0.1:21999"),
        ("::", "http://[::1]:21999"),
        ("127.0.0.1", "http://127.0.0.1:21999"),
    ],
)
def test_browser_opens_an_allowed_host_for_wildcard_binds(serve, host, expected):
    _, calls = serve("--host", host, "--open")

    assert calls["browser"] == expected


def test_request_log_records_route_templates_not_query_strings(page_service):
    """The log that replaces uvicorn's access log never carries the token."""

    app = create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="request-log-test-secret",
        ),
        page_service=page_service,
    )
    token = "one-time-reset-token-value"
    with captured_log_messages("dtxwiki.request", logging.INFO) as records:
        TestClient(app).get(f"/api/v1/auth/capabilities?token={token}")

    assert records, "the request log recorded nothing"
    entry = json.loads(records[-1])
    assert entry["route"] == "/api/v1/auth/capabilities"
    assert all(token not in record for record in records)

"""Framework-raised errors must use the dtxwiki envelope, not {"detail"}."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.tests.utils.environment import clear_ambient_dtxwiki_env


def _clear_dtxwiki_env(monkeypatch):
    clear_ambient_dtxwiki_env(monkeypatch)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'envelope.sqlite'}",
        auto_bootstrap=False,
    )


def _assert_envelope(body, *, error_code, status):
    assert body["error_code"] == error_code
    assert body["envelope"]["error_code"] == error_code
    assert body["envelope"]["result_status"] == status
    assert "message" in body
    assert "detail" not in body


def test_method_not_allowed_uses_error_envelope(tmp_path, monkeypatch):
    # 405 is raised by Starlette's router as starlette.exceptions.HTTPException
    # (not fastapi.HTTPException); it must still get the envelope.
    _clear_dtxwiki_env(monkeypatch)
    client = TestClient(create_app(settings=_settings(tmp_path)))

    response = client.post("/health/live")

    assert response.status_code == 405
    assert "GET" in response.headers["allow"]
    _assert_envelope(
        response.json(), error_code="internal_error", status="failed"
    )


def test_api_fallback_404_keeps_error_envelope(tmp_path, monkeypatch):
    # Regression guard: fastapi.HTTPException subclasses the Starlette class,
    # so the single Starlette-keyed handler must still cover it via MRO.
    _clear_dtxwiki_env(monkeypatch)
    client = TestClient(create_app(settings=_settings(tmp_path)))

    response = client.get("/api/v1/definitely-not-a-route")

    assert response.status_code == 404
    _assert_envelope(response.json(), error_code="not_found", status="failed")


def test_unhandled_exception_returns_envelope_500(tmp_path, monkeypatch):
    _clear_dtxwiki_env(monkeypatch)

    def boom(_session_factory):
        raise RuntimeError("kaboom: secret internals")

    # _check_db's own try/except only guards resolving the session factory,
    # not the ping call itself (backend/api/health.py) -- an unhandled raise
    # from here is real, reachable behavior, and a convenient vehicle for
    # this generic envelope test now that the deprecated /health smoke route
    # (which used to serve this purpose) is gone.
    monkeypatch.setattr("backend.api.health.database_ping", boom)
    client = TestClient(
        create_app(settings=_settings(tmp_path)),
        raise_server_exceptions=False,
    )

    response = client.get("/health/ready")

    assert response.status_code == 500
    _assert_envelope(
        response.json(), error_code="internal_error", status="failed"
    )
    assert response.json()["message"] == "internal server error"
    assert "kaboom" not in response.text


def test_schema_mismatch_exception_returns_actionable_envelope(tmp_path, monkeypatch):
    _clear_dtxwiki_env(monkeypatch)

    def schema_mismatch(_session_factory):
        raise OperationalError(
            "select job.next_attempt_at from job",
            {},
            sqlite3.OperationalError("no such column: job.next_attempt_at"),
        )

    monkeypatch.setattr("backend.api.health.database_ping", schema_mismatch)
    client = TestClient(
        create_app(settings=_settings(tmp_path)),
        raise_server_exceptions=False,
    )

    response = client.get("/health/ready")

    assert response.status_code == 500
    body = response.json()
    _assert_envelope(
        body, error_code="database_schema_mismatch", status="failed"
    )
    assert "reset the dev database" in body["message"]
    assert "dtxwiki bootstrap" in body["message"]
    assert "job.next_attempt_at" not in response.text

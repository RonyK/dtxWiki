from contextlib import asynccontextmanager
from functools import cache
from importlib.metadata import version
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings


def test_extra_lifespan_composes_with_mcp_lifespan(tmp_path):
    """R7-1: an overlay lifespan ADDS startup/shutdown behavior — the core
    MCP lifespan (session-manager install) must still run, and /health/ready
    must stay green. Replacing the core lifespan used to 503 ready."""

    events: list[str] = []

    @asynccontextmanager
    async def overlay(app):
        events.append("overlay-start")
        yield
        events.append("overlay-stop")

    app = create_app(settings=_settings(tmp_path), extra_lifespan=overlay)
    app.state.app_version_synced = True
    with TestClient(app) as client:
        assert events == ["overlay-start"]
        # Core MCP lifespan ran inside the overlay: the streamable session
        # manager and MCP contextvars are installed.
        assert getattr(app.state, "mcp_session_manager", None) is not None
        assert getattr(app.state, "mcp_actor_var", None) is not None
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["components"]["mcp"] == "ok"
    assert events == ["overlay-start", "overlay-stop"]


def test_live_root_path_200_without_a_database_check(monkeypatch, tmp_path):
    def fail_if_called(_session_factory):
        raise AssertionError("liveness must not check DB")

    monkeypatch.setattr("backend.api.health.database_ping", fail_if_called)
    client = TestClient(create_app(settings=_settings(tmp_path)))

    response = client.get("/health/live")

    assert response.status_code == 200
    # The installed distribution's version, read independently of the app's
    # settings plumbing, so a release bump never has to edit this test.
    assert response.json() == {"status": "alive", "version": version("dtxwiki")}


def test_ready_root_path_happy_200(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    app.state.app_version_synced = True
    # Default mcp_http_mode is "streamable"; the lifespan-scoped session
    # manager makes the mcp component report "ok". TestClient here does not
    # run the lifespan, so register the manager the same way the running app
    # would (mirrors test_ready_mcp_ok_when_manager_registered_in_auto).
    app.state.mcp_session_manager = object()
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "components": {
            "db": "ok",
            "app_version": "ok",
            "mcp": "ok",
            "oauth_jwk": "ok",
            "worker": "n/a",
        },
    }


def test_ready_db_probe_reuses_cached_session_factory(tmp_path, monkeypatch):
    # Orchestrators poll /health/ready; the DB ping must reuse the memoized
    # app-wide session factory instead of building a throwaway engine per probe.
    from backend.app import container as app_container

    calls = []
    real_factory = app_container.make_session_factory_from_settings

    def counting_factory(settings):
        calls.append(settings)
        return real_factory(settings)

    monkeypatch.setattr(
        app_container, "make_session_factory_from_settings", counting_factory
    )
    app = create_app(settings=_settings(tmp_path))
    app.state.app_version_synced = True
    app.state.mcp_session_manager = object()
    client = TestClient(app)

    first = client.get("/health/ready")
    second = client.get("/health/ready")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(calls) == 1
    assert hasattr(app.state, "session_factory")


def test_ready_db_down_503(tmp_path):
    missing_dir = tmp_path / "missing"
    app = create_app(
        settings=_settings(
            tmp_path,
            database_url=f"sqlite+pysqlite:///{missing_dir / 'db.sqlite'}",
        )
    )
    app.state.app_version_synced = True
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert response.json()["components"]["db"] == "error"


def test_ready_database_not_configured_is_valid_unhealthy_response(tmp_path):
    app = create_app(settings=_settings(tmp_path, database_url=""))
    app.state.app_version_synced = True
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert response.json()["components"]["db"] == "not_configured"


def test_ready_app_version_unsynced_503(tmp_path):
    client = TestClient(create_app(settings=_settings(tmp_path)))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert response.json()["components"]["app_version"] == "unsynced"
    # A readiness answer is JSON, never the SPA index page.
    assert response.headers["content-type"].startswith("application/json")
    assert "text/html" not in response.headers["content-type"]


def test_ready_app_version_drift_503(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    app.state.app_version_synced = False
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert response.json()["components"]["app_version"] == "drift"


def test_ready_mcp_manager_missing_in_streamable_503(tmp_path):
    app = create_app(settings=_settings(tmp_path, mcp_http_mode="streamable"))
    app.state.app_version_synced = True
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["components"]["mcp"] == "manager_missing"


def test_ready_mcp_manager_missing_in_auto_503(tmp_path):
    app = create_app(settings=_settings(tmp_path, mcp_http_mode="auto"))
    app.state.app_version_synced = True
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["components"]["mcp"] == "manager_missing"


def test_ready_mcp_ok_when_manager_registered_in_auto(tmp_path):
    app = create_app(settings=_settings(tmp_path, mcp_http_mode="auto"))
    app.state.app_version_synced = True
    app.state.mcp_session_manager = object()
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["components"]["mcp"] == "ok"


def test_ready_mcp_na_in_json_stateless_200(tmp_path):
    app = create_app(settings=_settings(tmp_path, mcp_http_mode="json_stateless"))
    app.state.app_version_synced = True
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["components"]["mcp"] == "n/a"


def test_ready_oauth_jwk_ephemeral_unhealthy(tmp_path):
    app = create_app(
        settings=_settings(tmp_path, oauth_jwt_private_key_pem=None)
    )
    app.state.app_version_synced = True
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["components"]["oauth_jwk"] == "ephemeral_unsafe"
    assert "ephemeral_unsafe" in response.text


def test_ready_response_shape_matches_adr_a7(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    app.state.app_version_synced = True
    client = TestClient(app)

    response = client.get("/health/ready")
    body = response.json()

    assert set(body) == {"status", "components"}
    assert set(body["components"]) == {
        "db", "app_version", "mcp", "oauth_jwk", "worker"
    }


def test_managed_worker_must_be_ready(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    app.state.app_version_synced = True
    app.state.mcp_session_manager = object()
    app.state.worker_mode = "managed"
    client = TestClient(app)

    for ready, expected_status, expected_component in (
        (False, 503, "error"),
        (True, 200, "ok"),
        (False, 503, "error"),
    ):
        app.state.worker_ready = ready
        response = client.get("/health/ready")
        assert response.status_code == expected_status
        assert response.json()["components"]["worker"] == expected_component


def test_external_worker_is_not_claimed_as_checked(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    app.state.app_version_synced = True
    app.state.mcp_session_manager = object()
    app.state.worker_mode = "external"
    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json()["components"]["worker"] == "n/a"


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite+pysqlite:///{tmp_path / 'ready.sqlite'}",
        "auto_bootstrap": False,
        "oauth_jwt_private_key_pem": _private_key_pem(),
    }
    values.update(overrides)
    return Settings(**values)


@cache
def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

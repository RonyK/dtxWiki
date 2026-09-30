from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.tests.utils.environment import clear_ambient_dtxwiki_env


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    clear_ambient_dtxwiki_env(monkeypatch)


def _write_tmp_pem(tmp_path: Path) -> Path:
    """Write a minimal RSA PKCS8 PEM for guard (i) in production tests."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "jwt.pem"
    p.write_bytes(pem)
    return p


def test_cors_default_origins_allowed():
    expected_origins = [
        "http://127.0.0.1:21080",
        "http://localhost:21080",
        "http://127.0.0.1:21173",
        "http://localhost:21173",
    ]
    settings = Settings()
    assert settings.cors_origin_list == expected_origins
    client = TestClient(create_app(settings=Settings()))

    for origin in expected_origins:
        response = client.options(
            "/api/v1/pages",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )

        assert response.headers.get("access-control-allow-origin") == origin
        assert response.headers.get("access-control-allow-credentials") == "true"


def test_cors_exposes_protected_content_metadata_to_the_spa():
    origin = "http://127.0.0.1:21173"
    client = TestClient(create_app(settings=Settings()))

    response = client.get("/health/live", headers={"Origin": origin})

    exposed = {
        value.strip().casefold()
        for value in response.headers["access-control-expose-headers"].split(",")
    }
    assert exposed == {
        "accept-ranges",
        "content-disposition",
        "content-range",
        "etag",
    }


def test_cors_default_origins_follow_resolved_ports():
    settings = Settings(backend_port=22080, frontend_dev_port=22173)

    assert settings.cors_origin_list == [
        "http://127.0.0.1:22080",
        "http://localhost:22080",
        "http://127.0.0.1:22173",
        "http://localhost:22173",
    ]


def test_cors_custom_origin_via_environment_json_list(monkeypatch):
    monkeypatch.setenv("DTXWIKI_CORS_ORIGINS", '["http://example.test"]')
    client = TestClient(create_app(settings=Settings()))

    response = client.options(
        "/api/v1/pages",
        headers={
            "Origin": "http://example.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("access-control-allow-origin") == "http://example.test"


def test_cors_custom_origin_via_settings_comma_list():
    client = TestClient(
        create_app(
            settings=Settings(
                cors_origins="http://alpha.test,http://beta.test",
            )
        )
    )

    response = client.options(
        "/api/v1/pages",
        headers={
            "Origin": "http://beta.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("access-control-allow-origin") == "http://beta.test"


def test_cors_unconfigured_origin_rejected():
    client = TestClient(create_app(settings=Settings()))

    response = client.options(
        "/api/v1/pages",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("access-control-allow-origin") is None


def test_cors_invalid_json_reports_config_error():
    with pytest.raises(RuntimeError, match="DTXWIKI_CORS_ORIGINS invalid"):
        create_app(settings=Settings(cors_origins='["http://example.test"'))


def test_cors_wildcard_origin_rejected_with_credentials():
    with pytest.raises(RuntimeError, match=r"\*"):
        create_app(settings=Settings(cors_origins='["*"]'))


def test_validate_for_startup_rejects_empty_cors_string():
    settings = Settings(
        app_env="development",
        cors_origins="",
        backend_port=21080,
        frontend_dev_port=21173,
    )

    with pytest.raises(RuntimeError, match="empty list"):
        settings.validate_for_startup()


def test_production_rejects_missing_session_secret(tmp_path):
    pem_path = _write_tmp_pem(tmp_path)
    with pytest.raises(RuntimeError, match="DTXWIKI_SESSION_SECRET"):
        create_app(
            settings=Settings(
                app_env="production",
                public_base_url="https://wiki.example",
                cors_origins="https://wiki.example",
                oauth_jwt_private_key_path=str(pem_path),
                auto_bootstrap=False,
            )
        )


def test_validate_for_startup_derives_cors_from_base_url_when_unset(tmp_path):
    # Unset CORS in production defaults to the app's own origin (same-origin), so
    # the operator sets the domain once via DTXWIKI_PUBLIC_BASE_URL and does not
    # repeat it. Explicit DTXWIKI_CORS_ORIGINS is only needed for extra origins.
    pem_path = _write_tmp_pem(tmp_path)
    settings = Settings(
        app_env="production",
        public_base_url="https://wiki.example",
        session_secret="production-session-secret",
        cors_origins=None,
        backend_port=21080,
        frontend_dev_port=21173,
        oauth_jwt_private_key_path=str(pem_path),
        auto_bootstrap=False,
    )

    settings.validate_for_startup()  # must not raise
    assert settings.cors_origin_list == ["https://wiki.example"]


def test_validate_for_startup_accepts_explicit_production_cors(tmp_path):
    pem_path = _write_tmp_pem(tmp_path)
    settings = Settings(
        app_env="production",
        public_base_url="https://example.com",
        session_secret="production-session-secret",
        cors_origins="https://example.com,https://www.example.com",
        backend_port=21080,
        frontend_dev_port=21173,
        oauth_jwt_private_key_path=str(pem_path),
        auto_bootstrap=False,
    )

    settings.validate_for_startup()


def test_production_rejects_localhost_cors_origin(tmp_path):
    pem_path = _write_tmp_pem(tmp_path)
    with pytest.raises(RuntimeError, match="localhost"):
        create_app(
            settings=Settings(
                app_env="production",
                public_base_url="https://wiki.example",
                session_secret="production-session-secret",
                cors_origins="http://localhost:21173",
                oauth_jwt_private_key_path=str(pem_path),
                auto_bootstrap=False,
            )
        )

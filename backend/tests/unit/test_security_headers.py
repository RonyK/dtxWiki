import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from backend.api.middleware.security_headers import (
    CSP_SANDBOXED_DOCUMENT_STATE_KEY,
    DEFAULT_HSTS_VALUE,
    HSTS_HEADER,
    SECURITY_HEADERS,
    SecurityHeadersMiddleware,
)
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.tests.utils.environment import clear_ambient_dtxwiki_env


def _clear_dtxwiki_env(monkeypatch):
    clear_ambient_dtxwiki_env(monkeypatch)


def _write_rsa_key(path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _production_settings(tmp_path, monkeypatch) -> Settings:
    _clear_dtxwiki_env(monkeypatch)
    key_path = tmp_path / "jwt.pem"
    _write_rsa_key(key_path)
    return Settings(
        app_env="production",
        public_base_url="https://wiki.example.com",
        session_secret="production-session-secret",
        cors_origins="https://wiki.example.com",
        auto_bootstrap=False,
        oauth_jwt_private_key_path=str(key_path),
    )


def _production_client(app) -> TestClient:
    # Production mode enables HTTPSRedirectMiddleware; TestClient defaults to
    # http://testserver and would receive a 307 before reaching the route.
    # All production-mode assertions must hit the app over https.
    return TestClient(app, base_url="https://testserver")


def _assert_security_headers(response, *, hsts=DEFAULT_HSTS_VALUE):
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    if hsts is None:
        assert HSTS_HEADER not in response.headers
    else:
        assert response.headers[HSTS_HEADER] == hsts


def _csp_directives(policy):
    return {
        name: sources
        for directive in policy.split(";")
        if directive.strip()
        for name, *sources in [directive.split()]
    }


def test_blob_workers_are_allowed_without_widening_script_or_network_sources():
    app = FastAPI()

    @app.get("/graph")
    def graph():
        return PlainTextResponse("graph")

    app.add_middleware(SecurityHeadersMiddleware, enabled=True)
    response = TestClient(app).get("/graph")

    directives = _csp_directives(response.headers["content-security-policy"])
    assert directives["worker-src"] == ["'self'", "blob:"]
    assert directives["script-src"] == ["'self'"]
    assert directives["connect-src"] == ["'self'"]
    assert directives["default-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert all(
        "blob:" not in sources
        for name, sources in directives.items()
        if name != "worker-src"
    )
    assert "'unsafe-eval'" not in response.headers["content-security-policy"]


def test_production_headers_present(tmp_path, monkeypatch):
    app = create_app(settings=_production_settings(tmp_path, monkeypatch))

    response = _production_client(app).get("/health/live")

    assert response.status_code == 200
    _assert_security_headers(response)


def test_dev_headers_absent_by_default(monkeypatch):
    _clear_dtxwiki_env(monkeypatch)
    app = create_app(settings=Settings())

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    for name in SECURITY_HEADERS:
        assert name not in response.headers
    assert HSTS_HEADER not in response.headers


def test_dev_force_env_enables_headers(monkeypatch):
    _clear_dtxwiki_env(monkeypatch)
    monkeypatch.setenv("DTXWIKI_FORCE_SECURITY_HEADERS", "1")
    app = create_app(settings=Settings())

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    # No https public_base_url: this is an HTTP-shaped run, so forcing the
    # headers must not advertise an HSTS pin.
    _assert_security_headers(response, hsts=None)


def _remote_mode_dev_settings(app_mode: str) -> Settings:
    return Settings(
        app_env="development",
        app_mode=app_mode,
        session_secret="remote-mode-test-secret-with-32-plus-chars",
        database_url="sqlite+pysqlite:///:memory:",
    )


@pytest.mark.parametrize("app_mode", ["team", "solo_remote"])
def test_remote_mode_enables_headers_even_in_dev_env(app_mode, monkeypatch):
    # A remote-shaped deployment left at app_env=development must still serve
    # CSP/XFO — the public reader and the SPA face remote clients. HSTS stays
    # off until the operator declares an https public_base_url.
    _clear_dtxwiki_env(monkeypatch)
    app = create_app(settings=_remote_mode_dev_settings(app_mode))

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    _assert_security_headers(response, hsts=None)


def test_remote_mode_with_https_base_url_gets_hsts(monkeypatch):
    _clear_dtxwiki_env(monkeypatch)
    settings = Settings(
        app_env="development",
        app_mode="team",
        session_secret="remote-mode-test-secret-with-32-plus-chars",
        database_url="sqlite+pysqlite:///:memory:",
        public_base_url="https://wiki.example.com",
    )
    app = create_app(settings=settings)

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    _assert_security_headers(response)


def test_remote_mode_dev_env_does_not_force_https_redirect(monkeypatch):
    # The HTTPS redirect stays keyed on app_env=production (the app cannot know
    # the proxy's scheme handling in dev); the startup warning covers the gap.
    _clear_dtxwiki_env(monkeypatch)
    app = create_app(settings=_remote_mode_dev_settings("team"))

    response = TestClient(app, base_url="http://testserver").get(
        "/oauth/authorize", follow_redirects=False
    )

    assert response.status_code == 302
    assert not response.headers["location"].startswith("https://testserver")
    _assert_security_headers(response, hsts=None)


def test_oauth_authorize_has_headers(tmp_path, monkeypatch):
    app = create_app(settings=_production_settings(tmp_path, monkeypatch))

    response = _production_client(app).get("/oauth/authorize", follow_redirects=False)

    assert response.status_code == 302
    _assert_security_headers(response)


def test_mcp_endpoint_has_headers(tmp_path, monkeypatch):
    app = create_app(settings=_production_settings(tmp_path, monkeypatch))

    response = _production_client(app).post("/mcp")

    assert response.status_code == 401
    _assert_security_headers(response)


def test_health_endpoint_has_headers(tmp_path, monkeypatch):
    app = create_app(settings=_production_settings(tmp_path, monkeypatch))

    response = _production_client(app).get("/health/live")

    assert response.status_code == 200
    _assert_security_headers(response)


def test_health_probe_over_http_not_redirected_in_production(tmp_path, monkeypatch):
    # Container liveness probes hit the app over plain HTTP; they must answer 200,
    # not a 307 HTTPS redirect (regression for the remote-smoke healthcheck).
    app = create_app(settings=_production_settings(tmp_path, monkeypatch))
    client = TestClient(app, base_url="http://testserver")

    response = client.get("/health/live", follow_redirects=False)

    assert response.status_code == 200


def test_non_health_over_http_redirects_in_production(tmp_path, monkeypatch):
    app = create_app(settings=_production_settings(tmp_path, monkeypatch))
    client = TestClient(app, base_url="http://testserver")

    response = client.get("/oauth/authorize", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://")


def test_header_values_exact_match(tmp_path, monkeypatch):
    app = create_app(settings=_production_settings(tmp_path, monkeypatch))

    response = _production_client(app).get("/health/live")

    # No includeSubDomains: dtxWiki cannot assume it owns sibling subdomains,
    # so the default pin covers only this host (operators broaden at the proxy).
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; "
        "script-src 'self'; "
        "worker-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )


def test_per_response_csp_override_only_changes_requested_directives():
    app = FastAPI()

    @app.get("/override")
    def override(request: Request):
        request.state.csp_extra_form_action_origins = ["http://127.0.0.1:58713"]
        request.state.csp_script_nonce = "test-nonce"
        return PlainTextResponse("ok")

    app.add_middleware(SecurityHeadersMiddleware, enabled=True)
    response = TestClient(app).get("/override")

    csp = response.headers["content-security-policy"]
    assert "form-action 'self' http://127.0.0.1:58713" in csp
    assert "script-src 'self' 'nonce-test-nonce'" in csp
    directives = _csp_directives(csp)
    assert directives["worker-src"] == ["'self'", "blob:"]
    expected = _csp_directives(SECURITY_HEADERS["content-security-policy"])
    expected["form-action"] = ["'self'", "http://127.0.0.1:58713"]
    expected["script-src"] = ["'self'", "'nonce-test-nonce'"]
    assert directives == expected
    for name, value in SECURITY_HEADERS.items():
        if name != "content-security-policy":
            assert response.headers[name] == value


@pytest.mark.parametrize("with_response_overrides", [False, True])
def test_sandboxed_document_uses_inert_csp_without_weakening_other_headers(
    with_response_overrides,
):
    app = FastAPI()

    @app.get("/preview-content")
    def preview_content(request: Request):
        setattr(request.state, CSP_SANDBOXED_DOCUMENT_STATE_KEY, True)
        if with_response_overrides:
            request.state.csp_extra_form_action_origins = ["http://127.0.0.1:58713"]
            request.state.csp_script_nonce = "test-nonce"
        return PlainTextResponse(
            "preview",
            headers={"Content-Security-Policy": "script-src * 'unsafe-inline'"},
        )

    app.add_middleware(SecurityHeadersMiddleware, enabled=True)
    response = TestClient(app).get("/preview-content")

    assert response.headers["content-security-policy"] == (
        "default-src 'none'; sandbox"
    )
    assert "worker-src" not in response.headers["content-security-policy"]
    assert "blob:" not in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_password_reset_document_uses_no_referrer_policy():
    app = FastAPI()

    @app.get("/reset-password")
    def reset_password_page():
        return PlainTextResponse("reset")

    @app.get("/verify-email")
    def verify_email_page():
        return PlainTextResponse("verify")

    app.add_middleware(SecurityHeadersMiddleware, enabled=True)
    response = TestClient(app).get("/reset-password?token=secret")

    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"

    trailing = TestClient(app).get("/reset-password/?token=secret")
    assert trailing.headers["referrer-policy"] == "no-referrer"
    assert trailing.headers["cache-control"] == "no-store"

    verification = TestClient(app).get("/verify-email/?token=secret")
    assert verification.headers["referrer-policy"] == "no-referrer"
    assert verification.headers["cache-control"] == "no-store"

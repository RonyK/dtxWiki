from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from backend.app.container import assemble
from backend.app.main import create_app
from backend.app.settings import Settings


def test_api_routes_still_work_with_spa_mount(tmp_path):
    app = _app_with_spa_bundle(tmp_path)
    app.state.app_version_synced = True
    app.state.mcp_session_manager = object()
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["status"] == "ready"


def test_bundled_spa_served_when_static_assets_exist(tmp_path):
    client = TestClient(_app_with_spa_bundle(tmp_path))

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "<div id=\"root\">" in response.text


def test_bundled_assets_are_immutable(tmp_path):
    client = TestClient(_app_with_spa_bundle(tmp_path))

    response = client.get("/assets/app.js")

    assert response.status_code == 200
    assert (
        response.headers["cache-control"]
        == "public, max-age=31536000, immutable"
    )


def test_spa_history_route_falls_back_to_index(tmp_path):
    client = TestClient(_app_with_spa_bundle(tmp_path))

    response = client.get("/pages/new")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "<div id=\"root\">" in response.text


def test_public_page_spa_shell_does_not_blanket_noindex(tmp_path):
    client = TestClient(_app_with_spa_bundle(tmp_path))

    response = client.get("/w/acme/pages/welcome")

    assert response.status_code == 200
    # A shared shell cannot know indexed_web vs unlisted_web before the public
    # API resolves the grant. It stays non-cacheable, while the rendered client
    # adds robots metadata only for an unlisted payload or unavailable state.
    assert response.headers["cache-control"] == "no-store"
    assert "X-Robots-Tag" not in response.headers


def test_spa_cache_policy_does_not_override_api_or_oauth(tmp_path):
    app = _app_with_spa_bundle(tmp_path)
    app.state.app_version_synced = True
    app.state.mcp_session_manager = object()
    client = TestClient(app)

    api_response = client.get("/health/ready")
    oauth_response = client.get("/oauth/authorize", follow_redirects=False)

    # The point is that the SPA policy keeps its hands off these routes. It
    # must not stamp the long immutable asset lifetime on them, and it must
    # not invent a no-store either -- whatever each endpoint decides for
    # itself is what should arrive.
    assert api_response.status_code == 200
    assert "cache-control" not in api_response.headers
    assert oauth_response.status_code == 302
    assert "immutable" not in oauth_response.headers.get("cache-control", "")


def test_unknown_api_route_returns_json_404(tmp_path):
    client = TestClient(_app_with_spa_bundle(tmp_path))

    response = client.get("/api/v1/no-such-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error_code"] == "not_found"


def test_api_root_does_not_fall_back_to_spa(tmp_path):
    client = TestClient(_app_with_spa_bundle(tmp_path))

    response = client.get("/api")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error_code"] == "not_found"


def test_non_get_unknown_api_route_returns_json_404(tmp_path):
    client = TestClient(_app_with_spa_bundle(tmp_path))

    response = client.post("/api/v1/no-such-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error_code"] == "not_found"


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _settings(tmp_path) -> Settings:
    # /health/ready pings the configured database. The default URL is
    # <DTXWIKI_DATA_DIR>/data/dtxwiki.sqlite, and create_app does not create
    # that directory (bootstrap does), so on the default the probe answers 503
    # unless an earlier test in the same pytest process happened to create it.
    # Each app gets a database of its own instead.
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'ready.sqlite'}",
        oauth_jwt_private_key_pem=_private_key_pem(),
    )


def test_unknown_frontend_route_still_returns_spa_index(tmp_path):
    client = TestClient(_app_with_spa_bundle(tmp_path))

    response = client.get("/pages/some/client/route")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<div id=\"root\">" in response.text


def test_create_app_accepts_container_and_extra_mount(tmp_path):
    settings = _settings(tmp_path)
    container = assemble(settings)

    def mount_cloud_probe(app):
        @app.get("/cloud/ping")
        def cloud_ping():
            return {"ok": True}

    app = create_app(
        settings=settings,
        container=container,
        extra_mount=mount_cloud_probe,
        static_root=tmp_path,
    )
    client = TestClient(app)

    response = client.get("/cloud/ping")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def _app_with_spa_bundle(tmp_path):
    static_root = tmp_path / "static"
    assets = static_root / "assets"
    assets.mkdir(parents=True)
    (static_root / "index.html").write_text(
        '<html><body><div id="root"></div><script type="module" '
        'src="/assets/app.js"></script></body></html>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('test bundle');", encoding="utf-8")

    return create_app(settings=_settings(tmp_path), static_root=static_root)

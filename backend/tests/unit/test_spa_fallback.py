"""SPA fallback reserved-prefix exemption tests (Task 4.11 — ADR A10)."""

import pytest
from fastapi.testclient import TestClient

from backend.app.main import RESERVED_NON_SPA_PREFIXES, create_app


@pytest.fixture
def client(tmp_path):
    static_root = tmp_path / "static"
    assets = static_root / "assets"
    assets.mkdir(parents=True)
    (static_root / "index.html").write_text(
        '<html><body><div id="root"></div><script type="module" '
        'src="/assets/app.js"></script></body></html>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('test bundle');", encoding="utf-8")

    app = create_app(static_root=static_root)
    return TestClient(app, raise_server_exceptions=False)


def _assert_not_index_html(resp):
    content_type = resp.headers.get("content-type", "")
    assert not content_type.startswith("text/html")
    assert "<html" not in resp.text.lower()


def test_reserved_prefixes_set():
    """Verify the reserved prefixes are defined."""
    assert "api" in RESERVED_NON_SPA_PREFIXES
    assert "health" in RESERVED_NON_SPA_PREFIXES
    assert "metrics" in RESERVED_NON_SPA_PREFIXES
    assert ".well-known" in RESERVED_NON_SPA_PREFIXES
    assert "oauth" in RESERVED_NON_SPA_PREFIXES
    assert "mcp" in RESERVED_NON_SPA_PREFIXES
    assert "llms.txt" in RESERVED_NON_SPA_PREFIXES
    assert "THIRD_PARTY_NOTICES.txt" in RESERVED_NON_SPA_PREFIXES


def test_health_liveness_uses_real_route_not_index_html(client):
    resp = client.get("/health/live")
    assert resp.status_code == 200
    _assert_not_index_html(resp)
    assert resp.json()["status"] == "alive"


def test_metrics_does_not_return_index_html(client):
    resp = client.get("/metrics")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "metrics_disabled"
    _assert_not_index_html(resp)


def test_mcp_does_not_return_index_html(client):
    resp = client.get("/mcp")
    # The reserved /mcp prefix must never fall through to the SPA index.
    # json_stateless rejects GET with 405; the streamable default treats GET
    # as an auth-gated SSE stream and returns 401. Either way it is the MCP
    # router answering, not the SPA fallback.
    assert resp.status_code in (401, 405)
    _assert_not_index_html(resp)


def test_oauth_does_not_return_index_html(client):
    resp = client.get("/oauth/authorize", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?")
    _assert_not_index_html(resp)


def test_well_known_does_not_return_index_html(client):
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    _assert_not_index_html(resp)


def test_unmatched_route_returns_html(client):
    """Positive control (ADR A6): unknown SPA path still returns index."""
    resp = client.get("/admin/pages/foo")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_third_party_notices_absent_does_not_return_index_html(client):
    # The fixture's bundle ships no notices file: the reserved path must 404
    # rather than fall through to the SPA index.
    resp = client.get("/THIRD_PARTY_NOTICES.txt")
    assert resp.status_code == 404
    assert "not_found" in resp.text
    _assert_not_index_html(resp)


def test_third_party_notices_are_served_as_plain_text(tmp_path):
    static_root = tmp_path / "static"
    (static_root / "assets").mkdir(parents=True)
    (static_root / "index.html").write_text("<html></html>", encoding="utf-8")
    (static_root / "THIRD_PARTY_NOTICES.txt").write_text(
        "Third-party software bundled with the dtxWiki web UI\n\n- react@19.2.6: MIT\n",
        encoding="utf-8",
    )
    client = TestClient(
        create_app(static_root=static_root), raise_server_exceptions=False
    )

    resp = client.get("/THIRD_PARTY_NOTICES.txt")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["cache-control"] == "no-store"
    assert "- react@19.2.6: MIT" in resp.text

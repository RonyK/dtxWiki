from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api import metrics
from backend.app.main import create_app
from backend.app.settings import Settings


@pytest.fixture(autouse=True)
def reset_metrics():
    metrics.reset_metrics_for_test()
    yield
    metrics.reset_metrics_for_test()


def test_disabled_returns_403_not_html():
    client = TestClient(create_app(settings=Settings()))

    response = client.get("/metrics")

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error_code"] == "metrics_disabled"
    assert "text/html" not in response.headers["content-type"]


def test_enabled_value_must_be_1(monkeypatch):
    monkeypatch.setenv("DTXWIKI_METRICS_ENABLED", "1")
    assert Settings().metrics_enabled is True

    monkeypatch.setenv("DTXWIKI_METRICS_ENABLED", "true")
    with pytest.raises(ValidationError):
        Settings()


def test_on_without_auth_fails_startup():
    settings = Settings(
        app_env="production",
        public_base_url="https://dtxwiki.test",
        session_secret="production-session-secret",
        cors_origins="https://client.test",
        forwarded_allow_ips="127.0.0.1",
        metrics_enabled=True,
        auto_bootstrap=False,
    )

    with pytest.raises(RuntimeError, match="DTXWIKI_METRICS_ENABLED=1 requires"):
        settings.validate_for_startup()


def test_on_with_bearer_serves_text_plain():
    client = TestClient(
        create_app(
            settings=Settings(
                metrics_enabled=True,
                metrics_bearer_token="metrics-secret",
            )
        )
    )

    response = client.get(
        "/metrics",
        headers={"Authorization": "Bearer metrics-secret"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# HELP http_requests_total" in response.text
    assert "# TYPE http_requests_total counter" in response.text


def test_allowlist_uses_forwarded_ip_only_from_trusted_peer():
    trusted_client = TestClient(
        create_app(
            settings=Settings(
                metrics_enabled=True,
                metrics_allowlist="203.0.113.42",
                behind_proxy=True,
                # Trust the immediate peer AND the intermediate proxy so the
                # originating client (203.0.113.42) is resolved through the chain.
                forwarded_allow_ips="testclient,10.0.0.1",
            )
        )
    )
    untrusted_client = TestClient(
        create_app(
            settings=Settings(
                metrics_enabled=True,
                metrics_allowlist="203.0.113.42",
                behind_proxy=True,
            )
        )
    )

    trusted = trusted_client.get(
        "/metrics",
        headers={"X-Forwarded-For": "203.0.113.42, 10.0.0.1"},
    )
    untrusted = untrusted_client.get(
        "/metrics",
        headers={"X-Forwarded-For": "203.0.113.42, 10.0.0.1"},
    )

    assert trusted.status_code == 200
    assert untrusted.status_code == 403


def test_three_counters_present():
    metrics.inc_oauth_token_grant(grant_type="authorization_code", result="success")
    metrics.inc_mcp_tool_call(tool="read", status="ok")
    client = TestClient(
        create_app(
            settings=Settings(
                metrics_enabled=True,
                metrics_bearer_token="metrics-secret",
            )
        )
    )
    client.get("/health/live")

    response = client.get(
        "/metrics",
        headers={"Authorization": "Bearer metrics-secret"},
    )

    assert response.status_code == 200
    assert "http_requests_total" in response.text
    assert "oauth_token_grants_total" in response.text
    assert "mcp_tool_calls_total" in response.text


def test_tls_cert_expiry_gauge_reads_public_endpoint_certificate(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    expires_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wiki.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(expires_at - timedelta(days=30))
        .not_valid_after(expires_at)
        .sign(key, hashes.SHA256())
    )
    monkeypatch.setattr(
        metrics,
        "_read_tls_peer_certificate",
        lambda host, port: certificate.public_bytes(serialization.Encoding.DER),
    )
    client = TestClient(
        create_app(
            settings=Settings(
                public_base_url="https://wiki.test",
                metrics_enabled=True,
                metrics_bearer_token="metrics-secret",
            )
        )
    )

    response = client.get(
        "/metrics",
        headers={"Authorization": "Bearer metrics-secret"},
    )

    assert response.status_code == 200
    assert "# TYPE tls_cert_expiry gauge" in response.text
    assert f"tls_cert_expiry {expires_at.timestamp():.0f}" in response.text


def test_tls_cert_expiry_omits_sample_for_http_deployment(monkeypatch):
    monkeypatch.setattr(
        metrics,
        "_read_tls_peer_certificate",
        lambda *_args: pytest.fail("HTTP deployment must not read a TLS certificate"),
    )
    client = TestClient(
        create_app(
            settings=Settings(
                public_base_url="http://wiki.test",
                metrics_enabled=True,
                metrics_bearer_token="metrics-secret",
            )
        )
    )

    response = client.get(
        "/metrics",
        headers={"Authorization": "Bearer metrics-secret"},
    )

    assert response.status_code == 200
    assert "# TYPE tls_cert_expiry gauge" in response.text
    assert not any(
        line.startswith("tls_cert_expiry ")
        for line in response.text.splitlines()
    )


def test_mcp_tool_label_clamps_unknown_names():
    metrics.inc_mcp_tool_call(tool="read", status="ok")
    metrics.inc_mcp_tool_call(tool="made-up-tool-1", status="ok")
    metrics.inc_mcp_tool_call(tool="made-up-tool-2", status="error")

    snapshot = metrics._counter_snapshot()["mcp_tool_calls_total"]
    tool_labels = {dict(key)["tool"] for key in snapshot}
    assert tool_labels == {"read", "unknown"}
    assert snapshot[(("tool", "unknown"), ("status", "ok"))] == 1
    assert snapshot[(("tool", "unknown"), ("status", "error"))] == 1


def test_no_user_label_cardinality():
    assert set(metrics._LABELS["http_requests_total"]) == {"method", "status"}
    assert set(metrics._LABELS["oauth_token_grants_total"]) == {
        "grant_type",
        "result",
        "refusal_reason",
    }
    assert set(metrics._LABELS["mcp_tool_calls_total"]) == {"tool", "status"}

    forbidden = {"user_id", "workspace_id", "page_id", "library_item_id", "client_id"}
    for metric_name, label_tuple in metrics._LABELS.items():
        for label in label_tuple:
            assert label not in forbidden, (
                f"{metric_name} has forbidden high-cardinality label {label!r}"
            )


def test_refresh_refusal_metric_reason_is_closed_set():
    metrics.inc_oauth_token_grant(
        grant_type="refresh_token",
        result="invalid_grant",
        refusal_reason="consent_missing",
    )
    metrics.inc_oauth_token_grant(
        grant_type="refresh_token",
        result="invalid_grant",
        refusal_reason="attacker-controlled",
    )
    # Retired with the refresh-path membership check it used to label; a
    # stale caller must not resurrect the label.
    metrics.inc_oauth_token_grant(
        grant_type="refresh_token",
        result="invalid_grant",
        refusal_reason="membership_missing",
    )

    snapshot = metrics._counter_snapshot()["oauth_token_grants_total"]
    assert snapshot[
        (
            ("grant_type", "refresh_token"),
            ("result", "invalid_grant"),
            ("refusal_reason", "consent_missing"),
        )
    ] == 1
    assert snapshot[
        (
            ("grant_type", "refresh_token"),
            ("result", "invalid_grant"),
            ("refusal_reason", ""),
        )
    ] == 2


def test_does_not_return_index_html():
    client = TestClient(
        create_app(
            settings=Settings(
                metrics_enabled=True,
                metrics_bearer_token="metrics-secret",
            )
        )
    )

    response = client.get("/metrics")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert "text/html" not in response.headers["content-type"]
    assert response.json()["error_code"] == "unauthorized"

"""Unit tests for deployment metadata + prod_setup pure helpers."""

from __future__ import annotations

import os
import socket
import stat
import sys
from pathlib import Path

import pytest

from backend.app.settings import Settings
from backend.cli import prod_setup
from backend.services import deployment


def test_field_env_roundtrip():
    assert deployment.field_to_env("public_base_url") == "DTXWIKI_PUBLIC_BASE_URL"
    assert deployment.env_to_field("DTXWIKI_PUBLIC_BASE_URL") == "public_base_url"
    assert deployment.env_to_field("DTXWIKI_SERVICE_SHARD_ROUTER_URL") is None
    assert deployment.is_service_env_key("DTXWIKI_SERVICE_SHARD_ROUTER_URL") is True
    assert deployment.env_to_field("NOT_DTXWIKI") is None


def test_valid_env_keys_cover_settings():
    keys = deployment.valid_env_keys()
    assert "DTXWIKI_SESSION_SECRET" in keys
    # Every key maps back onto a real Settings field.
    for key in keys:
        assert deployment.env_to_field(key) in Settings.model_fields


def test_spec_blast_radius_defaults_to_restart():
    assert deployment.blast_radius_for("DTXWIKI_APP_NAME") == "restart"
    assert (
        deployment.blast_radius_for("DTXWIKI_SESSION_SECRET")
        == "session_invalidation"
    )
    assert deployment.spec_for("DTXWIKI_SESSION_SECRET").dangerous is True
    # Unknown-but-valid key has no tailored spec.
    assert deployment.spec_for("DTXWIKI_WORKSPACE_SLUG") is None


def test_domain_of_and_mcp_url():
    settings = Settings(public_base_url="https://wiki.example.com")
    assert deployment.domain_of(settings.public_base_url) == "wiki.example.com"
    assert deployment.mcp_url(settings) == "https://wiki.example.com/mcp"
    assert deployment.https_configured(settings) is True
    assert deployment.https_configured(Settings(public_base_url=None)) is False
    assert deployment.https_configured(Settings(public_base_url="http://x")) is False


def test_build_suggested_commands_contextualized():
    settings = Settings(public_base_url="https://current.example.com")
    suggestions = deployment.build_suggested_commands(settings)
    assert any("current.example.com" in s.command for s in suggestions)
    assert any(s.blast_radius == "session_invalidation" for s in suggestions)


def test_signup_configuration_uses_effective_resolver_and_scoped_commands():
    defaulted = deployment.signup_configuration(Settings(app_mode="team"))
    explicit = deployment.signup_configuration(
        Settings(app_mode="team", signup_enabled=True)
    )

    assert defaulted.configured is None
    assert defaulted.effective is False
    assert defaulted.source == "mode_default"
    assert defaulted.mode_defaults == {
        "solo_local": True,
        "solo_remote": False,
        "team": False,
    }
    assert "DTXWIKI_SIGNUP_ENABLED=true" in defaulted.systemd_command
    assert "DTXWIKI_SIGNUP_ENABLED=true" in defaulted.compose_command
    assert explicit.configured is True
    assert explicit.effective is True
    assert explicit.source == "explicit_environment"
    assert "DTXWIKI_SIGNUP_ENABLED=false" in explicit.systemd_command
    assert explicit.restart_required is True
    assert explicit.impact == "new_signups_only"


def test_signup_config_key_has_restart_only_impact_guidance():
    spec = deployment.spec_for("DTXWIKI_SIGNUP_ENABLED")

    assert spec is not None
    assert spec.blast_radius == "restart"
    assert "Existing sessions" in spec.warning
    assert "workspace memberships" in spec.warning


def test_recovery_configuration_reports_capabilities_without_secret_values():
    settings = Settings(
        public_base_url="https://wiki.example.com",
        mail_provider="smtp",
        mail_from_address="wiki@example.com",
        smtp_host="smtp.example.com",
        smtp_username="mailer",
        smtp_password="smtp-secret",
    )

    recovery = deployment.recovery_configuration(
        settings,
        outbox_queued=2,
        outbox_failed=1,
        latest_delivery_status="failed",
    )

    assert recovery.mode == "email"
    assert recovery.admin_assisted is True
    assert recovery.link_delivery_available is True
    assert recovery.mail_status == "configured"
    assert recovery.mail_connection_status == "failed"
    assert recovery.smtp_credentials_configured is True
    assert recovery.outbox_queued == 2
    assert recovery.outbox_failed == 1
    assert recovery.worker_required is True
    assert "smtp-secret" not in repr(recovery)
    assert "smtp-secret" not in recovery.systemd_command
    assert "smtp-secret" not in recovery.compose_command

    succeeded = deployment.recovery_configuration(
        settings,
        latest_delivery_status="sent",
    )
    assert succeeded.mail_connection_status == "succeeded"


def test_admin_assisted_recovery_does_not_claim_mail_worker_is_required():
    recovery = deployment.recovery_configuration(
        Settings(public_base_url="https://wiki.example.com")
    )

    assert recovery.mode == "admin_assisted"
    assert recovery.mail_provider == "none"
    assert recovery.worker_required is False


def test_set_env_values_preserves_comments_and_updates_in_place(tmp_path: Path):
    env = tmp_path / "dtxwiki.env"
    env.write_text(
        "# header comment\n"
        "DTXWIKI_APP_NAME=dtxWiki\n"
        "\n"
        "DTXWIKI_PUBLIC_BASE_URL=https://old.example.com\n",
        encoding="utf-8",
    )
    prod_setup.set_env_values(
        env, {"DTXWIKI_PUBLIC_BASE_URL": "https://new.example.com"}
    )
    text = env.read_text(encoding="utf-8")
    assert "# header comment" in text
    assert "DTXWIKI_PUBLIC_BASE_URL=https://new.example.com" in text
    assert "old.example.com" not in text
    # A backup of the prior content was created.
    assert (tmp_path / "dtxwiki.env.bak").exists()


def test_set_env_values_appends_new_key(tmp_path: Path):
    env = tmp_path / "dtxwiki.env"
    env.write_text("DTXWIKI_APP_NAME=dtxWiki\n", encoding="utf-8")
    prod_setup.set_env_values(env, {"DTXWIKI_MCP_HTTP_MODE": "auto"})
    parsed = prod_setup.read_env_file(env)
    assert parsed["DTXWIKI_APP_NAME"] == "dtxWiki"
    assert parsed["DTXWIKI_MCP_HTTP_MODE"] == "auto"


def test_validate_env_key():
    prod_setup.validate_env_key("DTXWIKI_PUBLIC_BASE_URL")
    with pytest.raises(ValueError):
        prod_setup.validate_env_key("DTXWIKI_BOGUS")
    with pytest.raises(ValueError):
        prod_setup.validate_env_key("PUBLIC_BASE_URL")


def test_parse_assignment_handles_equals_in_value():
    key, value = prod_setup.parse_assignment(
        "DTXWIKI_DATABASE_URL=sqlite+pysqlite:///x?ssl=true"
    )
    assert key == "DTXWIKI_DATABASE_URL"
    assert value == "sqlite+pysqlite:///x?ssl=true"


def test_render_caddyfile_substitutes_domain():
    rendered = prod_setup.render_caddyfile(
        "wiki.example.com {\n  reverse_proxy 127.0.0.1:21080\n}\n",
        "docs.acme.io",
    )
    assert "docs.acme.io {" in rendered
    assert "wiki.example.com" not in rendered


def test_render_production_env_passes_guard_shape():
    content = prod_setup.render_production_env(
        domain="wiki.example.com",
        session_secret="s" * 80,
        jwt_key_path="/etc/dtxwiki/jwt.pem",
    )
    parsed = prod_setup.read_env_file_from_text(content)
    assert parsed["DTXWIKI_APP_ENV"] == "production"
    assert parsed["DTXWIKI_PUBLIC_BASE_URL"] == "https://wiki.example.com"
    assert parsed["DTXWIKI_CORS_ORIGINS"] == "https://wiki.example.com"
    assert parsed["DTXWIKI_AUTO_BOOTSTRAP"] == "false"
    assert parsed["DTXWIKI_SIGNUP_ENABLED"] == "false"
    assert parsed["DTXWIKI_MAIL_PROVIDER"] == "none"
    assert parsed["DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH"] == "/etc/dtxwiki/jwt.pem"
    # Writable state must point inside the systemd ReadWritePaths, not ~/.dtxwiki
    # (read-only under ProtectSystem=strict) — otherwise image uploads 500.
    assert parsed["DTXWIKI_DATA_DIR"] == "/var/lib/dtxwiki"


def test_render_caddyfile_substitutes_cert_and_key():
    rendered = prod_setup.render_caddyfile(
        "wiki.example.com {\n"
        "  tls /path/to/fullchain.pem /path/to/privkey.pem\n"
        "}\n",
        "docs.acme.io",
        cert="/etc/ssl/acme.crt",
        key="/etc/ssl/acme.key",
    )
    assert "docs.acme.io {" in rendered
    assert "tls /etc/ssl/acme.crt /etc/ssl/acme.key" in rendered
    assert "/path/to/" not in rendered


def test_render_production_env_internal_mode_keeps_https():
    content = prod_setup.render_production_env(
        domain="wiki.lan",
        session_secret="s" * 80,
        jwt_key_path="/etc/dtxwiki/jwt.pem",
        tls_mode="internal",
    )
    parsed = prod_setup.read_env_file_from_text(content)
    assert parsed["DTXWIKI_PUBLIC_BASE_URL"] == "https://wiki.lan"
    assert parsed["DTXWIKI_TLS_MODE"] == "internal"
    # The insecure opt-in is only emitted for 'none'.
    assert "DTXWIKI_ALLOW_INSECURE_HTTP" not in parsed


def test_render_production_env_none_mode_is_plain_http_and_opts_in():
    content = prod_setup.render_production_env(
        domain="192.168.1.50",
        session_secret="s" * 80,
        jwt_key_path="/etc/dtxwiki/jwt.pem",
        tls_mode="none",
    )
    parsed = prod_setup.read_env_file_from_text(content)
    assert parsed["DTXWIKI_PUBLIC_BASE_URL"] == "http://192.168.1.50"
    assert parsed["DTXWIKI_CORS_ORIGINS"] == "http://192.168.1.50"
    assert parsed["DTXWIKI_TLS_MODE"] == "none"
    assert parsed["DTXWIKI_ALLOW_INSECURE_HTTP"] == "true"


def test_render_production_env_external_mode_trusts_proxy_ip():
    content = prod_setup.render_production_env(
        domain="wiki.corp.com",
        session_secret="s" * 80,
        jwt_key_path="/etc/dtxwiki/jwt.pem",
        tls_mode="external",
        forwarded_allow_ips="10.0.0.5",
    )
    parsed = prod_setup.read_env_file_from_text(content)
    assert parsed["DTXWIKI_PUBLIC_BASE_URL"] == "https://wiki.corp.com"
    assert parsed["DTXWIKI_TLS_MODE"] == "external"
    assert parsed["DTXWIKI_FORWARDED_ALLOW_IPS"] == "10.0.0.5"


def test_preflight_internal_skips_public_dns(monkeypatch):
    # preflight_internal must not attempt public DNS resolution (internal names
    # have none) — make resolve_host explode to prove it is never called.
    def boom(domain: str) -> list[str]:  # pragma: no cover
        raise AssertionError("preflight_internal must not resolve public DNS")

    monkeypatch.setattr(prod_setup, "resolve_host", boom)
    checks = prod_setup.preflight_internal(
        "wiki.lan", port_probe=lambda port: ("ok", "free")
    )
    assert {c.name for c in checks} == {"dns", "port_80", "port_443"}
    assert checks[0].status == "ok"  # informational, not a resolution attempt
    assert prod_setup.worst_status(checks) == "ok"


def test_oauth_availability_requires_https_only_dcr_is_advisory():
    """MCP-10: DCR off must not report OAuth as unavailable -- a client
    using an https URL client identifier (CIMD) authorizes fine without DCR
    (_resolve_authorization_client never reads oauth_allow_dcr). DCR is
    surfaced separately via oauth_dcr_enabled so the UI can show an
    accurate "self-registering clients only" caveat instead of steering
    operators to a weaker Access Key when OAuth actually still works."""

    https = Settings(public_base_url="https://wiki.example.com")
    assert deployment.oauth_available(https) is True
    assert deployment.oauth_unavailable_reason(https) is None
    assert deployment.oauth_dcr_enabled(https) is True

    plain = Settings(public_base_url="http://wiki.lan")
    assert deployment.oauth_available(plain) is False
    assert "https" in (deployment.oauth_unavailable_reason(plain) or "")

    no_dcr = Settings(
        public_base_url="https://wiki.example.com", oauth_allow_dcr=False
    )
    assert deployment.oauth_available(no_dcr) is True
    assert deployment.oauth_unavailable_reason(no_dcr) is None
    assert deployment.oauth_dcr_enabled(no_dcr) is False


def test_preflight_dns_failure_is_fatal():
    def failing_resolver(domain: str) -> list[str]:
        raise socket.gaierror("no such host")

    checks = prod_setup.preflight(
        "missing.example.com",
        resolver=failing_resolver,
        egress=lambda: None,
        port_probe=lambda port: ("ok", "free"),
    )
    assert checks[0].name == "dns"
    assert checks[0].status == "fail"
    assert prod_setup.worst_status(checks) == "fail"


def test_preflight_ok_with_matching_ip():
    checks = prod_setup.preflight(
        "wiki.example.com",
        resolver=lambda d: ["203.0.113.10"],
        egress=lambda: "203.0.113.10",
        port_probe=lambda port: ("ok", "free"),
    )
    assert prod_setup.worst_status(checks) == "ok"
    assert all(c.status == "ok" for c in checks)


def test_preflight_nat_mismatch_warns_not_fails():
    checks = prod_setup.preflight(
        "wiki.example.com",
        resolver=lambda d: ["203.0.113.10"],
        egress=lambda: "192.168.0.50",  # behind NAT
        port_probe=lambda port: ("ok", "free"),
    )
    assert prod_setup.worst_status(checks) == "warn"
    assert any(c.name == "dns_match" and c.status == "warn" for c in checks)


def test_set_key_ownership_makes_key_group_readable_as_root(tmp_path: Path):
    key = tmp_path / "jwt.pem"
    key.write_bytes(b"pem")
    calls: dict[str, object] = {}

    def fake_chown(path, user=None, group=None):
        calls["chown"] = (str(path), user, group)

    def fake_chmod(path, mode):
        calls["chmod"] = (str(path), mode)

    applied = prod_setup.set_key_ownership(
        key, "dtxwiki", geteuid=lambda: 0, chown=fake_chown, chmod=fake_chmod
    )
    assert applied is True
    # root:dtxwiki 0640 so the systemd service group can read the signing key.
    assert calls["chown"] == (str(key), "root", "dtxwiki")
    assert calls["chmod"] == (str(key), 0o640)


def test_set_key_ownership_noop_when_not_root(tmp_path: Path):
    key = tmp_path / "jwt.pem"
    key.write_bytes(b"pem")

    def forbidden(*_a, **_k):
        raise AssertionError("must not chown/chmod when not root")

    applied = prod_setup.set_key_ownership(
        key, "dtxwiki", geteuid=lambda: 1000, chown=forbidden, chmod=forbidden
    )
    assert applied is False


def test_set_env_values_collapses_duplicate_keys(tmp_path: Path):
    env = tmp_path / "dtxwiki.env"
    env.write_text(
        "DTXWIKI_APP_NAME=dtxWiki\n"
        "DTXWIKI_PUBLIC_BASE_URL=https://old.example.com\n"
        "DTXWIKI_PUBLIC_BASE_URL=https://dup.example.com\n",
        encoding="utf-8",
    )
    prod_setup.set_env_values(
        env, {"DTXWIKI_PUBLIC_BASE_URL": "https://new.example.com"}
    )
    text = env.read_text(encoding="utf-8")
    # Exactly one line for the key holding the new value — no stale duplicate
    # further down that readers / systemd (which take the LAST line) would win.
    assert text.count("DTXWIKI_PUBLIC_BASE_URL=") == 1
    parsed = prod_setup.read_env_file(env)
    assert parsed["DTXWIKI_PUBLIC_BASE_URL"] == "https://new.example.com"


def test_read_env_file_tolerates_export_prefix():
    parsed = prod_setup.read_env_file_from_text(
        "export DTXWIKI_APP_NAME=dtxWiki\nDTXWIKI_APP_MODE=team\n"
    )
    assert parsed["DTXWIKI_APP_NAME"] == "dtxWiki"
    assert parsed["DTXWIKI_APP_MODE"] == "team"


def test_read_env_file_strips_surrounding_quotes_like_systemd():
    # systemd's EnvironmentFile strips one pair of quotes, so a hand-quoted
    # DATABASE_URL works for the service; the CLI must see the same value or
    # make_url() chokes and the pre-update DB backup is silently skipped.
    parsed = prod_setup.read_env_file_from_text(
        'DTXWIKI_DATABASE_URL="sqlite+pysqlite:////var/lib/dtxwiki/dtxwiki.sqlite"\n'
        "DTXWIKI_APP_NAME='My Wiki'\n"
        "DTXWIKI_DATA_DIR=/var/lib/dtxwiki\n"
    )
    assert parsed["DTXWIKI_DATABASE_URL"] == (
        "sqlite+pysqlite:////var/lib/dtxwiki/dtxwiki.sqlite"
    )
    assert parsed["DTXWIKI_APP_NAME"] == "My Wiki"
    assert parsed["DTXWIKI_DATA_DIR"] == "/var/lib/dtxwiki"


def test_read_env_file_keeps_unbalanced_or_inner_quotes():
    parsed = prod_setup.read_env_file_from_text(
        "DTXWIKI_APP_NAME=\"unterminated\n"
        "DTXWIKI_SESSION_SECRET='a\"b'\n"
        'DTXWIKI_CORS_ORIGINS=ht"tps\n'
        "DTXWIKI_MCP_HTTP_MODE='\n"
    )
    # Only one MATCHING pair of surrounding quotes is stripped, exactly once.
    assert parsed["DTXWIKI_APP_NAME"] == '"unterminated'
    assert parsed["DTXWIKI_SESSION_SECRET"] == 'a"b'
    assert parsed["DTXWIKI_CORS_ORIGINS"] == 'ht"tps'
    assert parsed["DTXWIKI_MCP_HTTP_MODE"] == "'"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_generate_jwt_key_is_created_0600(tmp_path: Path):
    key = tmp_path / "jwt.pem"
    assert prod_setup.generate_jwt_key(key) is True
    # The key must be 0600 from creation (no world-readable umask window).
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600

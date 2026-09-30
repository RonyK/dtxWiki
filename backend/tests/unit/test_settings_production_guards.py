"""Production startup guard tests (Task 4.3 — 9 guards + 1 positive control)."""

import logging
import warnings
from pathlib import Path

import pytest

from backend.app.settings import Settings, _warn_removed_env
from backend.tests.conftest import captured_log_messages
from backend.tests.utils.environment import clear_ambient_dtxwiki_env

PROD_BASE = {
    "app_env": "production",
    "public_base_url": "https://wiki.example.com",
    "session_secret": "a-real-secret-that-is-not-default",
    "cors_origins": "https://wiki.example.com",
    "forwarded_allow_ips": "127.0.0.1",
    "database_url": "sqlite+pysqlite:////tmp/test.sqlite",
    "auto_bootstrap": False,
    "metrics_enabled": False,
}


def _make_settings(tmp_pem: Path, **overrides) -> Settings:
    """Return a Settings instance that passes all guards unless overridden."""
    kwargs = dict(PROD_BASE)
    kwargs["oauth_jwt_private_key_path"] = str(tmp_pem)
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    clear_ambient_dtxwiki_env(monkeypatch)


@pytest.fixture
def tmp_pem(tmp_path):
    """Write a minimal RSA PEM stub for guard (i) happy path."""
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


def test_happy_path_passes(tmp_pem):
    """Positive control (ADR A6): all 9 guards satisfied -> no exception."""
    s = _make_settings(tmp_pem)
    s.validate_for_startup()  # must not raise


@pytest.mark.parametrize(
    ("env_key", "value", "guidance"),
    [
        (
            "DTXWIKI_UPLOAD_WORKSPACE_QUOTA_BYTES",
            "1048576",
            "configure each workspace quota",
        ),
        (
            "DTXWIKI_WORKSPACE_SLUG",
            "default",
            "authenticated user or request",
        ),
        (
            "DTXWIKI_MCP_CLIENT_ID",
            "legacy-client",
            "authenticated /mcp endpoint",
        ),
        (
            "DTXWIKI_MCP_LOG_LEVEL",
            "DEBUG",
            "server application's logging configuration",
        ),
        (
            "DTXWIKI_MCP_TOKEN",
            "dtxw_legacy",
            "remote MCP client",
        ),
        (
            "DTXWIKI_WORKSPACE_TOKENS",
            "workspace-id=dtxw_legacy",
            "one PAT or OAuth grant",
        ),
        (
            "DTXWIKI_DESKTOP_MCP_COMMAND",
            "/old/dtxwiki-desktop",
            "authenticated HTTP MCP endpoints",
        ),
    ],
)
def test_removed_env_warns_once_and_does_not_fail_production_startup(
    tmp_pem, monkeypatch, env_key, value, guidance
):
    monkeypatch.setenv(env_key, value)
    _warn_removed_env.cache_clear()

    # caplog reads through the root logger, which a full-suite run leaves
    # unusable -- see captured_log_messages' docstring.
    with captured_log_messages("dtxwiki.startup", logging.WARNING) as messages:
        _make_settings(tmp_pem).validate_for_startup()
        _make_settings(tmp_pem).validate_for_startup()

    assert len(messages) == 1
    assert messages[0].startswith(f"{env_key} is no longer supported and is ignored;")
    assert guidance in messages[0]


def test_removed_env_does_not_warn_when_absent(tmp_pem):
    _warn_removed_env.cache_clear()

    with captured_log_messages("dtxwiki.startup", logging.WARNING) as messages:
        _make_settings(tmp_pem).validate_for_startup()

    assert not messages


def test_no_public_base_url_fails(tmp_pem):
    s = _make_settings(tmp_pem, public_base_url=None)
    with pytest.raises(RuntimeError, match="DTXWIKI_PUBLIC_BASE_URL"):
        s.validate_for_startup()


def test_http_public_base_url_fails(tmp_pem):
    s = _make_settings(tmp_pem, public_base_url="http://wiki.example.com")
    with pytest.raises(RuntimeError, match="https://"):
        s.validate_for_startup()


def test_internal_https_name_and_ip_pass(tmp_pem):
    # The 3 https TLS modes (acme/internal/custom) need no guard change: an
    # internal hostname or a private IP over https is accepted as-is.
    for origin in ("https://wiki.internal", "https://192.168.1.50"):
        s = _make_settings(
            tmp_pem, public_base_url=origin, cors_origins=origin, tls_mode="internal"
        )
        s.validate_for_startup()  # must not raise


def test_http_passes_only_with_insecure_opt_in(tmp_pem):
    origin = "http://192.168.1.50"
    # Without the opt-in, plain http is rejected (guard a).
    s = _make_settings(tmp_pem, public_base_url=origin, cors_origins=origin)
    with pytest.raises(RuntimeError, match="https://"):
        s.validate_for_startup()
    # With the eval-only opt-in it is allowed (loudly — see warning below).
    s = _make_settings(
        tmp_pem,
        public_base_url=origin,
        cors_origins=origin,
        tls_mode="none",
        allow_insecure_http=True,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s.validate_for_startup()  # must not raise
    assert any("plaintext" in str(w.message).lower() for w in caught)


def test_tls_fields_are_real_not_unknown_env(tmp_pem, monkeypatch):
    # The new knobs are declared Settings fields, so setting them via env must
    # NOT trip guard (h)'s unknown-DTXWIKI_* rejection.
    monkeypatch.setenv("DTXWIKI_TLS_MODE", "internal")
    monkeypatch.setenv("DTXWIKI_ALLOW_INSECURE_HTTP", "false")
    s = _make_settings(tmp_pem)
    s.validate_for_startup()  # must not raise


def test_cors_unset_derives_from_base_url(tmp_pem):
    # Unset CORS defaults to the app's own origin (same-origin), so the operator
    # sets the domain once via DTXWIKI_PUBLIC_BASE_URL and does not repeat it.
    s = _make_settings(tmp_pem, cors_origins=None)
    s.validate_for_startup()  # must not raise
    assert s.cors_origin_list == ["https://wiki.example.com"]


def test_dev_session_secret_fails(tmp_pem):
    s = _make_settings(tmp_pem, session_secret="dtxwiki-development-session-secret")
    with pytest.raises(RuntimeError, match="DTXWIKI_SESSION_SECRET"):
        s.validate_for_startup()


def test_wildcard_forwarded_ips_fails(tmp_pem):
    s = _make_settings(tmp_pem, forwarded_allow_ips="*")
    with pytest.raises(RuntimeError, match="DTXWIKI_FORWARDED_ALLOW_IPS"):
        s.validate_for_startup()


def test_non_sqlite_url_warns_with_epilogue_hint(tmp_pem):
    s = _make_settings(
        tmp_pem, database_url="postgresql+pg8000://user:pass@localhost/db"
    )
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        s.validate_for_startup()
    # Filter to UserWarning only (exclude Pydantic deprecation warnings)
    user_warns = [x for x in w if issubclass(x.category, UserWarning)]
    assert len(user_warns) == 1
    assert "Sprint_5 epilogue" in str(user_warns[0].message)
    assert "ADR A14" in str(user_warns[0].message)


def test_dev_token_env_fails(tmp_pem, monkeypatch):
    monkeypatch.setenv("DTXWIKI_DEV_TOKEN", "secret")
    s = _make_settings(tmp_pem)
    with pytest.raises(RuntimeError, match="DTXWIKI_DEV_TOKEN"):
        s.validate_for_startup()


def test_metrics_enabled_without_protection_fails(tmp_pem):
    s = _make_settings(
        tmp_pem,
        metrics_enabled=True,
        metrics_bearer_token=None,
        metrics_allowlist=None,
    )
    with pytest.raises(RuntimeError, match="DTXWIKI_METRICS_ENABLED"):
        s.validate_for_startup()


def test_metrics_enabled_raw_bool_word_fails(tmp_pem, monkeypatch):
    monkeypatch.setenv("DTXWIKI_METRICS_ENABLED", "true")
    s = _make_settings(tmp_pem, metrics_enabled=False)
    with pytest.raises(
        RuntimeError,
        match="DTXWIKI_METRICS_ENABLED must be set to 0 or 1",
    ):
        s.validate_for_startup()


def test_unknown_dtxwiki_env_typo_fails(tmp_pem, monkeypatch):
    monkeypatch.setenv("DTXWIKI_TYPO_FIELD_XYZ", "value")
    s = _make_settings(tmp_pem)
    with pytest.raises(RuntimeError, match="DTXWIKI_TYPO_FIELD_XYZ"):
        s.validate_for_startup()


def test_operational_env_not_rejected_by_unknown_guard(tmp_pem, monkeypatch):
    # DTXWIKI_DATA_DIR / DTXWIKI_LIBRARY_FILE_ROOTS are read directly via
    # os.environ by free functions outside Settings — legitimate in production,
    # so guard (h) must NOT reject them (regression: they used to crash startup).
    monkeypatch.setenv("DTXWIKI_DATA_DIR", "/var/lib/dtxwiki")
    monkeypatch.setenv("DTXWIKI_LIBRARY_FILE_ROOTS", "/srv/library")
    s = _make_settings(tmp_pem)
    s.validate_for_startup()  # must not raise


def test_service_env_prefix_not_rejected_by_unknown_guard(tmp_pem, monkeypatch):
    monkeypatch.setenv("DTXWIKI_SERVICE_SHARD_ROUTER_URL", "https://svc.example")
    s = _make_settings(tmp_pem)
    s.validate_for_startup()  # must not raise


def test_no_jwt_key_path_fails_with_keygen_hint(tmp_pem):
    s = _make_settings(tmp_pem, oauth_jwt_private_key_path=None)
    with pytest.raises(RuntimeError, match="dtxwiki keygen"):
        s.validate_for_startup()


def test_invalid_jwt_key_path_fails_with_keygen_hint(tmp_path, tmp_pem):
    invalid_pem = tmp_path / "invalid.jwt.pem"
    invalid_pem.write_text(
        "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n"
    )
    s = _make_settings(tmp_pem, oauth_jwt_private_key_path=str(invalid_pem))
    with pytest.raises(RuntimeError, match="not readable or not a valid PEM"):
        s.validate_for_startup()


def test_auto_bootstrap_true_fails_with_bootstrap_hint(tmp_pem):
    s = _make_settings(tmp_pem, auto_bootstrap=True)
    with pytest.raises(RuntimeError, match="dtxwiki bootstrap"):
        s.validate_for_startup()


def test_default_secret_with_team_mode_fails_even_in_development():
    s = Settings(
        app_env="development",
        app_mode="team",
        database_url="sqlite+pysqlite:///:memory:",
    )
    with pytest.raises(RuntimeError, match="DTXWIKI_SESSION_SECRET"):
        s.validate_for_startup()


def test_default_secret_with_solo_remote_mode_fails_even_in_development():
    s = Settings(
        app_env="development",
        app_mode="solo_remote",
        database_url="sqlite+pysqlite:///:memory:",
    )
    with pytest.raises(RuntimeError, match="DTXWIKI_SESSION_SECRET"):
        s.validate_for_startup()


def test_real_secret_with_team_mode_passes_in_development():
    s = Settings(
        app_env="development",
        app_mode="team",
        session_secret="a-real-secret-that-is-not-default",
        database_url="sqlite+pysqlite:///:memory:",
    )
    s.validate_for_startup()  # must not raise


def test_default_secret_with_solo_local_passes_in_development():
    s = Settings(
        app_env="development",
        app_mode="solo_local",
        database_url="sqlite+pysqlite:///:memory:",
    )
    s.validate_for_startup()  # must not raise


@pytest.mark.parametrize("mode", ["team", "solo_remote"])
def test_remote_mode_with_dev_env_warns_about_disabled_guards(mode):
    s = Settings(
        app_env="development",
        app_mode=mode,
        session_secret="a-real-secret-that-is-not-default",
        database_url="sqlite+pysqlite:///:memory:",
    )
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        s.validate_for_startup()
    user_warns = [x for x in w if issubclass(x.category, UserWarning)]
    assert len(user_warns) == 1
    message = str(user_warns[0].message)
    assert f"DTXWIKI_APP_MODE={mode}" in message
    assert "DTXWIKI_APP_ENV=production" in message


def test_solo_local_dev_does_not_warn_about_app_mode():
    s = Settings(
        app_env="development",
        app_mode="solo_local",
        database_url="sqlite+pysqlite:///:memory:",
    )
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        s.validate_for_startup()
    assert [x for x in w if issubclass(x.category, UserWarning)] == []


def test_production_team_mode_does_not_emit_cross_check_warning(tmp_pem):
    s = _make_settings(tmp_pem, app_mode="team")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        s.validate_for_startup()
    assert [x for x in w if issubclass(x.category, UserWarning)] == []

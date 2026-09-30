"""Tests for infra/prod/.env.example (Task 4.7)."""
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.app.settings import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / "infra" / "prod" / ".env.example"


def test_env_example_exists():
    assert ENV_EXAMPLE.is_file()


def test_env_example_uses_sqlite_default():
    content = ENV_EXAMPLE.read_text()
    active_db_lines = [
        line
        for line in content.splitlines()
        if line.startswith("DTXWIKI_DATABASE_URL=") and "sqlite" in line
    ]
    assert len(active_db_lines) >= 1, "Active DATABASE_URL should be SQLite"
    assert active_db_lines[0].startswith("DTXWIKI_DATABASE_URL=sqlite+pysqlite://")


def test_env_example_pg_variant_commented_out():
    content = ENV_EXAMPLE.read_text()
    for line in content.splitlines():
        if "postgresql+pg8000" in line:
            assert line.strip().startswith("#"), (
                f"PG line must be commented out: {line!r}"
            )


def test_env_example_documents_postgres_not_supported():
    # Production is SQLite-only; the commented PG line must point at the doc that
    # explains the unsupported-external-DB stance (replaced the Sprint_5 epilogue
    # pointer, which the operator-facing template no longer carries).
    content = ENV_EXAMPLE.read_text()
    assert "DEVELOPMENT.md" in content
    assert "not a supported production path" in content


def test_env_example_auto_bootstrap_false():
    content = ENV_EXAMPLE.read_text()
    assert "DTXWIKI_AUTO_BOOTSTRAP=false" in content


def test_env_example_disables_team_signup():
    content = ENV_EXAMPLE.read_text()
    assert "\nDTXWIKI_SIGNUP_ENABLED=false\n" in content


def test_env_example_defaults_to_admin_assisted_recovery_and_documents_smtp():
    content = ENV_EXAMPLE.read_text()
    assert "\nDTXWIKI_MAIL_PROVIDER=none\n" in content
    assert "# DTXWIKI_MAIL_FROM_ADDRESS=" in content
    assert "# DTXWIKI_SMTP_HOST=" in content
    assert "# DTXWIKI_SMTP_PASSWORD=" in content


def test_env_example_has_jwt_key_path():
    content = ENV_EXAMPLE.read_text()
    assert "DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH" in content
    assert "dtxwiki keygen" in content


def _template_values() -> dict[str, str]:
    values = {}
    for raw_line in ENV_EXAMPLE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith("DTXWIKI_"):
            continue
        key_name, raw_value = line.split("=", 1)
        values[key_name] = raw_value
    return values


def _use_template_env(monkeypatch, tmp_path, **overrides: str) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "jwt.pem"
    key_path.write_bytes(pem)

    for env_key in list(os.environ):
        if env_key.startswith("DTXWIKI_"):
            monkeypatch.delenv(env_key, raising=False)

    values = _template_values()
    values.update(
        {
            "DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH": str(key_path),
            "DTXWIKI_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'prod.sqlite'}",
            # Keep the test's own data directory (the template names the real
            # /var/lib/dtxwiki).
            "DTXWIKI_DATA_DIR": os.environ.get("DTXWIKI_DATA_DIR", str(tmp_path)),
            **overrides,
        }
    )
    for key_name, value in values.items():
        monkeypatch.setenv(key_name, value)


def test_env_example_parses_and_passes_guard(monkeypatch, tmp_path):
    _use_template_env(
        monkeypatch, tmp_path, DTXWIKI_SESSION_SECRET="test-" + "x" * 64
    )

    Settings().validate_for_startup()


def test_env_example_has_no_placeholder_a_guard_could_accept():
    values = _template_values()
    # Empty: the operator generates the secret. A placeholder here once passed
    # the production guard and signed sessions with a published string.
    assert values["DTXWIKI_SESSION_SECRET"] == ""
    for key_name, value in values.items():
        assert not (value.startswith("<") and value.endswith(">")), key_name
        assert "replace-with" not in value, key_name
        assert "change-me" not in value, key_name


def test_env_example_as_shipped_refuses_to_start(monkeypatch, tmp_path):
    _use_template_env(monkeypatch, tmp_path)

    with pytest.raises((RuntimeError, ValueError), match=r"(?i)session.?secret"):
        Settings().validate_for_startup()

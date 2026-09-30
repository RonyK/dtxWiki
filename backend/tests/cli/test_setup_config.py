"""CLI tests for `dtxwiki setup production` and `dtxwiki config`."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import backend.cli.main as cli_main
from backend.cli import prod_setup

runner = CliRunner()


def _out(result) -> str:
    """Combined stdout+stderr (this Click version routes err=True separately)."""
    text = result.stdout or ""
    try:
        text += result.stderr or ""
    except ValueError:  # mix_stderr=True — stderr already folded into stdout
        pass
    return text


def _tmp_db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'db.sqlite').as_posix()}"


def _common_paths(tmp_path: Path) -> dict[str, str]:
    return {
        "env": str(tmp_path / "dtxwiki.env"),
        "jwt": str(tmp_path / "jwt.pem"),
        "caddy": str(tmp_path / "Caddyfile"),
    }


# --------------------------------------------------------------------------- #
# config show / set
# --------------------------------------------------------------------------- #
def test_config_show_missing_file_exits_nonzero(tmp_path: Path):
    result = runner.invoke(
        cli_main.app,
        ["config", "show", "--env-file", str(tmp_path / "absent.env")],
    )
    assert result.exit_code == 1
    assert "setup production" in result.stdout


def test_config_set_then_show_roundtrip_redacts_secret(tmp_path: Path):
    env = tmp_path / "dtxwiki.env"
    set_result = runner.invoke(
        cli_main.app,
        [
            "config", "set",
            "DTXWIKI_SESSION_SECRET=super-secret-value",
            "--env-file", str(env),
            "--i-understand",
        ],
    )
    assert set_result.exit_code == 0, set_result.stdout
    assert prod_setup.get_env_value(env, "DTXWIKI_SESSION_SECRET") == (
        "super-secret-value"
    )

    show = runner.invoke(
        cli_main.app, ["config", "show", "--env-file", str(env), "--json"]
    )
    assert show.exit_code == 0
    import json

    entries = {e["key"]: e["value"] for e in json.loads(show.stdout)["entries"]}
    assert entries["DTXWIKI_SESSION_SECRET"] == "****"


def test_config_show_redacts_embedding_api_key(tmp_path: Path):
    # M6: the embedding API key is a secret and must be masked by `config show`,
    # like the other provider keys.
    env = tmp_path / "dtxwiki.env"
    env.write_text("DTXWIKI_EMBEDDING_API_KEY=sk-embed-secret\n", encoding="utf-8")

    show = runner.invoke(
        cli_main.app, ["config", "show", "--env-file", str(env), "--json"]
    )
    assert show.exit_code == 0
    import json

    entries = {e["key"]: e["value"] for e in json.loads(show.stdout)["entries"]}
    assert entries["DTXWIKI_EMBEDDING_API_KEY"] == "****"


def test_config_show_redacts_smtp_password_in_text_and_json(tmp_path: Path):
    env = tmp_path / "dtxwiki.env"
    set_result = runner.invoke(
        cli_main.app,
        [
            "config",
            "set",
            "DTXWIKI_SMTP_PASSWORD=mail-provider-secret",
            "--env-file",
            str(env),
            "--i-understand",
        ],
    )
    assert set_result.exit_code == 0, _out(set_result)
    assert "mail-provider-secret" not in _out(set_result)
    assert prod_setup.get_env_value(env, "DTXWIKI_SMTP_PASSWORD") == (
        "mail-provider-secret"
    )

    text_result = runner.invoke(
        cli_main.app,
        ["config", "show", "--env-file", str(env)],
    )
    json_result = runner.invoke(
        cli_main.app,
        ["config", "show", "--env-file", str(env), "--json"],
    )

    assert text_result.exit_code == 0, _out(text_result)
    assert json_result.exit_code == 0, _out(json_result)
    assert "mail-provider-secret" not in text_result.stdout
    assert "DTXWIKI_SMTP_PASSWORD=****" in text_result.stdout
    assert "mail-provider-secret" not in json_result.stdout
    import json

    entries = {e["key"]: e["value"] for e in json.loads(json_result.stdout)["entries"]}
    assert entries["DTXWIKI_SMTP_PASSWORD"] == "****"


def test_config_show_redacts_database_url_credentials(tmp_path: Path):
    env = tmp_path / "dtxwiki.env"
    env.write_text(
        "DTXWIKI_DATABASE_URL="
        "postgresql+pg8000://dtx:s3cret@db.internal:5432/dtx?sslpassword=keypass\n",
        encoding="utf-8",
    )

    show = runner.invoke(
        cli_main.app, ["config", "show", "--env-file", str(env), "--json"]
    )
    assert show.exit_code == 0
    import json

    entries = {e["key"]: e["value"] for e in json.loads(show.stdout)["entries"]}
    # Credentials masked, but the host/db the operator needs stay visible.
    assert entries["DTXWIKI_DATABASE_URL"] == (
        "postgresql+pg8000://dtx:****@db.internal:5432/dtx?sslpassword=****"
    )

    text = runner.invoke(cli_main.app, ["config", "show", "--env-file", str(env)])
    assert text.exit_code == 0
    assert "s3cret" not in text.stdout
    assert "keypass" not in text.stdout
    assert "db.internal:5432/dtx" in text.stdout


def test_config_set_rejects_unknown_key(tmp_path: Path):
    result = runner.invoke(
        cli_main.app,
        [
            "config", "set", "DTXWIKI_BOGUS=1",
            "--env-file", str(tmp_path / "e.env"),
        ],
    )
    assert result.exit_code == 2
    assert "unknown config key" in _out(result)


def test_config_set_allows_service_prefix_key(tmp_path: Path):
    env = tmp_path / "e.env"
    result = runner.invoke(
        cli_main.app,
        [
            "config",
            "set",
            "DTXWIKI_SERVICE_SHARD_ROUTER_URL=https://svc.example",
            "--env-file",
            str(env),
        ],
    )
    assert result.exit_code == 0, _out(result)
    assert "DTXWIKI_SERVICE_SHARD_ROUTER_URL=https://svc.example" in env.read_text()


def test_config_set_dangerous_requires_flag(tmp_path: Path):
    result = runner.invoke(
        cli_main.app,
        [
            "config", "set", "DTXWIKI_SESSION_SECRET=x",
            "--env-file", str(tmp_path / "e.env"),
        ],
    )
    assert result.exit_code == 3
    assert "REFUSED" in _out(result)
    # No file written when refused.
    assert not (tmp_path / "e.env").exists()


def test_config_set_rejects_bad_typed_value(tmp_path: Path):
    env = tmp_path / "e.env"
    result = runner.invoke(
        cli_main.app,
        ["config", "set", "DTXWIKI_BACKEND_PORT=abc", "--env-file", str(env)],
    )
    assert result.exit_code == 2
    assert "invalid value" in _out(result)
    assert not env.exists()  # nothing written when the value is invalid


def test_config_set_rejects_bad_enum_value(tmp_path: Path):
    env = tmp_path / "e.env"
    result = runner.invoke(
        cli_main.app,
        ["config", "set", "DTXWIKI_APP_MODE=banana", "--env-file", str(env)],
    )
    assert result.exit_code == 2
    assert not env.exists()


def test_config_set_rejects_non_url_public_base(tmp_path: Path):
    env = tmp_path / "e.env"
    result = runner.invoke(
        cli_main.app,
        [
            "config", "set", "DTXWIKI_PUBLIC_BASE_URL=not-a-url",
            "--env-file", str(env),
        ],
    )
    assert result.exit_code == 2
    assert not env.exists()


def test_config_set_accepts_valid_typed_value(tmp_path: Path):
    env = tmp_path / "e.env"
    result = runner.invoke(
        cli_main.app,
        ["config", "set", "DTXWIKI_BACKEND_PORT=9000", "--env-file", str(env)],
    )
    assert result.exit_code == 0, _out(result)
    assert prod_setup.get_env_value(env, "DTXWIKI_BACKEND_PORT") == "9000"


def _prod_env_file(tmp_path: Path) -> Path:
    """A valid production env file (passes all startup guards)."""
    env = tmp_path / "dtxwiki.env"
    env.write_text(
        prod_setup.render_production_env(
            domain="wiki.example.com",
            session_secret="s" * 80,
            jwt_key_path=str(tmp_path / "jwt.pem"),
            database_url=_tmp_db_url(tmp_path),
        ),
        encoding="utf-8",
    )
    return env


def test_config_set_rejects_value_breaking_prod_startup(tmp_path: Path):
    # AUTO_BOOTSTRAP=true passes type-checking but violates a production startup
    # guard, so it must be rejected up front (not only at the next restart).
    env = _prod_env_file(tmp_path)
    result = runner.invoke(
        cli_main.app,
        ["config", "set", "DTXWIKI_AUTO_BOOTSTRAP=true", "--env-file", str(env)],
    )
    assert result.exit_code == 2
    assert "production startup" in _out(result)
    # The bad value was NOT written.
    assert prod_setup.get_env_value(env, "DTXWIKI_AUTO_BOOTSTRAP") == "false"


def test_config_set_rejects_wildcard_forwarded_ips_in_prod(tmp_path: Path):
    env = _prod_env_file(tmp_path)
    result = runner.invoke(
        cli_main.app,
        ["config", "set", "DTXWIKI_FORWARDED_ALLOW_IPS=*", "--env-file", str(env)],
    )
    assert result.exit_code == 2
    assert "production startup" in _out(result)


def test_config_set_allows_valid_value_on_prod_file(tmp_path: Path):
    # A value that satisfies the production guards is accepted on a prod file.
    env = _prod_env_file(tmp_path)
    result = runner.invoke(
        cli_main.app,
        ["config", "set", "DTXWIKI_APP_NAME=My Wiki", "--env-file", str(env)],
    )
    assert result.exit_code == 0, _out(result)
    assert prod_setup.get_env_value(env, "DTXWIKI_APP_NAME") == "My Wiki"


def test_config_set_syncs_caddyfile_domain(tmp_path: Path):
    env = tmp_path / "dtxwiki.env"
    env.write_text(
        "DTXWIKI_PUBLIC_BASE_URL=https://old.example.com\n", encoding="utf-8"
    )
    caddy = tmp_path / "Caddyfile"
    caddy.write_text(
        "old.example.com {\n  reverse_proxy 127.0.0.1:21080\n}\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        cli_main.app,
        [
            "config", "set",
            "DTXWIKI_PUBLIC_BASE_URL=https://new.example.com",
            "--env-file", str(env),
            "--caddyfile", str(caddy),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "new.example.com {" in caddy.read_text(encoding="utf-8")
    assert "old.example.com" not in caddy.read_text(encoding="utf-8")
    assert "Caddyfile domain synced" in result.stdout


# --------------------------------------------------------------------------- #
# setup production
# --------------------------------------------------------------------------- #
def test_setup_requires_domain_when_no_existing_env(tmp_path: Path):
    result = runner.invoke(
        cli_main.app,
        ["setup", "production", "--env-file", str(tmp_path / "e.env")],
    )
    assert result.exit_code == 2
    assert "--domain is required" in _out(result)


def test_setup_dry_run_writes_nothing(tmp_path: Path):
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        cli_main.app,
        [
            "setup", "production",
            "--domain", "wiki.example.com",
            "--email", "admin@example.com",
            "--env-file", paths["env"],
            "--jwt-key", paths["jwt"],
            "--caddyfile", paths["caddy"],
            "--dry-run", "--skip-preflight",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "DRY-RUN complete" in result.stdout
    assert not Path(paths["env"]).exists()
    assert not Path(paths["jwt"]).exists()


def test_setup_tls_none_completion_uses_http_scheme(tmp_path: Path):
    """I13 (Codex F9): tls=none runs over plain HTTP, so the completion message
    must point the operator at http://, not a dead https:// link."""
    paths = _common_paths(tmp_path)
    db_url = _tmp_db_url(tmp_path)
    result = runner.invoke(
        cli_main.app,
        [
            "setup", "production",
            "--domain", "wiki.internal",
            "--email", "admin@example.com",
            "--env-file", paths["env"],
            "--jwt-key", paths["jwt"],
            "--database-url", db_url,
            "--tls", "none", "--insecure-eval",
            "--skip-preflight",
            "--no-systemd", "--no-caddy", "--no-smoke",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Visit http://wiki.internal" in result.stdout
    assert "https://wiki.internal" not in result.stdout
    parsed = prod_setup.read_env_file(Path(paths["env"]))
    assert parsed["DTXWIKI_PUBLIC_BASE_URL"] == "http://wiki.internal"


def test_setup_fresh_writes_env_key_and_admin(tmp_path: Path):
    paths = _common_paths(tmp_path)
    db_url = _tmp_db_url(tmp_path)
    result = runner.invoke(
        cli_main.app,
        [
            "setup", "production",
            "--domain", "wiki.example.com",
            "--email", "admin@example.com",
            "--env-file", paths["env"],
            "--jwt-key", paths["jwt"],
            "--database-url", db_url,
            "--skip-preflight",
            "--no-systemd", "--no-caddy", "--no-smoke",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Setup complete" in result.stdout

    env = Path(paths["env"])
    assert env.exists()
    assert Path(paths["jwt"]).exists()
    parsed = prod_setup.read_env_file(env)
    assert parsed["DTXWIKI_PUBLIC_BASE_URL"] == "https://wiki.example.com"
    assert parsed["DTXWIKI_AUTO_BOOTSTRAP"] == "false"
    assert parsed["DTXWIKI_SIGNUP_ENABLED"] == "false"
    assert parsed["DTXWIKI_DATABASE_URL"] == db_url
    assert len(parsed["DTXWIKI_SESSION_SECRET"]) > 40
    assert "admin@example.com created" in result.stdout
    assert "temporary password" in result.stdout


def test_setup_reconfigure_preserves_secret_and_key(tmp_path: Path):
    paths = _common_paths(tmp_path)
    db_url = _tmp_db_url(tmp_path)
    base_args = [
        "setup", "production",
        "--env-file", paths["env"],
        "--jwt-key", paths["jwt"],
        "--database-url", db_url,
        "--skip-preflight",
        "--no-systemd", "--no-caddy", "--no-smoke", "--no-admin", "--no-bootstrap",
    ]
    first = runner.invoke(
        cli_main.app, [*base_args, "--domain", "wiki.example.com"]
    )
    assert first.exit_code == 0, first.stdout
    env = Path(paths["env"])
    secret_before = prod_setup.get_env_value(env, "DTXWIKI_SESSION_SECRET")
    key_bytes_before = Path(paths["jwt"]).read_bytes()

    second = runner.invoke(
        cli_main.app,
        [*base_args, "--domain", "wiki2.example.com", "--reconfigure"],
    )
    assert second.exit_code == 0, second.stdout
    parsed = prod_setup.read_env_file(env)
    # Domain changed, but secret + key are preserved (no forced re-auth).
    assert parsed["DTXWIKI_PUBLIC_BASE_URL"] == "https://wiki2.example.com"
    assert parsed["DTXWIKI_CORS_ORIGINS"] == "https://wiki2.example.com"
    assert parsed["DTXWIKI_SESSION_SECRET"] == secret_before
    assert Path(paths["jwt"]).read_bytes() == key_bytes_before


def test_setup_reconfigure_backfills_data_dir(tmp_path: Path):
    # Codex #21: a pre-Fix-9 env file with no DTXWIKI_DATA_DIR must get it on
    # --reconfigure — otherwise uploads/library writes fall back to ~/.dtxwiki,
    # which is read-only under the systemd ProtectSystem=strict sandbox (500s).
    paths = _common_paths(tmp_path)
    env = Path(paths["env"])
    env.write_text(
        "DTXWIKI_PUBLIC_BASE_URL=https://old.example.com\n"
        "DTXWIKI_SESSION_SECRET=" + ("s" * 80) + "\n"
        "DTXWIKI_APP_ENV=production\n",
        encoding="utf-8",
    )
    assert prod_setup.get_env_value(env, "DTXWIKI_DATA_DIR") is None

    result = runner.invoke(
        cli_main.app,
        [
            "setup", "production",
            "--domain", "wiki.example.com",
            "--env-file", paths["env"],
            "--jwt-key", paths["jwt"],
            "--database-url", _tmp_db_url(tmp_path),
            "--skip-preflight",
            "--no-systemd", "--no-caddy", "--no-smoke",
            "--no-admin", "--no-bootstrap",
            "--reconfigure",
        ],
    )
    assert result.exit_code == 0, _out(result)
    assert (
        prod_setup.get_env_value(env, "DTXWIKI_DATA_DIR")
        == prod_setup.DEFAULT_DATA_DIR
    )


def test_setup_reconfigure_preserves_custom_data_dir(tmp_path: Path):
    # A custom DATA_DIR the operator already set must be preserved, not clobbered.
    paths = _common_paths(tmp_path)
    env = Path(paths["env"])
    env.write_text(
        "DTXWIKI_PUBLIC_BASE_URL=https://old.example.com\n"
        "DTXWIKI_SESSION_SECRET=" + ("s" * 80) + "\n"
        "DTXWIKI_DATA_DIR=/srv/custom/dtxwiki\n"
        "DTXWIKI_APP_ENV=production\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        cli_main.app,
        [
            "setup", "production",
            "--domain", "wiki.example.com",
            "--env-file", paths["env"],
            "--jwt-key", paths["jwt"],
            "--database-url", _tmp_db_url(tmp_path),
            "--skip-preflight",
            "--no-systemd", "--no-caddy", "--no-smoke",
            "--no-admin", "--no-bootstrap",
            "--reconfigure",
        ],
    )
    assert result.exit_code == 0, _out(result)
    assert (
        prod_setup.get_env_value(env, "DTXWIKI_DATA_DIR") == "/srv/custom/dtxwiki"
    )


@pytest.mark.parametrize(
    ("app_mode", "existing_signup", "expected"),
    [
        ("team", None, "false"),
        ("solo_remote", None, "false"),
        ("solo_local", None, "true"),
        ("team", "true", "true"),
        ("solo_local", "false", "false"),
    ],
)
def test_setup_reconfigure_derives_or_preserves_signup_setting(
    tmp_path: Path,
    app_mode: str,
    existing_signup: str | None,
    expected: str,
):
    paths = _common_paths(tmp_path)
    env = Path(paths["env"])
    lines = [
        "DTXWIKI_PUBLIC_BASE_URL=https://old.example.com",
        "DTXWIKI_SESSION_SECRET=" + ("s" * 80),
        "DTXWIKI_APP_ENV=production",
        f"DTXWIKI_APP_MODE={app_mode}",
    ]
    if existing_signup is not None:
        lines.append(f"DTXWIKI_SIGNUP_ENABLED={existing_signup}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = runner.invoke(
        cli_main.app,
        [
            "setup",
            "production",
            "--domain",
            "wiki.example.com",
            "--env-file",
            paths["env"],
            "--jwt-key",
            paths["jwt"],
            "--database-url",
            _tmp_db_url(tmp_path),
            "--skip-preflight",
            "--no-systemd",
            "--no-caddy",
            "--no-smoke",
            "--no-admin",
            "--no-bootstrap",
            "--reconfigure",
        ],
    )

    assert result.exit_code == 0, _out(result)
    assert prod_setup.get_env_value(env, "DTXWIKI_SIGNUP_ENABLED") == expected


def test_setup_preflight_dns_failure_aborts(tmp_path: Path, monkeypatch):
    def boom(domain: str):
        raise OSError("nxdomain")

    monkeypatch.setattr(prod_setup, "resolve_host", boom)
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        cli_main.app,
        [
            "setup", "production",
            "--domain", "missing.example.com",
            "--env-file", paths["env"],
            "--jwt-key", paths["jwt"],
            "--no-systemd", "--no-caddy", "--no-smoke",
            "--no-admin", "--no-bootstrap",
        ],
    )
    assert result.exit_code == 2
    assert "preflight failed" in _out(result)
    assert not Path(paths["env"]).exists()
